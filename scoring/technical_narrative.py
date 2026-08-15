"""Technical Analysis narrative: a deterministic, rule-based read of moving
averages, volume-spread (where price closed within a bar's own high/low
range), and overhead supply from past high-volume down bars -- the kind of
discretionary chart read a trader would type up by eye. No ML/black-box
classification; every sentence traces back to a threshold in
config.yaml's technicals section.

Separate from scoring/technicals.py (which feeds the Technical Timing
Score) -- this never feeds any score or the signal. It's a narrative
overlay on the same price/volume data, triggered on demand from the
dashboard, not persisted.
"""
from datetime import date, timedelta


def get_ohlcv_series(conn, company):
    """Ascending list of dicts with date/open/high/low/close/volume, for
    days where all five are known. Yahoo Finance's chart API always returns
    a full OHLCV bar together, so in practice this matches
    technicals.get_price_volume_series() day-for-day once OHLC backfill has
    run once."""
    rows = conn.execute(
        """
        SELECT c.period_end AS d, o.value AS open, h.value AS high, l.value AS low,
               c.value AS close, v.value AS volume
        FROM metrics c
        JOIN metrics o ON o.metric_key = 'price_open_usd' AND o.company = c.company AND o.period_end = c.period_end
        JOIN metrics h ON h.metric_key = 'price_high_usd' AND h.company = c.company AND h.period_end = c.period_end
        JOIN metrics l ON l.metric_key = 'price_low_usd' AND l.company = c.company AND l.period_end = c.period_end
        JOIN metrics v ON v.metric_key = 'volume_shares' AND v.company = c.company AND v.period_end = c.period_end
        WHERE c.metric_key = 'price_usd' AND c.company = ?
        ORDER BY c.period_end ASC
        """,
        (company,),
    ).fetchall()
    return [
        {"date": r["d"], "open": r["open"], "high": r["high"], "low": r["low"],
         "close": r["close"], "volume": r["volume"]}
        for r in rows
    ]


def _moving_average(values, window):
    return [
        (sum(values[i + 1 - window:i + 1]) / window) if i + 1 >= window else None
        for i in range(len(values))
    ]


def _classify_ma_slope(cfg, dates, ma_series, label):
    tcfg = cfg["technicals"]
    lookback = tcfg["ma_slope_lookback_days"]
    flat_pct = tcfg["ma_flat_slope_pct"]
    valid_idx = [i for i, v in enumerate(ma_series) if v is not None]
    if len(valid_idx) < lookback + 1:
        return {"label": label, "available": False}

    last_i = valid_idx[-1]
    ref_i = max(valid_idx[0], last_i - lookback)
    last_v, ref_v = ma_series[last_i], ma_series[ref_i]
    if not ref_v:
        return {"label": label, "available": False}

    slope_pct = (last_v - ref_v) / ref_v * 100
    if abs(slope_pct) < flat_pct:
        direction = "flat"
    else:
        direction = "up" if slope_pct > 0 else "down"
    return {
        "label": label, "available": True, "direction": direction,
        "slope_pct": slope_pct, "value": last_v, "date": dates[last_i],
    }


def _detect_ma_retake(cfg, dates, closes, ma_series, label):
    """Finds the start of the current 'closing above this MA' streak and
    confirms it was actually a crossover (the day before, price was below
    it) that happened recently enough to call out."""
    n = len(closes)
    if n < 2 or ma_series[-1] is None or closes[-1] is None or closes[-1] < ma_series[-1]:
        return None
    i = n - 1
    while i - 1 >= 0 and ma_series[i - 1] is not None and closes[i - 1] is not None \
            and closes[i - 1] >= ma_series[i - 1]:
        i -= 1
    if i == 0 or ma_series[i - 1] is None or closes[i - 1] is None:
        return None  # ran out of data before finding a confirmed crossover
    days_ago = n - 1 - i
    if days_ago > cfg["technicals"]["ma_retake_lookback_days"]:
        return None
    return {"label": label, "date": dates[i], "days_ago": days_ago}


