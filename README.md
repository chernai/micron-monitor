# Micron Monitor

A fundamentally-driven monitoring dashboard for MU (Micron Technology). It does
**not** predict the stock price. It tracks four fundamental drivers — HBM
demand, DRAM pricing, Micron gross margins, and customer AI capex — plus
MU's price/valuation, and produces a decision-support signal:

🟢 BUYING OPPORTUNITY · 🟡 NEUTRAL/WAIT · 🔴 RISK/REDUCE

Every number traces back to a source with a date, a link, and a confidence
level. See `ARCHITECTURE.md` (the design doc from the planning phase) for the
full data-source and scoring rationale.

## Setup (one-time)

```bash
cd /Users/markchern/Desktop/micron-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 db/init_db.py
```

## Run

**1. Collect data + compute scores** (takes ~90s; hits SEC EDGAR, Yahoo
Finance, and Google News live):

```bash
source .venv/bin/activate
python3 -m scripts.refresh
```

**2. Launch the dashboard:**

```bash
source .venv/bin/activate
streamlit run dashboard/app.py
```

Opens at http://localhost:8501. There's also a "Run full refresh now" button
in the sidebar so you don't need the terminal for routine use.

## Daily automated refresh (optional)

```bash
bash scripts/install_schedule.sh   # writes a launchd job (does not activate it)
launchctl load ~/Library/LaunchAgents/com.micronmonitor.refresh.plist   # activates: runs daily at 7am
```

To undo: `launchctl unload ~/Library/LaunchAgents/com.micronmonitor.refresh.plist`

## Configuration

Everything tunable lives in `config/config.yaml`: component weights, the
company universe tracked for capex, news search queries, lookback windows,
and signal thresholds. No code changes needed to adjust these.

## What's solid vs. best-effort

- **Gross Margins, Customer Capex, Valuation, price data**: built directly
  from SEC EDGAR XBRL filings and Yahoo Finance — deterministic, sourced,
  high/medium confidence.
- **HBM Demand, DRAM Pricing**: no free structured pricing feed exists
  (TrendForce/Omdia are paid, no public API). These two run on a transparent
  keyword rubric over recent news headlines — evidence-based but explicitly
  lower-confidence (labeled LOW/MEDIUM in the UI). If you get access to a
  paid DRAM/HBM pricing index, that's the natural upgrade path — see
  `ARCHITECTURE.md`.

## Project layout

```
collectors/   sec_edgar.py, market_data.py, news_feed.py — append-only, sourced
scoring/      rubric.py (point bands/keywords), engine.py (scores), alerts.py
dashboard/    app.py (Streamlit), data.py (read-only queries)
db/           schema.sql, init_db.py, store.py
config/       config.yaml (weights/thresholds/queries), loader.py
scripts/      refresh.py (run everything), install_schedule.sh (launchd)
data/         micron_monitor.db (SQLite, git-ignored)
```
