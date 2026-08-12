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

## Daily automated refresh (local, optional)

```bash
bash scripts/install_schedule.sh   # writes a launchd job (does not activate it)
launchctl load ~/Library/LaunchAgents/com.micronmonitor.refresh.plist   # activates: runs daily at 7am
```

To undo: `launchctl unload ~/Library/LaunchAgents/com.micronmonitor.refresh.plist`

## Deploying (Render / Railway)

This is a single always-on web service, not a serverless app — Streamlit
needs a persistent process, and the SQLite database needs a persistent disk.
Don't use Vercel or Streamlit Community Cloud for this: neither gives you
both a long-running process *and* durable local disk storage, which this app
needs for its daily score history to actually accumulate.

**Render** (`render.yaml` is already set up as a Blueprint):

1. Push this repo to GitHub (already done if you're reading this from the repo).
2. On render.com: New -> Blueprint -> select this repo. Render reads `render.yaml` automatically.
3. It provisions one web service on a paid plan (persistent disks require a
   paid instance — see [Render's disk docs](https://render.com/docs/disks))
   with a 1GB disk mounted at `data/`, running `streamlit run dashboard/app.py`.
4. `MICRON_MONITOR_ENABLE_SCHEDULER=1` is set automatically in `render.yaml` —
   this turns on `scripts/background_scheduler.py`, which runs the full
   refresh once a day *inside the same process*, no separate cron job needed.
   (It's off by default locally so a plain `streamlit run` doesn't silently
   start a background job — see that file for the on/off switch.)

**Railway**: same idea, no separate config file needed — create a new
service from this repo, add a volume mounted at `data/`, set the start
command to the same `streamlit run ...` line from `render.yaml`, and add the
`MICRON_MONITOR_ENABLE_SCHEDULER=1` env var.

Either way, the first deploy starts with an empty database — click "Run full
refresh now" in the sidebar once to populate it (~90s), then the daily
background job keeps it current.

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
scripts/      refresh.py (run everything), install_schedule.sh (launchd, local),
              background_scheduler.py (in-process daily job, for hosted deploys)
data/         micron_monitor.db (SQLite, git-ignored)
render.yaml   Render Blueprint (web service + persistent disk)
```
