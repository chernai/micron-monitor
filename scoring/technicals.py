"""Price-action / technicals for MU: volume regime, volume balance, and
support/resistance levels. Deliberately separate from scoring/engine.py —
this is short-term trading context (is now a technically opportune moment
to act), not a fundamental signal, and never feeds the BUYING/RISK signal.

Everything here is a deterministic function of price_usd/volume_shares
metrics already in the database, so unlike the fundamental scorers there's
nothing to persist — the dashboard calls these directly at render time.
"""
from datetime import date, timedelta


def get_price_volume_series(conn, company):
    """Ascending list of (date, close, volume) where both a close and a
    volume are known for that day."""
    rows = conn.execute(
        """
        SELECT p.period_end AS d, p.value AS close, v.value AS volume
        FROM metrics p
        JOIN metrics v ON v.metric_key = 'volume_shares' AND v.company = p.company AND v.period_end = p.period_end
        WHERE p.metric_key = 'price_usd' AND p.company = ?
        ORDER BY p.period_end ASC
        """,
        (company,),
    ).fetchall()
    return [(r["d"], r["close"], r["volume"]) for r in rows]


def compute_volume_regime(cfg, series):
    tcfg = cfg["technicals"]
    short_n, long_n = tcfg["volume_avg_short_days"], tcfg["volume_avg_long_days"]
    if len(series) < long_n:
        return {"insufficient_data": True,
                "rationale": f"Insufficient data: need >= {long_n} days of volume history, have {len(series)}."}

    volumes = [v for _, _, v in series]
    avg_short = sum(volumes[-short_n:]) / short_n
    avg_long = sum(volumes[-long_n:]) / long_n
    ratio = avg_short / avg_long if avg_long else None

    if ratio is None:
        regime = "UNKNOWN"
    elif ratio >= tcfg["volume_regime_above_pct"]:
        regime = "ABOVE_AVERAGE"
    elif ratio <= tcfg["volume_regime_below_pct"]:
        regime = "BELOW_AVERAGE"
    else:
        regime = "IN_LINE"

    return {
        "insufficient_data": False, "regime": regime,
        "avg_short": avg_short, "avg_long": avg_long, "ratio": ratio,
        "short_days": short_n, "long_days": long_n,
        "rationale": (
            f"{short_n}-day avg volume {avg_short:,.0f} vs {long_n}-day avg {avg_long:,.0f} "
            f"({ratio * 100:.0f}% of the longer average). A sustained breakout typically wants "
            f"above-average volume confirming institutional participation, not just price movement."
        ),
    }


def compute_volume_balance(cfg, series):
    window = cfg["technicals"]["volume_balance_window_days"]
    if len(series) < window + 1:
        return {"insufficient_data": True,
                "rationale": f"Insufficient data: need >= {window + 1} days, have {len(series)}."}

    recent = series[-(window + 1):]
    signed_intensity_sum = 0.0
    volume_sum = 0.0
    up_volume = 0.0
    down_volume = 0.0
    for i in range(1, len(recent)):
        prev_close = recent[i - 1][1]
        close = recent[i][1]
        vol = recent[i][2]
        if not prev_close:
            continue
        pct_change = (close - prev_close) / prev_close
        signed_intensity_sum += vol * pct_change
        volume_sum += vol
        if pct_change > 0:
            up_volume += vol
        elif pct_change < 0:
            down_volume += vol

    balance_pct = (signed_intensity_sum / volume_sum * 100) if volume_sum else None
    if balance_pct is None:
        direction = "UNKNOWN"
    elif balance_pct <= -0.05:
        direction = "NET_DISTRIBUTION"
    elif balance_pct >= 0.05:
        direction = "NET_ACCUMULATION"
    else:
        direction = "BALANCED"

    return {
        "insufficient_data": False, "balance_pct": balance_pct, "direction": direction,
        "window_days": window, "up_volume": up_volume, "down_volume": down_volume,
        "rationale": (
            f"{window}-day volume-weighted balance: {balance_pct:+.2f}% (sum of daily volume x %price-change, "
            f"normalized by total volume). Negative means down days moved more, on more volume, than up days "
            f"recovered — selling pressure outweighing buying pressure on the way down, not just plain "
            f"up-volume-vs-down-volume. Up-day volume {up_volume:,.0f} vs down-day volume {down_volume:,.0f}."
        ),
    }


def _find_swing_points(series, k):
    highs, lows = [], []
    n = len(series)
    for i in range(k, n - k):
        window = series[i - k:i + k + 1]
        closes = [w[1] for w in window]
        center_close = series[i][1]
        if center_close == max(closes):
            highs.append(series[i])
        if center_close == min(closes):
            lows.append(series[i])
    return highs, lows


