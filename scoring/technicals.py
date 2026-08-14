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
    current_price = series[-1][1]
    current_date = date.fromisoformat(series[-1][0])
    near_cutoff = (current_date - timedelta(days=near_days)).isoformat()
    major_cutoff = (current_date - timedelta(days=major_days)).isoformat()

    near_highs = [h for h in highs if h[0] >= near_cutoff]
    major_highs = [h for h in highs if h[0] >= major_cutoff]
    near_lows = [l for l in lows if l[0] >= near_cutoff]
    major_lows = [l for l in lows if l[0] >= major_cutoff]

    above_near_highs = [h for h in near_highs if h[1] > current_price]
    near_term_resistance = min(above_near_highs, key=lambda h: h[1]) if above_near_highs else (
        near_highs[-1] if near_highs else None)
    major_resistance = max(major_highs, key=lambda h: h[1]) if major_highs else None

    below_near_lows = [l for l in near_lows if l[1] < current_price]
    near_term_support = max(below_near_lows, key=lambda l: l[1]) if below_near_lows else (
        near_lows[-1] if near_lows else None)
    structural_support = min(major_lows, key=lambda l: l[1]) if major_lows else None

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
            f"+/-{k} trading days of itself, not hand-drawn trendlines. Near-term = within the last "
            f"{near_days} days; major/structural = within the last {major_days} days. Macro floor = "
            f"lowest close in the full available price history (~{len(series)} trading days) — not "
            f"necessarily an all-time low, just the floor of our data window."
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


def compute_all(cfg, conn, ticker=None):
    ticker = ticker or cfg["subject_ticker"]
    series = get_price_volume_series(conn, ticker)
    return {
        "series_length": len(series),
        "volume_regime": compute_volume_regime(cfg, series),
        "volume_balance": compute_volume_balance(cfg, series),
        "support_resistance": compute_support_resistance(cfg, series),
    }
