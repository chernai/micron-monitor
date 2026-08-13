"""Scoring engine: reads observations/metrics, applies the rubric in
rubric.py, and writes component_scores + overall_scores. This is the only
place scores get computed — the dashboard never recalculates, it just
displays what's stored here, so every number a user sees can be traced back
to this code and the observations that fed it.
"""
import json
from datetime import date, datetime, timedelta

from config.loader import load_config
from db.init_db import get_conn
from scoring import rubric

COMPONENTS = ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex", "valuation"]


def get_metric_series(conn, metric_key, company, as_of_date):
    """Point-in-time correct: only returns metric values whose SOURCE
    OBSERVATION was itself known (filed/published) by as_of_date, not just
    values whose fiscal period ended by then. A quarter's gross margin isn't
    knowable until the 10-Q reporting it is actually filed — using
    period_end alone would let a historical backfill 'see the future'
    (e.g. crediting a date in June with a quarter that wasn't filed until
    the following month). Metrics with no source_observation_id (shouldn't
    happen post-fix, see store.upsert_metric) are excluded rather than
    assumed known, since we can't verify when they became known.
    """
    rows = conn.execute(
        """
        SELECT m.period_end, m.value FROM metrics m
        JOIN observations o ON o.id = m.source_observation_id
        WHERE m.metric_key=? AND m.company=? AND o.obs_date <= ?
        ORDER BY m.period_end ASC
        """,
        (metric_key, company, as_of_date),
    ).fetchall()
    return [(r["period_end"], r["value"]) for r in rows]


