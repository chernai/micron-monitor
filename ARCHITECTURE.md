# Architecture

## Flow

```
Collectors (Python)                     Storage (Postgres/Supabase)   Dashboard
sec_edgar.py (SEC EDGAR XBRL)  ---raw facts--->  observations   ---reads--->  Streamlit
market_data.py (yfinance)      ---raw facts--->  metrics (series)             dashboard
news_feed.py (Google News RSS) ---headlines--->  observations                 (read-only)
                                                        |
                                                        v
                                                scoring/engine.py
                                          (rubric, config-driven weights)
                                                        |
                                                        v
                                component_scores / overall_scores / alerts
```

Collectors only ever **append** immutable, sourced observations. The scoring
engine is a separate deterministic pass reading observations + config
(weights/thresholds) and writing scores. The dashboard only reads.

**Storage backend**: Postgres, hosted on Supabase's free tier — not local
SQLite. This was a deliberate pivot (see below) so the app can run on a free
host (Streamlit Community Cloud) without losing accumulated score history on
every restart, since Streamlit Cloud gives you neither a durable local disk
nor a cron scheduler. `db/init_db.py` wraps a psycopg2 connection in a thin
`PgConnection` class that mimics sqlite3's `conn.execute(...)` convenience
API (translating `?` placeholders to `%s`, returning dict-like rows via
`RealDictCursor`), so collectors/scoring/dashboard code — all originally
written against sqlite3 — didn't need per-call-site rewrites. The only files
with real Postgres-specific logic are `db/init_db.py` and `db/store.py`
(which uses `INSERT ... ON CONFLICT DO NOTHING RETURNING id` instead of
sqlite's `cursor.lastrowid`, since Postgres has no equivalent and duplicate
inserts are the expected common case as collectors re-scan overlapping
windows on every run).

**Scheduling**: no single mechanism — pick the one matching your deploy path.
On the free path (Streamlit Community Cloud), a GitHub Actions cron workflow
(`.github/workflows/refresh.yml`) runs `scripts/refresh.py` daily against the
same Supabase database. On the paid always-on path (Render/Railway), an
in-process background thread (`scripts/background_scheduler.py`, opt-in via
`MICRON_MONITOR_ENABLE_SCHEDULER=1`) does the same job without needing a
separate scheduler. Purely local runs can still use `launchd`
(`scripts/install_schedule.sh`) — all three point at the same database, so
they're interchangeable, not mutually exclusive.

## Data sources

| Source | Provides | Frequency | Cost/Key | Confidence |
|---|---|---|---|---|
| SEC EDGAR XBRL (`data.sec.gov`) | MU + MSFT/AMZN/GOOGL/META/ORCL/NVDA/AMD revenue, gross profit, opex, EPS, capex, R&D, cash | Per filing (quarterly) | Free, no key | FACT |
| SEC EDGAR full-text / 8-K exhibits | MD&A guidance language, earnings press releases | Per filing | Free, no key | MANAGEMENT_GUIDANCE |
| Yahoo Finance chart API | Price history, 52-wk high/low, moving averages | ~15-min delayed | Free, no key, unofficial | FACT (market data) |
| yfinance `.info` | Forward/trailing P/E, forward EPS, analyst targets | Periodic | Free, unofficial, fragile (Yahoo auth changes break it periodically) | ANALYST_ESTIMATE, MEDIUM |
| Google News RSS | Headline/source/date/link per query | Real-time | Free, no key | NEWS_REPORT, LOW (MEDIUM for allow-listed wire/trade press) |
| TrendForce/Omdia DRAM-HBM pricing indices | The gold-standard numeric pricing series | N/A | **Paid, no public API — the one real gap** | N/A unless subscribed |
| Full earnings-call transcripts | Verbatim Q&A | N/A | Paid (AlphaSense/Finnhub/Quartr) or free via press releases | MANAGEMENT_GUIDANCE (partial) |

## Direct observation vs. derived vs. inference

- **Direct FACT**: revenue, gross profit, capex, EPS, price, 52-wk range — cited to filing/accession number.
- **Derived FACT**: gross margin % (GrossProfit/Revenue), QoQ/YoY deltas, EV/EBITDA — computed from two FACTs.
- **Hyperscaler AI Capex Trend**: derived from real capex FACTs + a small news-based guidance-language nudge.
- **HBM Demand & DRAM Pricing**: INFERENCE — a keyword rubric over news/guidance text within a lookback window, weighted by source tier. Lowest-confidence part of the system by design; upgradeable if a paid pricing feed is added.