def _analyze_bars(cfg, series):
    """One entry per bar (None for the warm-up period before there's a
    trailing volume average): whether it's a 'big' volume bar, red/green,
    and where the close sits within that bar's own high/low range -- the
    classic volume-spread read (closed near the low on heavy volume =
    real selling; closed near the high despite being red = buying
    absorbing the supply)."""
    tcfg = cfg["technicals"]
    avg_days = tcfg["big_volume_avg_days"]
    mult = tcfg["big_volume_multiplier"]
    lo_max = tcfg["close_position_low_max"]
    hi_min = tcfg["close_position_high_min"]
    volumes = [b["volume"] for b in series]

    out = []
    for i, b in enumerate(series):
        if i < avg_days:
            out.append(None)
            continue
        avg_vol = sum(volumes[i - avg_days:i]) / avg_days
        rng = b["high"] - b["low"]
        close_pos = (b["close"] - b["low"]) / rng if rng > 0 else 0.5
        if close_pos <= lo_max:
            pos_label = "near_low"
        elif close_pos >= hi_min:
            pos_label = "near_high"
        else:
            pos_label = "mid"
        out.append({
            "date": b["date"], "close": b["close"], "high": b["high"], "low": b["low"],
            "volume": b["volume"], "avg_volume": avg_vol,
            "is_big_volume": avg_vol > 0 and b["volume"] >= mult * avg_vol,
            "direction": "green" if b["close"] >= b["open"] else "red",
            "close_position": close_pos, "pos_label": pos_label,
        })
    return out


def _detect_overhead_supply(cfg, series, bars, current_price):
    """Past big-volume RED bars whose range sits above today's price --
    buyers trapped underwater there tend to sell into any rally back up to
    their entry, i.e. resistance from real supply, not just a chart line."""
    tcfg = cfg["technicals"]
    if not series:
        return []
    cutoff = (date.fromisoformat(series[-1]["date"]) - timedelta(days=tcfg["overhead_supply_lookback_days"])).isoformat()
    zones = [
        {"date": b["date"], "price_low": b["low"], "price_high": b["high"], "volume": b["volume"]}
        for b, a in zip(series, bars)
        if a is not None and b["date"] >= cutoff and a["is_big_volume"] and a["direction"] == "red"
        and b["low"] > current_price
    ]
    zones.sort(key=lambda z: z["price_low"])  # closest overhead zone first
    return zones[:tcfg["overhead_supply_max_zones"]]


def _analyze_trend_leg_origin(cfg, series, bars, origin_date):
    """Reads the bars from a swing-low origin forward: how much of that
    move happened with no overhead supply and no serious red-volume
    selling (red bars that still closed near their high don't count as
    real distribution -- buying absorbed it)."""
    idx = next((i for i, b in enumerate(series) if b["date"] == origin_date), None)
    if idx is None:
        return None
    leg = [a for a in bars[idx:] if a is not None]
    if not leg:
        return None
    big_red = [a for a in leg if a["is_big_volume"] and a["direction"] == "red"]
    red_bars = [a for a in leg if a["direction"] == "red"]
    weak_red = [a for a in red_bars if a["pos_label"] == "near_high"]
    return {
        "origin_date": origin_date, "num_bars": len(leg),
        "big_red_count": len(big_red), "red_count": len(red_bars), "weak_red_count": len(weak_red),
        "clean_advance": len(big_red) == 0,
    }


def _detect_reversal_bar(cfg, series, bars, lookback_days=60):
    """Most recent big-volume red bar that closed near its low -- the kind
    of bar that, in hindsight, tends to mark where an up-move stalled."""
    if not series:
        return None
    cutoff = (date.fromisoformat(series[-1]["date"]) - timedelta(days=lookback_days)).isoformat()
    hits = [
        (i, a) for i, (b, a) in enumerate(zip(series, bars))
        if a is not None and b["date"] >= cutoff and a["is_big_volume"]
        and a["direction"] == "red" and a["pos_label"] == "near_low"
    ]
    if not hits:
        return None
    idx, a = hits[-1]
    return {"date": a["date"], "close": a["close"], "volume": a["volume"],
            "days_ago": len(series) - 1 - idx}


