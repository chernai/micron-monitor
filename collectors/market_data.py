"""Market data collector: MU price/valuation via yfinance.

yfinance is an unofficial wrapper around Yahoo Finance. Price history comes
from a public endpoint that works reliably; valuation fields (forward P/E,
analyst targets, EBITDA) come from Yahoo's authenticated quoteSummary
endpoint, which yfinance accesses via session/cookie handling that can break
when Yahoo changes its auth scheme. Treat this whole collector as
MEDIUM confidence / best-effort, and fail soft (skip the field) rather than
crash the whole refresh if one field is missing.
"""
from datetime import date, datetime, timedelta

import yfinance as yf

from config.loader import load_config
from db.init_db import get_conn
from db.store import insert_observation, upsert_metric, make_dedup_key


def _pct_change(series, days_back):
    if len(series) < 2:
        return None
    target_date = series.index[-1] - timedelta(days=days_back)
    prior = series[series.index <= target_date]
    if prior.empty:
        return None
    start_val = prior.iloc[-1]
    end_val = series.iloc[-1]
    if not start_val:
        return None
    # pandas/numpy scalars (e.g. numpy.float64) aren't reliably adapted by
    # psycopg2 the way sqlite3 tolerated them — cast to native float so every
    # value written downstream is a plain Python type.
    return float((end_val / start_val - 1) * 100)


