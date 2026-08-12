-- Micron Monitor database schema (Postgres / Supabase)
-- Design principle: observations are immutable, sourced, append-only.
-- Scores are always derived from observations and can be recomputed.

CREATE TABLE IF NOT EXISTS observations (
    id              SERIAL PRIMARY KEY,
    category        TEXT NOT NULL,      -- hbm_demand | dram_pricing | gross_margins | customer_capex | valuation | price | general
    metric_key      TEXT,               -- e.g. mu_gross_margin_pct, msft_capex_usd; NULL for qualitative/news items
    company         TEXT,               -- ticker this observation is about, if applicable
    value           DOUBLE PRECISION,   -- numeric value if this is a numeric observation
    unit            TEXT,               -- USD, pct, ratio, etc.
    text_excerpt    TEXT,               -- headline / quote / guidance text
    obs_date        TEXT NOT NULL,      -- date the observation was published/reported (ISO date)
    period_end      TEXT,               -- fiscal period end this data point refers to, if applicable
    source_name     TEXT NOT NULL,      -- e.g. "SEC EDGAR 10-Q", "Google News: Reuters"
    source_url      TEXT,
    source_type     TEXT NOT NULL,      -- FACT | MANAGEMENT_GUIDANCE | ANALYST_ESTIMATE | INDUSTRY_ESTIMATE | NEWS_REPORT | INFERENCE
    confidence      TEXT NOT NULL,      -- HIGH | MEDIUM | LOW
    fetched_at      TEXT NOT NULL,      -- when our collector pulled this (ISO datetime)
    dedup_key       TEXT                -- used to avoid re-inserting the same observation
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_dedup ON observations(dedup_key);
CREATE INDEX IF NOT EXISTS idx_observations_category_date ON observations(category, obs_date);
CREATE INDEX IF NOT EXISTS idx_observations_metric ON observations(metric_key, period_end);

-- Normalized time series derived from FACT-tier observations (e.g. one row
-- per company per quarter for gross margin %). Kept separate from raw
-- observations so scorers can query clean series without re-parsing XBRL.
CREATE TABLE IF NOT EXISTS metrics (
    id              SERIAL PRIMARY KEY,
    metric_key      TEXT NOT NULL,      -- e.g. gross_margin_pct, revenue_usd, capex_usd
    company         TEXT NOT NULL,
    period_end      TEXT NOT NULL,      -- fiscal quarter end date
    period_label    TEXT,               -- e.g. "FY2026 Q2"
    value           DOUBLE PRECISION NOT NULL,
    derived_from    TEXT,               -- free text: formula or source observation ids
    source_observation_id INTEGER,
    FOREIGN KEY (source_observation_id) REFERENCES observations(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_unique ON metrics(metric_key, company, period_end);

CREATE TABLE IF NOT EXISTS component_scores (
    id              SERIAL PRIMARY KEY,
    component       TEXT NOT NULL,      -- hbm_demand | dram_pricing | gross_margins | customer_capex | valuation
    as_of_date      TEXT NOT NULL,
    score           DOUBLE PRECISION,   -- 0-100, NULL if insufficient data
    insufficient_data INTEGER NOT NULL DEFAULT 0,
    confidence      TEXT,               -- HIGH | MEDIUM | LOW | NULL
    rationale       TEXT,               -- human-readable explanation of why this score
    evidence_ids    TEXT,               -- JSON array of observation ids used
    trend           TEXT                -- up | stable | down | NULL (vs prior reading)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_component_scores_unique ON component_scores(component, as_of_date);

CREATE TABLE IF NOT EXISTS overall_scores (
    id                  SERIAL PRIMARY KEY,
    as_of_date          TEXT NOT NULL UNIQUE,
    fundamental_score   DOUBLE PRECISION,  -- weighted avg of hbm/dram/margins/capex (renormalized if any missing)
    valuation_score      DOUBLE PRECISION, -- price attractiveness score
    overall_score       DOUBLE PRECISION,  -- for trend charting; blends fundamental+valuation per weights
    signal              TEXT,           -- BUYING_OPPORTUNITY | NEUTRAL_WAIT | RISK_REDUCE
    confidence          TEXT,           -- HIGH | MEDIUM | LOW
    missing_components  TEXT,           -- JSON array of components excluded for insufficient data
    explanation          TEXT           -- narrative: why the signal is what it is
);

CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    triggered_at    TEXT NOT NULL,
    severity        TEXT NOT NULL,      -- GREEN | RED
    category        TEXT NOT NULL,
    message         TEXT NOT NULL,
    related_ids     TEXT,               -- JSON array of observation/score ids
    acknowledged    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config_kv (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
