"""Peer comparison collector: SK Hynix (SKHY, NASDAQ ADR) and Samsung
Electronics (SSNLF, OTC) -- context alongside Micron, NOT a parallel
BUYING/RISK signal. Neither has usable structured financial data via SEC
EDGAR (SK Hynix files bare 6-Ks with no XBRL financials as a foreign
private issuer; Samsung isn't SEC-registered at all), so unlike MU's
gross-margin/capex collector, this leans on yfinance's own fundamentals
fields -- real data, ultimately sourced from their Korean exchange filings,
but lower-confidence than a direct SEC filing citation and without the
years of quarterly depth SEC EDGAR gives us for MU. See ARCHITECTURE.md.
"""
from datetime import date

import yfinance as yf

from config.loader import load_config
from db.init_db import get_conn
from db.store import insert_observation, upsert_metric, make_dedup_key

FUNDAMENTALS_MAP = {
    "gross_margin_pct_snapshot": ("grossMargins", 100),
    "operating_margin_pct_snapshot": ("operatingMargins", 100),
    "revenue_growth_pct_snapshot": ("revenueGrowth", 100),
    "forward_pe": ("forwardPE", 1),
    "trailing_pe": ("trailingPE", 1),
}


def collect_peer(conn, ticker, name, today):
    t = yf.Ticker(ticker)

    hist = t.history(period="1y", interval="1d")
    if not hist.empty:
        close = hist["Close"]
        current_price = float(close.iloc[-1])
        dedup_key = make_dedup_key("peer-price", ticker, "price_usd", today)
        obs_id = insert_observation(
            conn, category="peer_comparison",
            source_name=f"Yahoo Finance (chart API via yfinance) — {name}",
            source_type="FACT", confidence="MEDIUM", obs_date=today,
            dedup_key=dedup_key, metric_key="price_usd", company=ticker,
            value=round(current_price, 4), unit="USD",
            source_url=f"https://finance.yahoo.com/quote/{ticker}",
        )
        upsert_metric(conn, "price_usd", ticker, today, round(current_price, 4),
                       source_observation_id=obs_id)

        hist_count = 0
        for ts, close_val in close.items():
            day = ts.date().isoformat()
            if day == today:
                continue
            dedup_key = make_dedup_key("peer-price-hist", ticker, "price_usd", day)
            obs_id = insert_observation(
                conn, category="peer_comparison",
                source_name=f"Yahoo Finance (chart API via yfinance) — {name}",
                source_type="FACT", confidence="MEDIUM", obs_date=day,
                dedup_key=dedup_key, metric_key="price_usd", company=ticker,
                value=round(float(close_val), 4), unit="USD",
                source_url=f"https://finance.yahoo.com/quote/{ticker}",
            )
            upsert_metric(conn, "price_usd", ticker, day, round(float(close_val), 4),
                           source_observation_id=obs_id)
            hist_count += 1
        print(f"[peer_data]   {ticker}: {hist_count} historical daily closes")

    # Current fundamentals snapshot -- honestly labeled as lower-confidence
    # than MU's SEC-filed numbers: it's real data, but aggregated by Yahoo
    # from Korean exchange filings rather than traceable to a specific
    # filing/accession number the way every MU number in this app is.
    try:
        info = t.info
    except Exception as e:
        print(f"[peer_data]   WARNING: could not fetch .info for {ticker}: {e}")
        info = {}

    for metric_key, (info_key, scale) in FUNDAMENTALS_MAP.items():
        val = info.get(info_key)
        if val is None:
            continue
        value = round(float(val) * scale, 4)
        dedup_key = make_dedup_key("peer-fundamentals", ticker, metric_key, today)
        obs_id = insert_observation(
            conn, category="peer_comparison",
            source_name=f"Yahoo Finance fundamentals — {name} (derived from Korean exchange "
                         f"filings via yfinance; no direct filing/accession-number citation available)",
            source_type="FACT", confidence="MEDIUM", obs_date=today,
            dedup_key=dedup_key, metric_key=metric_key, company=ticker,
            value=value, unit="pct" if scale == 100 else "ratio",
            source_url=f"https://finance.yahoo.com/quote/{ticker}",
        )
        upsert_metric(conn, metric_key, ticker, today, value, source_observation_id=obs_id)

    # Quarterly gross margin trend, where available (typically the last
    # 4-6 quarters -- much shallower than MU's multi-year SEC history).
    # obs_date is today (the date we observed/derived it), not a true filing
    # date -- yfinance doesn't expose when each quarter was actually filed,
    # so unlike MU's SEC data this is NOT point-in-time-correct and
    # shouldn't be used for historical backfilling.
    try:
        qf = t.quarterly_income_stmt
        if qf is not None and not qf.empty and "Total Revenue" in qf.index and "Gross Profit" in qf.index:
            for col in qf.columns:
                revenue = qf.loc["Total Revenue", col]
                gross_profit = qf.loc["Gross Profit", col]
                if revenue and gross_profit and revenue == revenue and gross_profit == gross_profit:  # NaN check
                    period_end = col.date().isoformat() if hasattr(col, "date") else str(col)
                    margin = round(float(gross_profit) / float(revenue) * 100, 2)
                    dedup_key = make_dedup_key("peer-quarterly-margin", ticker, period_end)
                    obs_id = insert_observation(
                        conn, category="peer_comparison",
                        source_name=f"Yahoo Finance quarterly financials — {name} (not point-in-time "
                                     f"correct — filing date unknown, do not use for historical backfill)",
                        source_type="FACT", confidence="LOW", obs_date=today,
                        dedup_key=dedup_key, metric_key="gross_margin_pct", company=ticker,
                        value=margin, unit="pct", period_end=period_end,
                        source_url=f"https://finance.yahoo.com/quote/{ticker}",
                    )
                    upsert_metric(conn, "gross_margin_pct", ticker, period_end, margin,
                                  derived_from="Gross Profit / Total Revenue (yfinance quarterly_income_stmt)",
                                  source_observation_id=obs_id)
    except Exception as e:
        print(f"[peer_data]   WARNING: could not fetch quarterly financials for {ticker}: {e}")


def run():
    cfg = load_config()
    conn = get_conn()
    today = date.today().isoformat()
    for ticker, info in cfg.get("peers", {}).items():
        print(f"[peer_data] collecting {ticker} ({info['name']})...")
        try:
            collect_peer(conn, ticker, info["name"], today)
            conn.commit()
        except Exception as e:
            print(f"[peer_data]   ERROR for {ticker}: {e}")
    conn.close()
    print("[peer_data] done.")


if __name__ == "__main__":
    run()
