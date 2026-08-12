# Micron Monitor

A fundamentally-driven monitoring dashboard for MU (Micron Technology). It does
**not** predict the stock price. It tracks four fundamental drivers — HBM
demand, DRAM pricing, Micron gross margins, and customer AI capex — plus
MU's price/valuation, and produces a decision-support signal:

🟢 BUYING OPPORTUNITY · 🟡 NEUTRAL/WAIT · 🔴 RISK/REDUCE

Every number traces back to a source with a date, a link, and a confidence
level. See `ARCHITECTURE.md` (the design doc from the planning phase) for the
full data-source and scoring rationale.

Storage is Postgres via [Supabase](https://supabase.com) (free tier) — not
local SQLite — specifically so the dashboard can run on a free host
(Streamlit Community Cloud) without losing its daily score history on every
restart.

## Setup (one-time)

1. **Create a free Supabase project** at supabase.com. In the dashboard:
   Project Settings -> Database -> Connection string -> URI. Copy it.
2. **Local environment:**

```bash
cd /Users/markchern/Desktop/micron-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env, paste your connection string as DATABASE_URL
python3 db/init_db.py
```

## Run locally

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

## Deploying (free): Streamlit Community Cloud + Supabase + GitHub Actions

This is the genuinely free path — three free services, each doing the one
thing it's good at: Streamlit Cloud hosts the dashboard, Supabase holds the
data persistently, GitHub Actions runs the daily refresh on a schedule
(Streamlit Cloud itself has no cron/scheduler and no durable local disk, so
neither the hosting nor the scheduling can live there alone).

1. **Supabase**: already set up if you did the one-time setup above. Same
   project — the deployed app and your local runs share the same database.
2. **GitHub Actions**: already configured at `.github/workflows/refresh.yml`
   (runs daily at ~7am ET, plus a manual "Run workflow" button in the
   Actions tab). Add your connection string as a repo secret: GitHub repo ->
   Settings -> Secrets and variables -> Actions -> New repository secret ->
   name it `DATABASE_URL`.
3. **Streamlit Community Cloud**: streamlit.io -> New app -> pick this repo
   -> main file path `dashboard/app.py`. In the app's Settings -> Secrets,
   add:
   ```
   DATABASE_URL = "postgresql://postgres:...your connection string..."
   ```
4. First deploy starts with an empty database if you haven't run
   `scripts.refresh` locally yet — click "Run full refresh now" in the
   sidebar once, or just wait for the next GitHub Actions run.

## Deploying (paid, ~$5-7/mo): Render or Railway

Only worth it if you want everything on one always-on host instead of three
separate free services. Since persistence now lives in Supabase rather than
local disk, neither platform needs a paid disk add-on anymore — just a
regular web service.

**Render** (`render.yaml` is already set up as a Blueprint): render.com ->
New -> Blueprint -> select this repo. Set `DATABASE_URL` in the Render
dashboard's Environment tab (not stored in the repo). `render.yaml` sets
`MICRON_MONITOR_ENABLE_SCHEDULER=1`, which runs the daily refresh *inside
the same process* (`scripts/background_scheduler.py`) — you can skip the
GitHub Actions workflow with this path if you'd rather not depend on it.

**Railway**: same idea, no config file needed — create a service from this
repo, set the start command to the `streamlit run ...` line from
`render.yaml`, and add both `DATABASE_URL` and
`MICRON_MONITOR_ENABLE_SCHEDULER=1` as env vars.

## Daily automated refresh, purely local (optional, no cloud at all)

If you'd rather not deploy anywhere and just want the dashboard on your own
Mac with a local daily refresh:

```bash
bash scripts/install_schedule.sh   # writes a launchd job (does not activate it)
launchctl load ~/Library/LaunchAgents/com.micronmonitor.refresh.plist   # activates: runs daily at 7am
```

To undo: `launchctl unload ~/Library/LaunchAgents/com.micronmonitor.refresh.plist`

This still points at the same Supabase database (via your local `.env`), so
it works fine alongside or instead of GitHub Actions.

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
db/           schema.sql (Postgres), init_db.py (psycopg2 + sqlite3-compat wrapper), store.py
config/       config.yaml (weights/thresholds/queries), loader.py
scripts/      refresh.py (run everything), install_schedule.sh (launchd, purely local),
              background_scheduler.py (in-process daily job, for the Render/Railway path)
.github/workflows/refresh.yml   GitHub Actions cron (the free-path scheduler)
render.yaml   Render Blueprint (optional paid path)
.env.example  Template for DATABASE_URL — copy to .env, never commit .env
```