def collect(conn, cfg):
    ticker = cfg["subject_ticker"]
    t = yf.Ticker(ticker)
    today = date.today().isoformat()

    hist = t.history(period="1y", interval="1d")
    if hist.empty:
        print("[market_data] WARNING: no price history returned, skipping price metrics")
    else:
        close = hist["Close"]
        current_price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) > 1 else None
        daily_change_pct = ((current_price / prev_close - 1) * 100) if prev_close else None
        wk1 = _pct_change(close, 7)
        mo1 = _pct_change(close, 30)
        mo3 = _pct_change(close, 91)
        mo6 = _pct_change(close, 182)
        yr1 = _pct_change(close, 365)
        wk52_high = float(close.max())
        wk52_low = float(close.min())
        dist_from_high_pct = (current_price / wk52_high - 1) * 100
        ma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
        ma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
        daily_returns = close.pct_change().dropna()
        vol_30d_annualized = float(daily_returns.tail(30).std() * (252 ** 0.5) * 100) if len(daily_returns) >= 30 else None

        price_fields = {
            "price_usd": current_price,
            "price_change_1d_pct": daily_change_pct,
            "price_change_1w_pct": wk1,
            "price_change_1m_pct": mo1,
            "price_change_3m_pct": mo3,
            "price_change_6m_pct": mo6,
            "price_change_1y_pct": yr1,
            "price_52w_high_usd": wk52_high,
            "price_52w_low_usd": wk52_low,
            "price_dist_from_52w_high_pct": dist_from_high_pct,
            "price_ma50_usd": ma50,
            "price_ma200_usd": ma200,
            "price_volatility_30d_annualized_pct": vol_30d_annualized,
        }
        for metric_key, value in price_fields.items():
            if value is None:
                continue
            dedup_key = make_dedup_key("yfinance-price", ticker, metric_key, today)
            obs_id = insert_observation(
                conn, category="price",
                source_name="Yahoo Finance (chart API via yfinance)",
                source_type="FACT", confidence="MEDIUM", obs_date=today,
                dedup_key=dedup_key, metric_key=metric_key, company=ticker,
                value=round(value, 4), unit="USD" if "usd" in metric_key else "pct",
                source_url=f"https://finance.yahoo.com/quote/{ticker}",
            )
            upsert_metric(conn, metric_key, ticker, today, round(value, 4),
                           source_observation_id=obs_id)

        # Full historical daily closes, not just today's — we already fetched
        # a year of history to compute the stats above, so store all of it.
        # This is what lets the "price vs fundamentals" chart show more than
        # one point per day the collector happened to run.
        hist_count = 0
        for ts, close_val in close.items():
            day = ts.date().isoformat()
            if day == today:
                continue  # already inserted above with the full price_fields treatment
            dedup_key = make_dedup_key("yfinance-price-hist", ticker, "price_usd", day)
            obs_id = insert_observation(
                conn, category="price",
                source_name="Yahoo Finance (chart API via yfinance)",
                source_type="FACT", confidence="MEDIUM", obs_date=day,
                dedup_key=dedup_key, metric_key="price_usd", company=ticker,
                value=round(float(close_val), 4), unit="USD",
                source_url=f"https://finance.yahoo.com/quote/{ticker}",
            )
            upsert_metric(conn, "price_usd", ticker, day, round(float(close_val), 4),
                           source_observation_id=obs_id)
            hist_count += 1
        print(f"[market_data] backfilled {hist_count} historical daily closes")

        # Daily volume history — needed for the price-action/technicals
        # module (volume regime, 22-day volume balance). Same treatment as
        # price: store the whole fetched year, not just today.
        volume = hist["Volume"]
        vol_count = 0
        for ts, vol_val in volume.items():
            day = ts.date().isoformat()
            if not vol_val or vol_val != vol_val:  # NaN/zero guard
                continue
            dedup_key = make_dedup_key("yfinance-volume", ticker, "volume_shares", day)
            obs_id = insert_observation(
                conn, category="price",
                source_name="Yahoo Finance (chart API via yfinance)",
                source_type="FACT", confidence="MEDIUM", obs_date=day,
                dedup_key=dedup_key, metric_key="volume_shares", company=ticker,
                value=float(vol_val), unit="shares",
                source_url=f"https://finance.yahoo.com/quote/{ticker}",
            )
            upsert_metric(conn, "volume_shares", ticker, day, float(vol_val),
                           source_observation_id=obs_id)
            vol_count += 1
        print(f"[market_data] backfilled {vol_count} historical daily volumes")

        # Full historical daily open/high/low -- needed for the technical
        # narrative (bar range, where price closed within that range,
        # overhead supply from past high-volume down bars). Same treatment
        # as close/volume: store the whole fetched year, not just today.
        ohl_count = 0
        for ts, o_val, h_val, l_val in zip(hist.index, hist["Open"], hist["High"], hist["Low"]):
            day = ts.date().isoformat()
            if any(v != v for v in (o_val, h_val, l_val)):  # NaN guard
                continue
            for metric_key, val in (("price_open_usd", o_val), ("price_high_usd", h_val), ("price_low_usd", l_val)):
                dedup_key = make_dedup_key("yfinance-ohlc-hist", ticker, metric_key, day)
                obs_id = insert_observation(
                    conn, category="price",
                    source_name="Yahoo Finance (chart API via yfinance)",
                    source_type="FACT", confidence="MEDIUM", obs_date=day,
                    dedup_key=dedup_key, metric_key=metric_key, company=ticker,
                    value=round(float(val), 4), unit="USD",
                    source_url=f"https://finance.yahoo.com/quote/{ticker}",
                )
                upsert_metric(conn, metric_key, ticker, day, round(float(val), 4),
                               source_observation_id=obs_id)
            ohl_count += 1
        print(f"[market_data] backfilled {ohl_count} historical daily OHL (open/high/low)")

    # Valuation fields from .info (fragile — Yahoo's authenticated endpoint)
    try:
        info = t.info
    except Exception as e:
        print(f"[market_data] WARNING: could not fetch .info valuation fields: {e}")
        info = {}

    valuation_map = {
        "forward_pe": "forwardPE",
        "trailing_pe": "trailingPE",
        "forward_eps_usd": "forwardEps",
        "trailing_eps_usd": "trailingEps",
        "analyst_target_mean_usd": "targetMeanPrice",
        "analyst_target_median_usd": "targetMedianPrice",
        "num_analyst_opinions": "numberOfAnalystOpinions",
        "enterprise_value_usd": "enterpriseValue",
        "ebitda_usd": "ebitda",
        "market_cap_usd": "marketCap",
        "total_cash_usd": "totalCash",
        "total_debt_usd": "totalDebt",
        "free_cashflow_usd": "freeCashflow",
        "beta": "beta",
    }
    for metric_key, info_key in valuation_map.items():
        val = info.get(info_key)
        if val is None:
            continue
        dedup_key = make_dedup_key("yfinance-valuation", ticker, metric_key, today)
        obs_id = insert_observation(
            conn, category="valuation",
            source_name="Yahoo Finance (quoteSummary via yfinance)",
            source_type="ANALYST_ESTIMATE" if "analyst" in metric_key or metric_key in ("forward_pe", "forward_eps_usd") else "FACT",
            confidence="MEDIUM", obs_date=today,
            dedup_key=dedup_key, metric_key=metric_key, company=ticker,
            value=float(val), unit="ratio" if "pe" in metric_key or metric_key == "beta" else "USD",
            source_url=f"https://finance.yahoo.com/quote/{ticker}",
        )
        upsert_metric(conn, metric_key, ticker, today, float(val), source_observation_id=obs_id)

    # EV/EBITDA and Price/FCF, derived
    ev = info.get("enterpriseValue")
    ebitda = info.get("ebitda")
    if ev and ebitda:
        ev_ebitda = ev / ebitda
        dedup_key = make_dedup_key("derived-valuation", ticker, "ev_ebitda", today)
        obs_id = insert_observation(
            conn, category="valuation", source_name="Derived: EV / EBITDA",
            source_type="FACT", confidence="MEDIUM", obs_date=today,
            dedup_key=dedup_key, metric_key="ev_ebitda", company=ticker,
            value=round(ev_ebitda, 2), unit="ratio",
        )
        upsert_metric(conn, "ev_ebitda", ticker, today, round(ev_ebitda, 2), source_observation_id=obs_id)

    mcap = info.get("marketCap")
    fcf = info.get("freeCashflow")
    if mcap and fcf:
        price_fcf = mcap / fcf
        dedup_key = make_dedup_key("derived-valuation", ticker, "price_fcf", today)
        obs_id = insert_observation(
            conn, category="valuation", source_name="Derived: Market Cap / Free Cash Flow",
            source_type="FACT", confidence="MEDIUM", obs_date=today,
            dedup_key=dedup_key, metric_key="price_fcf", company=ticker,
            value=round(price_fcf, 2), unit="ratio",
        )
        upsert_metric(conn, "price_fcf", ticker, today, round(price_fcf, 2), source_observation_id=obs_id)


def run():
    cfg = load_config()
    conn = get_conn()
    print(f"[market_data] collecting {cfg['subject_ticker']}...")
    try:
        collect(conn, cfg)
        conn.commit()
    except Exception as e:
        print(f"[market_data] ERROR: {e}")
    conn.close()
    print("[market_data] done.")


if __name__ == "__main__":
    run()