def compute_support_resistance(cfg, series):
    tcfg = cfg["technicals"]
    k = tcfg["swing_point_k"]
    near_days, major_days = tcfg["near_term_window_days"], tcfg["major_window_days"]
    if len(series) < 2 * k + 1:
        return {"insufficient_data": True,
                "rationale": f"Insufficient data: need >= {2 * k + 1} days, have {len(series)}."}

    highs, lows = _find_swing_points(series, k)
    # A level's role (support vs. resistance) is decided dynamically by
    # where it sits relative to CURRENT price, not by whether it was
    # originally a swing high or a swing low. Pooling both together and
    # re-splitting on every call is what makes "polarity flip" fall out for
    # free: a former resistance level that price has since closed above
    # automatically reads as support next time this runs (and vice versa),
    # instead of staying permanently pinned to its original role.
    swings = highs + lows
    current_price = series[-1][1]
    current_date = date.fromisoformat(series[-1][0])
    near_cutoff = (current_date - timedelta(days=near_days)).isoformat()
    major_cutoff = (current_date - timedelta(days=major_days)).isoformat()

    near_swings = [p for p in swings if p[0] >= near_cutoff]
    major_swings = [p for p in swings if p[0] >= major_cutoff]

    def _closest_above(points):
        above = [p for p in points if p[1] > current_price]
        return min(above, key=lambda p: p[1]) if above else None

    def _closest_below(points):
        below = [p for p in points if p[1] < current_price]
        return max(below, key=lambda p: p[1]) if below else None

    def _farthest_above(points):
        above = [p for p in points if p[1] > current_price]
        return max(above, key=lambda p: p[1]) if above else None

    def _farthest_below(points):
        below = [p for p in points if p[1] < current_price]
        return min(below, key=lambda p: p[1]) if below else None

    # Near-term = the very next hurdle in each direction (closest).
    # Major/structural = the most extreme level still on the correct side of
    # price within the wider window (farthest) -- the "big" ceiling/floor.
    # Either can legitimately come back None: e.g. once price clears every
    # major-window high, there IS no major resistance left in that window.
    near_term_resistance = _closest_above(near_swings)
    near_term_support = _closest_below(near_swings)
    major_resistance = _farthest_above(major_swings)
    structural_support = _farthest_below(major_swings)

    macro_floor = min(series, key=lambda s: s[1])

    def _fmt(point):
        return {"date": point[0], "price": point[1]} if point else None

    return {
        "insufficient_data": False,
        "near_term_resistance": _fmt(near_term_resistance),
        "major_resistance": _fmt(major_resistance),
        "near_term_support": _fmt(near_term_support),
        "structural_support": _fmt(structural_support),
        "macro_floor": _fmt(macro_floor),
        "rationale": (
            f"Levels are swing highs/lows — a day whose close is the highest (or lowest) within "
            f"+/-{k} trading days of itself, not hand-drawn trendlines. Role is assigned dynamically by "
            f"current price, not by whether the swing was originally a high or a low: a broken resistance "
            f"becomes support, and a broken support becomes resistance. Near-term = the nearest such level "
            f"within the last {near_days} days; major/structural = the most extreme (farthest) such level "
            f"within the last {major_days} days -- either can be absent if price has cleared every level in "
            f"that window. Macro floor = lowest close in the full available price history "
            f"(~{len(series)} trading days) — not necessarily an all-time low, just the floor of our data "
            f"window."
        ),
    }


def moving_average_series(values, window):
    """Simple moving average, None until there's a full window's worth of
    data (no partial-window average pretending to be a real N-day MA)."""
    return [
        (sum(values[i + 1 - window:i + 1]) / window) if i + 1 >= window else None
        for i in range(len(values))
    ]


def rsi_series(closes, period=14):
    """Wilder's RSI (the textbook/standard smoothing, not a plain rolling
    average of gains/losses) -- the momentum oscillator most platforms mean
    by 'RSI'. None for the warm-up period before there's enough data."""
    n = len(closes)
    rsi = [None] * n
    if n <= period:
        return rsi

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return rsi