## Peer comparison (SK Hynix, Samsung) — context, not a signal

Added after checking: SK Hynix recently listed NASDAQ ADRs (`SKHY`) but
files only bare 6-Ks as a foreign private issuer, with no structured XBRL
financials (verified live against `data.sec.gov` — its companyfacts feed
has just 5 registration-fee tags, nothing financial). Samsung (`SSNLF`,
OTC) isn't SEC-registered at all. Neither can support the SEC-EDGAR-based
scoring built for MU, so `collectors/peer_data.py` falls back to
yfinance's own fundamentals fields (gross margin, forward P/E, revenue
growth) — real data, ultimately sourced from their Korean exchange
filings, but lower-confidence (no citable accession number) and shallower
history (a handful of quarters via `quarterly_income_stmt`, not point-in-time
correct) than MU's SEC data. Deliberately shown as a comparison table only
— no BUYING/RISK signal is computed for either, consistent with "reliable
data over more features."

## Price action / technicals (MU only) — informational, not a signal

`scoring/technicals.py` computes, live at render time from already-stored
price/volume data (nothing persisted, nothing backfillable further back
than the ~1y of price history collected):
- **Volume regime**: short-window vs long-window average volume (config:
  `technicals.volume_avg_short_days`/`long_days`).
- **Volume balance**: sum of `volume x %price-change` over a trailing
  window, normalized by total volume — negative means down days moved
  more, on more volume, than up days recovered (net distribution), not
  just a plain up-volume-vs-down-volume split.
- **Support/resistance**: swing-point detection (a day is a swing high/low
  if it's the extreme within +/-k trading days of itself) at two
  horizons — near-term and major/structural — plus the macro floor (lowest
  close in the full available window). Deliberately not hand-drawn
  trendlines or Fibonacci levels — reproducible from the same price series
  every time.

This is short-term trading context, not fundamentals — it never feeds the
BUYING/RISK signal, consistent with "not a price predictor."

## Missing data & confidence

No interpolation, no invented numbers. A component with too few/stale
observations reports "Insufficient data" and is excluded from the weighted
overall score (weights renormalize across what's available). Every component
score carries HIGH/MEDIUM/LOW confidence. The signal won't flip on LOW
confidence alone.

## Scoring (see `scoring/rubric.py` for exact bands)

- **Gross Margins**: point bands for margin level + sequential (QoQ) change + YoY change, all from SEC FACT data.
- **Customer Capex**: YoY capex growth per company mapped to a -2..+2 signal, averaged across the 7-company universe, plus a small news-guidance nudge.
- **Valuation**: forward P/E band + distance-from-52-week-high bonus. Weighted low (10%) and never the sole driver of the signal.
- **HBM Demand / DRAM Pricing**: strong/weak keyword counts over recent news, weighted by source confidence tier, normalized by sqrt(evidence count) to avoid saturating at the ceiling on high news volume alone.
- **Overall signal**: fundamental_score <= 45 -> RISK_REDUCE; fundamental_score >= 65 AND valuation_score >= 45 -> BUYING_OPPORTUNITY; otherwise NEUTRAL_WAIT. (Config: `signal_thresholds` in `config/config.yaml`.)

## Database schema

See `db/schema.sql`. Tables: `observations` (immutable sourced facts/quotes),
`metrics` (normalized time series), `component_scores`, `overall_scores`,
`alerts`, `config_kv`.

## Known limitations (v1)

- DRAM/HBM scoring is a headline-keyword heuristic, not verified analysis — treat as directional evidence, shown transparently in the dashboard's evidence feed.
- yfinance valuation fields depend on an unofficial Yahoo endpoint that can break; the collector fails soft (skips the field) rather than crashing.
- No historical backtesting yet (spec section 14) — would need years of point-in-time data this system hasn't accumulated yet; revisit once enough daily history exists.
- Supabase's free tier pauses a project after a week of no database activity — the daily GitHub Actions refresh should keep it awake, but if you skip both that and local runs for a stretch, the first request after a pause takes longer while it wakes back up.
