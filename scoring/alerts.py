"""Alert engine: run after scoring.engine on each refresh. Compares the
latest reading against prior readings and fires alerts per spec section 11,
including the flagship "stock getting cheaper while thesis intact" case.

Alerts are informational flags derived from stored scores/metrics — nothing
here recomputes a score, it only diffs what scoring.engine already wrote.
"""
import json
from datetime import date, datetime, timezone

from config.loader import load_config
from db.init_db import get_conn

COMPONENT_LABELS = {
    "hbm_demand": "HBM Demand",
    "dram_pricing": "DRAM Pricing",
    "gross_margins": "Micron Gross Margins",
    "customer_capex": "Customer AI Capex",
    "valuation": "Valuation",
}

BEARISH_THRESHOLD = 40
BULLISH_THRESHOLD = 70
MATERIAL_DELTA = 15
TURNING_POINT_LOOKBACK = 4
TURNING_POINT_MIN_MOVE = 10


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _already_fired_today(conn, category, message):
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT id FROM alerts WHERE category=? AND message=? AND triggered_at LIKE ?",
        (category, message, f"{today}%"),
    ).fetchone()
    return row is not None


def _add_alert(conn, severity, category, message, related_ids=None):
    if _already_fired_today(conn, category, message):
        return
    conn.execute(
        "INSERT INTO alerts (triggered_at, severity, category, message, related_ids, acknowledged) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (_now_iso(), severity, category, message, json.dumps(related_ids or [])),
    )


def _component_history(conn, component, limit=6):
    rows = conn.execute(
        "SELECT as_of_date, score FROM component_scores WHERE component=? AND score IS NOT NULL "
        "ORDER BY as_of_date DESC LIMIT ?",
        (component, limit),
    ).fetchall()
    return list(reversed([(r["as_of_date"], r["score"]) for r in rows]))  # oldest -> newest


def check_price_vs_fundamentals(conn, cfg):
    ticker = cfg["subject_ticker"]
    price_row = conn.execute(
        "SELECT value FROM metrics WHERE metric_key='price_change_1m_pct' AND company=? ORDER BY period_end DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    overall_row = conn.execute(
        "SELECT fundamental_score, confidence FROM overall_scores ORDER BY as_of_date DESC LIMIT 1"
    ).fetchone()
    if not price_row or not overall_row or overall_row["fundamental_score"] is None:
        return
    price_1m = price_row["value"]
    fscore = overall_row["fundamental_score"]
    if price_1m <= -10 and fscore >= 65:
        _add_alert(
            conn, "GREEN", "price_vs_fundamentals",
            f"MU fell {abs(price_1m):.1f}% over the past month while the fundamental score remains strong at "
            f"{fscore:.0f}/100 ({overall_row['confidence']} confidence) — the stock may be getting cheaper "
            f"while the thesis stays intact. Worth checking whether the decline is macro/valuation-driven "
            f"rather than a change in HBM demand, DRAM pricing, margins, or capex.",
        )
    if price_1m >= 15 and fscore < 55:
        _add_alert(
            conn, "RED", "price_vs_fundamentals",
            f"MU rose {price_1m:.1f}% over the past month while the fundamental score is only {fscore:.0f}/100 "
            f"({overall_row['confidence']} confidence) — the rally is outrunning the fundamental evidence.",
        )


def check_component_moves(conn, cfg):
    for component in ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex"]:
        label = COMPONENT_LABELS[component]
        history = _component_history(conn, component, limit=TURNING_POINT_LOOKBACK + 1)
        if len(history) < 2:
            continue
        (_, prior_score), (today_date, today_score) = history[-2], history[-1]
        delta = today_score - prior_score

        if delta <= -MATERIAL_DELTA:
            _add_alert(conn, "RED", component,
                       f"{label} deteriorated materially: {prior_score:.0f} -> {today_score:.0f}.")
        elif delta >= MATERIAL_DELTA:
            _add_alert(conn, "GREEN", component,
                       f"{label} improved materially: {prior_score:.0f} -> {today_score:.0f}.")

        if today_score < BEARISH_THRESHOLD <= prior_score:
            _add_alert(conn, "RED", component,
                       f"{label} turned bearish — score dropped below {BEARISH_THRESHOLD} "
                       f"({prior_score:.0f} -> {today_score:.0f}).")
        if today_score >= BULLISH_THRESHOLD > prior_score:
            _add_alert(conn, "GREEN", component,
                       f"{label} turned bullish — score crossed above {BULLISH_THRESHOLD} "
                       f"({prior_score:.0f} -> {today_score:.0f}).")

        # Turning point: monotonic move over the last N readings, even if
        # still comfortably in bullish/bearish territory (spec section 8).
        if len(history) >= TURNING_POINT_LOOKBACK:
            recent = history[-TURNING_POINT_LOOKBACK:]
            scores = [s for _, s in recent]
            declining = all(scores[i] > scores[i + 1] for i in range(len(scores) - 1))
            improving = all(scores[i] < scores[i + 1] for i in range(len(scores) - 1))
            move = scores[0] - scores[-1]
            path = " -> ".join(f"{s:.0f}" for s in scores)
            if declining and move >= TURNING_POINT_MIN_MOVE:
                _add_alert(conn, "RED", component,
                           f"{label} turning-point warning: {TURNING_POINT_LOOKBACK} consecutive declining "
                           f"readings ({path}) — still may look fine in isolation, but momentum is deteriorating.")
            if improving and -move >= TURNING_POINT_MIN_MOVE:
                _add_alert(conn, "GREEN", component,
                           f"{label} strengthening trend: {TURNING_POINT_LOOKBACK} consecutive improving "
                           f"readings ({path}).")


def run(as_of_date=None):
    cfg = load_config()
    conn = get_conn()
    check_price_vs_fundamentals(conn, cfg)
    check_component_moves(conn, cfg)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
    conn = get_conn()
    rows = conn.execute("SELECT triggered_at, severity, category, message FROM alerts ORDER BY id DESC LIMIT 20").fetchall()
    for r in rows:
        print(f"[{r['severity']}] {r['category']}: {r['message']}")
    conn.close()