def ema_series(values, period):
    """Exponential moving average, seeded with a simple average of the
    first `period` values (the standard way to bootstrap an EMA). None
    before that seed point."""
    n = len(values)
    ema = [None] * n
    if n < period:
        return ema
    multiplier = 2 / (period + 1)
    ema[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def macd_series(closes, fast=12, slow=26, signal=9):
    """Standard MACD: EMA(fast) - EMA(slow), a signal line that's an EMA(9)
    of that, and their difference as the histogram. Returns three lists,
    all None-padded until enough data exists for that line."""
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]

    valid_start = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line = [None] * len(macd_line)
    if valid_start is not None:
        sig = ema_series(macd_line[valid_start:], signal)
        for i, v in enumerate(sig):
            signal_line[valid_start + i] = v

    histogram = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def _rsi_band_score(rsi):
    if rsi is None:
        return 55, "unknown"
    if rsi <= 30:
        return 90, "oversold"
    if rsi <= 45:
        return 75, "below neutral"
    if rsi <= 55:
        return 60, "neutral"
    if rsi <= 70:
        return 45, "above neutral"
    return 20, "overbought"


def compute_technical_timing_score(cfg, conn, ticker, as_of_date):
    """A real, but deliberately asymmetric, 3rd input to the signal (see
    scoring/engine.py): measures whether NOW looks like a technically good
    MOMENT to act, not whether the investment thesis is sound. Point-in-time
    correct -- computed from the price/volume series truncated to as_of_date,
    the same way the fundamental backfill works, so this can be recomputed
    for any historical date, not just today.
    """
    tcfg = cfg["technicals"]
    full_series = get_price_volume_series(conn, ticker)
    series = [s for s in full_series if s[0] <= as_of_date]
    min_days = tcfg["technical_score_min_days"]
    if len(series) < min_days:
        return {"score": None, "insufficient_data": True,
                "rationale": f"Insufficient data: need >= {min_days} days of price history as of "
                             f"{as_of_date} to compute RSI/MACD reliably, have {len(series)}."}

    closes = [s[1] for s in series]
    rsi = rsi_series(closes, 14)
    _, _, macd_hist = macd_series(closes)
    vr = compute_volume_regime(cfg, series)
    vb = compute_volume_balance(cfg, series)

    latest_rsi = rsi[-1]
    rsi_score, rsi_label = _rsi_band_score(latest_rsi)

    vr_score_map = {"ABOVE_AVERAGE": 70, "IN_LINE": 55, "BELOW_AVERAGE": 40, "UNKNOWN": 50}
    vr_score = 50 if vr["insufficient_data"] else vr_score_map.get(vr["regime"], 50)

    vb_score_map = {"NET_ACCUMULATION": 75, "BALANCED": 55, "NET_DISTRIBUTION": 30, "UNKNOWN": 50}
    vb_score = 50 if vb["insufficient_data"] else vb_score_map.get(vb["direction"], 50)

    latest_hist = macd_hist[-1] if macd_hist else None
    prev_hist = macd_hist[-2] if len(macd_hist) >= 2 else None
    if latest_hist is None:
        macd_score = 55
    elif prev_hist is None:
        macd_score = 60 if latest_hist >= 0 else 45
    else:
        rising = latest_hist > prev_hist
        if latest_hist >= 0:
            macd_score = 70 if rising else 55
        else:
            macd_score = 55 if rising else 30

    weights = tcfg["technical_score_weights"]
    score = round(
        rsi_score * weights["rsi"] + vr_score * weights["volume_regime"]
        + vb_score * weights["volume_balance"] + macd_score * weights["macd_momentum"], 1
    )

    rationale = (
        f"RSI {latest_rsi:.0f} ({rsi_label}) -> {rsi_score}, volume regime "
        f"{vr.get('regime', 'unknown').lower().replace('_', ' ')} -> {vr_score}, volume balance "
        f"{vb.get('direction', 'unknown').lower().replace('_', ' ')} -> {vb_score}, MACD momentum -> "
        f"{macd_score}. Weighted: {score}/100. This measures entry timing, not the thesis -- it can only "
        f"downgrade a fundamentals+valuation BUYING_OPPORTUNITY to NEUTRAL_WAIT when timing looks poor, "
        f"never create a buy signal out of weak fundamentals."
    )
    return {
        "score": score, "insufficient_data": False, "rationale": rationale,
        "detail": {"rsi": latest_rsi, "rsi_score": rsi_score, "volume_regime": vr.get("regime"),
                   "volume_regime_score": vr_score, "volume_balance": vb.get("direction"),
                   "volume_balance_score": vb_score, "macd_score": macd_score},
    }


def compute_all(cfg, conn, ticker=None):
    ticker = ticker or cfg["subject_ticker"]
    series = get_price_volume_series(conn, ticker)
    return {
        "series_length": len(series),
        "volume_regime": compute_volume_regime(cfg, series),
        "volume_balance": compute_volume_balance(cfg, series),
        "support_resistance": compute_support_resistance(cfg, series),
    }
