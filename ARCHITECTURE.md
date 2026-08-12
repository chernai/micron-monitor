# Architecture

## Flow

```
Collectors (Python, scheduled)          Storage (SQLite)              Dashboard
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