def _closest_to(series, target_date_str, tolerance_days=20):
    target = date.fromisoformat(target_date_str)
    best = None
    best_diff = None
    for period_end, value in series:
        d = date.fromisoformat(period_end)
        diff = abs((d - target).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = (period_end, value), diff
    return best


def _recent_observations(conn, category, lookback_days, as_of_date, require_text=True):
    as_of = date.fromisoformat(as_of_date)
    cutoff = (as_of - timedelta(days=lookback_days)).isoformat()
    q = "SELECT * FROM observations WHERE category=? AND obs_date>=? AND obs_date<=?"
    if require_text:
        q += " AND text_excerpt IS NOT NULL"
    rows = conn.execute(q, (category, cutoff, as_of_date)).fetchall()
    return rows


def _keyword_score(conn, cfg, category, as_of_date):
    """Shared logic for hbm_demand / dram_pricing: keyword rubric over
    recent news + any management-guidance text observations."""
    lookback = cfg["lookback_days"][category]
    obs = _recent_observations(conn, category, lookback, as_of_date, require_text=True)
    min_obs = cfg["min_observations"][category]

    if len(obs) < min_obs:
        return {
            "score": None, "insufficient_data": True, "confidence": None,
            "rationale": f"Insufficient data: only {len(obs)} sourced item(s) in the last {lookback} days "
                         f"(need >= {min_obs}). No score computed.",
            "evidence_ids": [],
        }

    strong_kw = rubric.STRONG_KEYWORDS[category]
    weak_kw = rubric.WEAK_KEYWORDS[category]
    weighted_net = 0.0
    medium_hits = 0
    low_hits = 0
    evidence = []
    for o in obs:
        text = o["text_excerpt"] or ""
        s_hits = rubric.count_keyword_hits(text, strong_kw)
        w_hits = rubric.count_keyword_hits(text, weak_kw)
        if s_hits == 0 and w_hits == 0:
            continue
        weight = 2 if o["confidence"] == "MEDIUM" else 1
        weighted_net += weight * (s_hits - w_hits)
        if o["confidence"] == "MEDIUM":
            medium_hits += 1
        else:
            low_hits += 1
        evidence.append(o["id"])

    total_hits = medium_hits + low_hits
    if total_hits == 0:
        return {
            "score": 50.0, "insufficient_data": False, "confidence": "LOW",
            "rationale": f"{len(obs)} news item(s) reviewed in the last {lookback} days but none matched "
                         f"the strong/weak signal keyword list — no clear directional signal. Defaulting to neutral (50).",
            "evidence_ids": [],
        }

    import math
    # Normalize by sqrt(hit count) rather than a fixed constant: with a fixed
    # divisor, categories with a lot of news volume (dozens+ of matched
    # headlines) saturate to 0/100 almost immediately and lose all ability to
    # show acceleration/deceleration over time. Dividing by sqrt(hits) still
    # rewards more confirming evidence but leaves headroom for the score to
    # move as sentiment shifts, which is what the trend/momentum tracking needs.
    normalized = weighted_net / (total_hits ** 0.5)
    score = rubric.clip(50 + 50 * math.tanh(normalized / 6))
    confidence = "MEDIUM" if (medium_hits / total_hits) >= 0.5 else "LOW"
    direction = "more STRONG than WEAK signals" if weighted_net > 0 else (
        "more WEAK than STRONG signals" if weighted_net < 0 else "balanced STRONG/WEAK signals")
    rationale = (
        f"Keyword rubric over {len(obs)} news items ({lookback}d window): {total_hits} items matched "
        f"strong/weak signal language, net signal {direction} (weighted net={weighted_net:.0f} across "
        f"{total_hits} matched items). "
        f"This is a headline-keyword heuristic, not verified analysis — treat as directional evidence only, "
        f"not a fact about actual {category.replace('_', ' ')} conditions."
    )
    return {
        "score": round(score, 1), "insufficient_data": False, "confidence": confidence,
        "rationale": rationale, "evidence_ids": evidence[:25],
    }


def score_hbm_demand(conn, cfg, as_of_date):
    return _keyword_score(conn, cfg, "hbm_demand", as_of_date)


def score_dram_pricing(conn, cfg, as_of_date):
    return _keyword_score(conn, cfg, "dram_pricing", as_of_date)


def score_gross_margins(conn, cfg, as_of_date):
    ticker = cfg["subject_ticker"]
    series = get_metric_series(conn, "gross_margin_pct", ticker, as_of_date)
    min_obs = cfg["min_observations"]["gross_margins"]
    if len(series) < min_obs:
        return {"score": None, "insufficient_data": True, "confidence": None,
                "rationale": "Insufficient data: no gross margin history available from SEC filings.",
                "evidence_ids": []}

    latest_period, latest_margin = series[-1]
    level_score = rubric.gross_margin_level_score(latest_margin)

    seq_delta = None
    if len(series) >= 2:
        seq_delta = latest_margin - series[-2][1]
    seq_adj = rubric.gross_margin_change_adjustment(seq_delta, "sequential")

    yoy_target = (date.fromisoformat(latest_period) - timedelta(days=365)).isoformat()
    yoy_match = _closest_to(series[:-1], yoy_target)
    yoy_delta = (latest_margin - yoy_match[1]) if yoy_match else None
    yoy_adj = rubric.gross_margin_change_adjustment(yoy_delta, "yoy")

    score = rubric.clip(level_score + seq_adj + yoy_adj)

    # Cross-reference with DRAM pricing direction for the "why" narrative (section 3 ask)
    dram = score_dram_pricing(conn, cfg, as_of_date)
    if dram["score"] is not None and seq_delta is not None:
        if seq_delta > 0 and dram["score"] > 55:
            margin_reason = "consistent with a strengthening DRAM pricing environment"
        elif seq_delta > 0 and dram["score"] <= 55:
            margin_reason = "not clearly corroborated by DRAM pricing news — may reflect mix shift (HBM/AI) or utilization, worth checking the earnings call"
        elif seq_delta < 0 and dram["score"] < 45:
            margin_reason = "consistent with softening DRAM pricing"
        else:
            margin_reason = "direction not clearly explained by the DRAM pricing signal alone"
    else:
        margin_reason = "insufficient DRAM pricing data to explain the margin move"

    rationale = (
        f"Gross margin {latest_margin:.1f}% as of {latest_period} (level score {level_score}). "
        f"Sequential change {seq_delta:+.1f}pp" if seq_delta is not None else f"Gross margin {latest_margin:.1f}% as of {latest_period}."
    )
    rationale += (f", YoY change {yoy_delta:+.1f}pp. " if yoy_delta is not None else ". ")
    rationale += f"Margin trend is {margin_reason}."

    return {
        "score": round(score, 1), "insufficient_data": False, "confidence": "HIGH",
        "rationale": rationale, "evidence_ids": [],
        "detail": {"latest_margin": latest_margin, "seq_delta": seq_delta, "yoy_delta": yoy_delta},
    }


def score_customer_capex(conn, cfg, as_of_date):
    companies = list(cfg["capex_universe"].keys())
    signals = []
    per_company = {}
    for ticker in companies:
        series = get_metric_series(conn, "capex_usd", ticker, as_of_date)
        if len(series) < 2:
            continue
        latest_period, latest_val = series[-1]
        yoy_target = (date.fromisoformat(latest_period) - timedelta(days=365)).isoformat()
        yoy_match = _closest_to(series[:-1], yoy_target)
        if not yoy_match or not yoy_match[1]:
            continue
        yoy_growth = (latest_val / yoy_match[1] - 1) * 100
        sig = rubric.capex_growth_signal(yoy_growth)
        if sig is not None:
            signals.append(sig)
            per_company[ticker] = {"period": latest_period, "yoy_growth_pct": round(yoy_growth, 1)}

    min_obs = cfg["min_observations"]["customer_capex"]
    if len(signals) < min_obs:
        return {"score": None, "insufficient_data": True, "confidence": None,
                "rationale": "Insufficient data: not enough YoY capex history across tracked hyperscalers/chipmakers.",
                "evidence_ids": []}

    avg_signal = sum(signals) / len(signals)
    base_score = rubric.clip(50 + avg_signal * 25)

    # Small guidance-language nudge from recent capex-category news
    lookback = cfg["lookback_days"]["customer_capex"]
    obs = _recent_observations(conn, "customer_capex", lookback, as_of_date, require_text=True)
    strong_hits = sum(rubric.count_keyword_hits(o["text_excerpt"] or "", rubric.CAPEX_GUIDANCE_STRONG) for o in obs)
    weak_hits = sum(rubric.count_keyword_hits(o["text_excerpt"] or "", rubric.CAPEX_GUIDANCE_WEAK) for o in obs)
    nudge = rubric.clip((strong_hits - weak_hits) * 1.5, -10, 10)
    score = rubric.clip(base_score + nudge)

    detail_str = ", ".join(f"{t} {v['yoy_growth_pct']:+.0f}% YoY" for t, v in per_company.items())
    rationale = (
        f"YoY capex growth across {len(signals)} tracked companies: {detail_str}. "
        f"Average growth signal maps to base score {base_score:.0f}/100. "
        f"News guidance-language nudge: {strong_hits} bullish vs {weak_hits} bearish mentions ({nudge:+.0f})."
    )
    return {
        "score": round(score, 1), "insufficient_data": False,
        "confidence": "HIGH" if len(signals) >= 4 else "MEDIUM",
        "rationale": rationale, "evidence_ids": [], "detail": per_company,
    }


def score_valuation(conn, cfg, as_of_date):
    ticker = cfg["subject_ticker"]

    if as_of_date != date.today().isoformat():
        # We only ever capture a snapshot of forward P/E (today's), never a
        # history of it -- yfinance gives no historical-estimates series.
        # Approximating a historical valuation with a different methodology
        # (e.g. trailing price percentile alone) would silently mix two
        # incompatible approaches into one series, so this is left an honest
        # gap for backfilled dates rather than approximated.
        return {"score": None, "insufficient_data": True, "confidence": None,
                "rationale": "Insufficient data: historical forward P/E is not available (only today's "
                             "snapshot is ever captured), so valuation cannot be backfilled for past dates.",
                "evidence_ids": []}

    def latest_metric(key):
        series = get_metric_series(conn, key, ticker, as_of_date)
        return series[-1][1] if series else None

    forward_pe = latest_metric("forward_pe")
    dist_from_high = latest_metric("price_dist_from_52w_high_pct")

    if forward_pe is None:
        return {"score": None, "insufficient_data": True, "confidence": None,
                "rationale": "Insufficient data: forward P/E not available (Yahoo Finance valuation fields missing).",
                "evidence_ids": []}

    pe_score = rubric.forward_pe_band_score(forward_pe)
    bonus = rubric.distance_from_high_bonus(dist_from_high)
    score = rubric.clip((pe_score or 50) + bonus)

    rationale = f"Forward P/E {forward_pe:.1f}x (band score {pe_score}). "
    if dist_from_high is not None:
        rationale += f"Price is {dist_from_high:+.1f}% vs 52-week high (adjustment {bonus:+d}). "
    rationale += "Valuation is intentionally weighted low (10%) and never used alone to drive the signal."

    return {
        "score": round(score, 1), "insufficient_data": False, "confidence": "MEDIUM",
        "rationale": rationale, "evidence_ids": [],
        "detail": {"forward_pe": forward_pe, "dist_from_high_pct": dist_from_high},
    }


SCORERS = {
    "hbm_demand": score_hbm_demand,
    "dram_pricing": score_dram_pricing,
    "gross_margins": score_gross_margins,
    "customer_capex": score_customer_capex,
    "valuation": score_valuation,
}


def _prior_score(conn, component, before_date):
    row = conn.execute(
        "SELECT score FROM component_scores WHERE component=? AND as_of_date<? AND score IS NOT NULL "
        "ORDER BY as_of_date DESC LIMIT 1",
        (component, before_date),
    ).fetchone()
    return row["score"] if row else None


def _trend(new_score, prior_score, cfg):
    if new_score is None or prior_score is None:
        return None
    delta = new_score - prior_score
    if delta >= cfg["trend_thresholds"]["improving"]:
        return "up"
    if delta <= cfg["trend_thresholds"]["deteriorating"]:
        return "down"
    return "stable"


def compute_and_store(as_of_date=None):
    cfg = load_config()
    conn = get_conn()
    as_of_date = as_of_date or date.today().isoformat()

    results = {}
    for component, fn in SCORERS.items():
        r = fn(conn, cfg, as_of_date)
        prior = _prior_score(conn, component, as_of_date)
        trend = _trend(r["score"], prior, cfg)
        conn.execute(
            "INSERT INTO component_scores (component, as_of_date, score, insufficient_data, confidence, "
            "rationale, evidence_ids, trend) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(component, as_of_date) DO UPDATE SET score=excluded.score, "
            "insufficient_data=excluded.insufficient_data, confidence=excluded.confidence, "
            "rationale=excluded.rationale, evidence_ids=excluded.evidence_ids, trend=excluded.trend",
            (component, as_of_date, r["score"], int(r["insufficient_data"]), r["confidence"],
             r["rationale"], json.dumps(r["evidence_ids"]), trend),
        )
        results[component] = {**r, "trend": trend, "prior_score": prior}

    # Fundamental score = weighted avg of hbm_demand, dram_pricing, gross_margins, customer_capex
    weights = cfg["weights"]
    fundamental_components = ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex"]
    available = [c for c in fundamental_components if not results[c]["insufficient_data"]]
    missing = [c for c in fundamental_components if results[c]["insufficient_data"]]

    if available:
        total_w = sum(weights[c] for c in available)
        fundamental_score = sum(results[c]["score"] * weights[c] for c in available) / total_w
    else:
        fundamental_score = None

    valuation_score = results["valuation"]["score"]

    thresholds = cfg["signal_thresholds"]
    if fundamental_score is None:
        signal = "NEUTRAL_WAIT"
        overall_confidence = "LOW"
        explanation = "Insufficient fundamental data to compute a signal. Treat as NEUTRAL/WAIT until more data is collected."
    else:
        if fundamental_score <= thresholds["risk_reduce_max_fundamental"]:
            signal = "RISK_REDUCE"
        elif fundamental_score >= thresholds["buying_opportunity_min_fundamental"] and \
                (valuation_score is None or valuation_score >= thresholds["buying_opportunity_min_price_score"]):
            signal = "BUYING_OPPORTUNITY"
        else:
            signal = "NEUTRAL_WAIT"

        confidences = [results[c]["confidence"] for c in available if results[c]["confidence"]]
        if missing or "LOW" in confidences:
            overall_confidence = "LOW" if confidences.count("LOW") > len(confidences) / 2 or len(missing) >= 2 else "MEDIUM"
        elif confidences and all(c == "HIGH" for c in confidences):
            overall_confidence = "HIGH"
        else:
            overall_confidence = "MEDIUM"

        explanation = _build_explanation(results, fundamental_score, valuation_score, signal, missing)

    overall_score = fundamental_score  # kept distinct from valuation per spec; used for the trend chart

    conn.execute(
        "INSERT INTO overall_scores (as_of_date, fundamental_score, valuation_score, overall_score, signal, "
        "confidence, missing_components, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(as_of_date) DO UPDATE SET fundamental_score=excluded.fundamental_score, "
        "valuation_score=excluded.valuation_score, overall_score=excluded.overall_score, signal=excluded.signal, "
        "confidence=excluded.confidence, missing_components=excluded.missing_components, explanation=excluded.explanation",
        (as_of_date, fundamental_score, valuation_score, overall_score, signal, overall_confidence,
         json.dumps(missing), explanation),
    )
    conn.commit()
    conn.close()
    return {
        "as_of_date": as_of_date, "components": results, "fundamental_score": fundamental_score,
        "valuation_score": valuation_score, "signal": signal, "confidence": overall_confidence,
        "missing_components": missing, "explanation": explanation,
    }


COMPONENT_LABELS = {
    "hbm_demand": "HBM Demand", "dram_pricing": "DRAM Pricing", "gross_margins": "Gross Margins",
    "customer_capex": "Customer Capex", "valuation": "Valuation",
}


def _build_explanation(results, fundamental_score, valuation_score, signal, missing):
    bullets = []
    for c in ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex", "valuation"]:
        r = results[c]
        if r["insufficient_data"]:
            continue
        arrow = {"up": "improving", "down": "deteriorating", "stable": "stable", None: "first reading"}[r["trend"]]
        bullets.append(f"{COMPONENT_LABELS[c]}: {r['score']:.0f}/100 ({arrow})")
    header = {
        "BUYING_OPPORTUNITY": "Fundamentals are strong relative to price.",
        "NEUTRAL_WAIT": "Signal is mixed or valuation looks stretched relative to fundamentals.",
        "RISK_REDUCE": "Fundamentals are weak enough to outweigh any valuation appeal.",
    }[signal]
    if missing:
        header += f" (Note: {', '.join(COMPONENT_LABELS.get(m, m) for m in missing)} excluded from fundamental score — insufficient data.)"
    return header + " | " + " | ".join(bullets)


if __name__ == "__main__":
    result = compute_and_store()
    print(f"Signal: {result['signal']} | Fundamental: {result['fundamental_score']} | "
          f"Valuation: {result['valuation_score']} | Confidence: {result['confidence']}")
    print(result["explanation"])
