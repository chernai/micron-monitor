"""SEC EDGAR XBRL collector.

Pulls quarterly financial facts directly from company filings via
data.sec.gov's companyconcept API (free, no key, requires a descriptive
User-Agent). This is the highest-confidence data source in the system:
every number here traces back to an actual 10-Q/10-K.

Handles the XBRL quirk where a filing reports both a single-quarter value
and a year-to-date cumulative value for the same period end: we keep only
entries whose (start, end) duration is ~1 quarter (80-100 days) as the
quarterly series, and ~1 year (355-375 days) as the annual series. Q4 is
then derived as FY total minus the sum of Q1-Q3 when all are available.
"""
import time
from datetime import datetime, date

import requests

from config.loader import load_config
from db.init_db import get_conn
from db.store import insert_observation, upsert_metric, make_dedup_key

BASE = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"

# Candidate XBRL tags per logical metric, tried in order until one has data.
TAG_CANDIDATES = {
    "revenue_usd": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "cost_of_revenue_usd": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "gross_profit_usd": ["GrossProfit"],
    "operating_income_usd": ["OperatingIncomeLoss"],
    "eps_diluted_usd": ["EarningsPerShareDiluted"],
    "rnd_usd": ["ResearchAndDevelopmentExpense"],
    "capex_usd": ["PaymentsToAcquirePropertyPlantAndEquipment",
                  "PaymentsForCapitalImprovements",
                  "PaymentsToAcquireProductiveAssets"],
    "ocf_usd": ["NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "cash_usd": ["CashAndCashEquivalentsAtCarryingValue"],
}

INSTANT_METRICS = {"cash_usd"}  # balance-sheet items: point-in-time, no start date


def _headers(cfg):
    return {"User-Agent": cfg["sec_user_agent"]}


def _fetch_tag(cik, tag, cfg):
    url = BASE.format(cik=cik, tag=tag)
    resp = requests.get(url, headers=_headers(cfg), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _days_between(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _extract_series(payload, instant=False):
    """Return (quarterly_entries, annual_entries) deduped to one entry per
    period, preferring the most recently filed value on restatement."""
    usd_units = payload.get("units", {}).get("USD") or payload.get("units", {}).get("USD/shares")
    if not usd_units:
        return [], []

    by_period = {}  # (start,end) or end -> best entry
    for e in usd_units:
        if e.get("form") not in ("10-Q", "10-K"):
            continue
        end = e["end"]
        if instant:
            key = end
            duration = None
        else:
            start = e.get("start")
            if not start:
                continue
            duration = _days_between(start, end)
            key = (start, end)
        existing = by_period.get(key)
        if existing is None or e.get("filed", "") >= existing.get("filed", ""):
            e = dict(e)
            e["_duration"] = duration
            by_period[key] = e

    if instant:
        return list(by_period.values()), []

    quarterly = [e for e in by_period.values() if 80 <= e["_duration"] <= 100]
    annual = [e for e in by_period.values() if 355 <= e["_duration"] <= 375]
    return quarterly, annual


def _filing_url(cik, accn):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"


def collect_company(conn, cfg, ticker, cik):
    raw = {}  # metric_key -> {period_end: value}
    raw_filed = {}  # metric_key -> {period_end: filed_date} -- when each value became publicly known
    for metric_key, tags in TAG_CANDIDATES.items():
        instant = metric_key in INSTANT_METRICS
        # Companies sometimes switch XBRL tags over time (e.g. "Revenues" ->
        # the ASC 606 contract-revenue tag around 2018-2019). Merge across
        # ALL candidate tags rather than stopping at the first one with any
        # data, or we silently fall back to years-stale numbers.
        quarterly_by_period = {}
        annual_by_period = {}
        for tag in tags:
            payload = _fetch_tag(cik, tag, cfg)
            time.sleep(0.15)  # be polite to SEC's rate limits
            if not payload:
                continue
            q, a = _extract_series(payload, instant=instant)
            for e in q:
                key = (e.get("start"), e["end"]) if not instant else e["end"]
                existing = quarterly_by_period.get(key)
                if existing is None or e.get("filed", "") >= existing.get("filed", ""):
                    quarterly_by_period[key] = e
            for e in a:
                key = (e.get("start"), e["end"])
                existing = annual_by_period.get(key)
                if existing is None or e.get("filed", "") >= existing.get("filed", ""):
                    annual_by_period[key] = e
        quarterly = list(quarterly_by_period.values())
        annual = list(annual_by_period.values())
        if not quarterly and not annual:
            continue
        series = {}
        filed_dates = {}
        entries = quarterly if not instant else quarterly
        for e in entries:
            period_end = e["end"]
            value = e["val"]
            obs_date = e.get("filed", period_end)
            unit = "USD_per_share" if metric_key == "eps_diluted_usd" else "USD"
            dedup_key = make_dedup_key("sec", ticker, metric_key, period_end, e.get("accn"))
            obs_id = insert_observation(
                conn, category="gross_margins" if ticker == cfg["subject_ticker"] else "customer_capex",
                source_name=f"SEC EDGAR {e.get('form')} ({e.get('accn')})",
                source_type="FACT", confidence="HIGH", obs_date=obs_date,
                dedup_key=dedup_key, metric_key=metric_key, company=ticker,
                value=value, unit=unit, period_end=period_end,
                source_url=_filing_url(cik, e.get("accn", "")),
            )
            upsert_metric(conn, metric_key, ticker, period_end, value,
                           period_label=f"{e.get('fy')} {e.get('fp')}",
                           source_observation_id=obs_id)
            series[period_end] = value
            filed_dates[period_end] = obs_date
        raw[metric_key] = series
        raw_filed[metric_key] = filed_dates

        # Also derive Q4 for annual filers: FY total - sum(Q1..Q3) of same fiscal year
        if annual:
            for a in annual:
                fy_end = a["end"]
                fy_val = a["val"]
                # find quarterly entries in the 12 months ending fy_end
                same_year_q = {e["end"]: e["val"] for e in quarterly
                                if e["end"] <= fy_end and _days_between(e["end"][:4] + "-01-01", e["end"]) < 400}
                # crude match: take the 3 quarterly ends that fall within ~270 days before fy_end
                candidates = sorted([e for e in quarterly if e["end"] < fy_end],
                                     key=lambda e: e["end"])[-3:]
                if len(candidates) == 3:
                    q_sum = sum(c["val"] for c in candidates)
                    q4_val = fy_val - q_sum
                    q4_filed = a.get("filed", fy_end)
                    dedup_key = make_dedup_key("sec-derived-q4", ticker, metric_key, fy_end)
                    obs_id = insert_observation(
                        conn, category="gross_margins" if ticker == cfg["subject_ticker"] else "customer_capex",
                        source_name=f"Derived: FY10-K total minus Q1-Q3 ({a.get('accn')})",
                        source_type="FACT", confidence="HIGH", obs_date=q4_filed,
                        dedup_key=dedup_key, metric_key=metric_key, company=ticker,
                        value=q4_val, unit="USD" if metric_key != "eps_diluted_usd" else "USD_per_share",
                        period_end=fy_end,
                        source_url=_filing_url(cik, a.get("accn", "")),
                        text_excerpt="Derived Q4 value (FY 10-K total minus sum of 3 preceding fiscal quarters)",
                    )
                    upsert_metric(conn, metric_key, ticker, fy_end, q4_val,
                                  period_label=f"{a.get('fy')} Q4 (derived)",
                                  derived_from="FY - (Q1+Q2+Q3)",
                                  source_observation_id=obs_id)
                    raw[metric_key][fy_end] = q4_val
                    raw_filed[metric_key][fy_end] = q4_filed

    # Derived ratios for the subject company (gross margin %). obs_date is
    # the later of the two inputs' actual filing dates -- NOT today's date
    # -- so a point-in-time-correct historical backfill can't "see" this
    # ratio before it was actually derivable from public filings.
    if ticker == cfg["subject_ticker"] and "revenue_usd" in raw:
        rev = raw["revenue_usd"]
        gp = raw.get("gross_profit_usd", {})
        for period_end, revenue_val in rev.items():
            if period_end in gp and revenue_val:
                margin = gp[period_end] / revenue_val * 100
                filed = max(raw_filed["revenue_usd"][period_end], raw_filed["gross_profit_usd"][period_end])
                dedup_key = make_dedup_key("derived", ticker, "gross_margin_pct", period_end)
                obs_id = insert_observation(
                    conn, category="gross_margins",
                    source_name="Derived: GrossProfit / Revenue (SEC EDGAR)",
                    source_type="FACT", confidence="HIGH", obs_date=filed,
                    dedup_key=dedup_key, metric_key="gross_margin_pct", company=ticker,
                    value=round(margin, 2), unit="pct", period_end=period_end,
                )
                upsert_metric(conn, "gross_margin_pct", ticker, period_end, round(margin, 2),
                              derived_from="gross_profit_usd / revenue_usd",
                              source_observation_id=obs_id)
        ocf = raw.get("ocf_usd", {})
        capex = raw.get("capex_usd", {})
        for period_end, ocf_val in ocf.items():
            if period_end in capex:
                fcf = ocf_val - capex[period_end]
                filed = max(raw_filed["ocf_usd"][period_end], raw_filed["capex_usd"][period_end])
                dedup_key = make_dedup_key("derived", ticker, "fcf_usd", period_end)
                obs_id = insert_observation(
                    conn, category="gross_margins",
                    source_name="Derived: Operating Cash Flow - Capex (SEC EDGAR)",
                    source_type="FACT", confidence="HIGH", obs_date=filed,
                    dedup_key=dedup_key, metric_key="fcf_usd", company=ticker,
                    value=fcf, unit="USD", period_end=period_end,
                )
                upsert_metric(conn, "fcf_usd", ticker, period_end, fcf,
                              derived_from="ocf_usd - capex_usd",
                              source_observation_id=obs_id)

    return raw


def run():
    cfg = load_config()
    conn = get_conn()
    universe = {cfg["subject_ticker"]: cfg["subject_cik"]}
    for ticker, info in cfg["capex_universe"].items():
        universe[ticker] = info["cik"]

    for ticker, cik in universe.items():
        print(f"[sec_edgar] collecting {ticker} (CIK {cik})...")
        try:
            collect_company(conn, cfg, ticker, cik)
            conn.commit()
        except Exception as e:
            print(f"[sec_edgar]   ERROR for {ticker}: {e}")
    conn.close()
    print("[sec_edgar] done.")


if __name__ == "__main__":
    run()