def _forecast(ma10, ma50, ma200, overhead_zones, reversal, current_price):
    reasons = []
    st_up = ma10.get("direction") == "up"
    st_down = ma10.get("direction") == "down"
    mt_up = ma50.get("direction") == "up"
    mt_down = ma50.get("direction") == "down"
    lt_up = ma200.get("direction") == "up"

    nearby_supply = [z for z in overhead_zones if current_price and (z["price_low"] / current_price - 1) < 0.08]
    recent_reversal = reversal and reversal["days_ago"] <= 10

    if st_up and mt_down:
        reasons.append("short-term momentum (10d) has turned up but the medium-term trend (50d) is still pointed down")
    elif st_down and mt_up:
        reasons.append("short-term momentum (10d) has turned down against a still-rising 50d trend")
    if nearby_supply:
        reasons.append(f"there's overhead supply within reach (~${nearby_supply[0]['price_low']:.0f} from the "
                        f"{nearby_supply[0]['date']} distribution bar)")
    if recent_reversal:
        reasons.append(f"the most recent big-volume down bar ({reversal['date']}) closed near its low only "
                        f"{reversal['days_ago']} day(s) ago")

    if reasons and (st_up != mt_up or nearby_supply or recent_reversal) and not (st_down and mt_down):
        outlook = "CONSOLIDATION"
        text = "Best guess: consolidation for a while rather than a clean continuation -- " + "; ".join(reasons) + "."
    elif st_up and mt_up and lt_up and not nearby_supply and not recent_reversal:
        outlook = "CONTINUATION"
        text = "Best guess: trend likely continues -- short, medium, and long-term MAs are all aligned up with no nearby overhead supply or recent heavy distribution."
    elif st_down and mt_down:
        outlook = "DOWNTREND"
        text = "Best guess: still in a downtrend on this read -- both the 10d and 50d MAs are pointed down; wait for a base to form before treating any bounce as more than that."
    else:
        outlook = "MIXED"
        text = "Best guess: mixed / no clean read -- " + ("; ".join(reasons) if reasons else "signals aren't strongly aligned either way") + "."
    return {"outlook": outlook, "text": text}


def build_technical_narrative(cfg, conn, ticker=None, as_of_date=None):
    ticker = ticker or cfg["subject_ticker"]
    full_series = get_ohlcv_series(conn, ticker)
    series = [s for s in full_series if as_of_date is None or s["date"] <= as_of_date]

    tcfg = cfg["technicals"]
    min_days = max(tcfg["big_volume_avg_days"], 200) + 20
    if len(series) < min_days:
        return {"available": False,
                "rationale": f"Insufficient data: need >= {min_days} days of OHLCV history, have {len(series)}. "
                             f"Open/High/Low backfill may still be catching up -- run a refresh."}

    dates = [s["date"] for s in series]
    closes = [s["close"] for s in series]
    current_price = closes[-1]

    ma10_series = _moving_average(closes, tcfg["ma_short_days"])
    ma50_series = _moving_average(closes, 50)
    ma200_series = _moving_average(closes, 200)
    ma10 = _classify_ma_slope(cfg, dates, ma10_series, f"{tcfg['ma_short_days']}d")
    ma50 = _classify_ma_slope(cfg, dates, ma50_series, "50d")
    ma200 = _classify_ma_slope(cfg, dates, ma200_series, "200d")
    retake50 = _detect_ma_retake(cfg, dates, closes, ma50_series, "50d")
    retake10 = _detect_ma_retake(cfg, dates, closes, ma10_series, f"{tcfg['ma_short_days']}d")

    bars = _analyze_bars(cfg, series)
    overhead_zones = _detect_overhead_supply(cfg, series, bars, current_price)
    reversal = _detect_reversal_bar(cfg, series, bars)

    recent_big_red = [a for a in bars[-40:] if a is not None and a["is_big_volume"] and a["direction"] == "red"]

    from scoring.technicals import compute_support_resistance
    pv_for_sr = [(s["date"], s["close"], s["volume"]) for s in series]
    sr = compute_support_resistance(cfg, pv_for_sr)
    origin = None
    if not sr["insufficient_data"] and sr["structural_support"]:
        origin = _analyze_trend_leg_origin(cfg, series, bars, sr["structural_support"]["date"])

    forecast = _forecast(ma10, ma50, ma200, overhead_zones, reversal, current_price)

    # ---- assemble the narrative text, mirroring a discretionary trader's read ----
    lines = []

    ma_bits = []
    for ma, tag in ((ma10, "short-term (10d)"), (ma50, "medium-term (50d)"), (ma200, "long-term (200d)")):
        if not ma.get("available"):
            continue
        ma_bits.append(f"the {tag} MA is turning {ma['direction']} ({ma['slope_pct']:+.1f}% over "
                        f"the last {tcfg['ma_slope_lookback_days']} sessions)")
    ma_line = "; ".join(ma_bits) + "." if ma_bits else ""
    if retake50:
        ma_line += f" Price just retook the 50d MA {retake50['days_ago']} day(s) ago ({retake50['date']})."
    lines.append(("Moving averages", ma_line))

    if recent_big_red:
        dist_dates = ", ".join(f"{b['date']} (closed {b['pos_label'].replace('_', ' ')} of the bar)" for b in recent_big_red[-3:])
        vol_line = (f"{len(recent_big_red)} big-volume red bar(s) in the last 40 sessions -- most recently "
                    f"{dist_dates}. Big red volume closing near the low is real distribution; big red volume "
                    f"that still closes near the high means buying absorbed the supply.")
    else:
        vol_line = "No big-volume red (distribution) bars in the last 40 sessions."
    lines.append(("Recent distribution", vol_line))

    if overhead_zones:
        zones_txt = "; ".join(f"${z['price_low']:.0f}-${z['price_high']:.0f} from the {z['date']} distribution bar"
                               for z in overhead_zones)
        supply_line = f"Overhead supply above current price: {zones_txt}."
    else:
        supply_line = "No significant overhead supply detected above current price in the lookback window."
    lines.append(("Overhead supply", supply_line))

    if origin:
        if origin["clean_advance"]:
            origin_line = (f"From the swing low on {origin['origin_date']}, the advance had no big-volume red bars "
                            f"({origin['red_count']} red bar(s) total, {origin['weak_red_count']} of them still "
                            f"closing near their high) -- no overhead supply was created on the way up.")
        else:
            origin_line = (f"From the swing low on {origin['origin_date']}, the advance included "
                            f"{origin['big_red_count']} big-volume red bar(s) out of {origin['red_count']} red "
                            f"bar(s) total -- some supply was created along the way.")
    else:
        origin_line = "Not enough swing-point history to identify a clean trend origin."
    lines.append(("Trend origin", origin_line))

    if reversal:
        reversal_line = (f"Likely stall/reversal point: {reversal['date']} ({reversal['days_ago']} day(s) ago) -- "
                          f"a big-volume red bar that closed near its low, the kind of bar that in hindsight tends "
                          f"to mark where an advance loses control to sellers.")
    else:
        reversal_line = "No big-volume red bar closing near its low in the last 60 sessions -- no clear stall point yet."
    lines.append(("Trend end / stall", reversal_line))

    lines.append(("Outlook", forecast["text"]))

    return {
        "available": True,
        "as_of_date": dates[-1],
        "current_price": current_price,
        "ma10": ma10, "ma50": ma50, "ma200": ma200,
        "retake_50d": retake50, "retake_short": retake10,
        "overhead_zones": overhead_zones,
        "trend_origin": origin,
        "reversal_bar": reversal,
        "forecast": forecast,
        "sections": lines,
    }
