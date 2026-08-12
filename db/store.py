"""Shared helpers for writing observations/metrics. All collectors use these
so dedup, source-type validation, and confidence rules stay consistent in
one place instead of being re-implemented per collector.
"""
import hashlib
from datetime import datetime, timezone

from db.init_db import get_conn

VALID_SOURCE_TYPES = {
    "FACT",
    "MANAGEMENT_GUIDANCE",
    "ANALYST_ESTIMATE",
    "INDUSTRY_ESTIMATE",
    "NEWS_REPORT",
    "INFERENCE",
}
VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_dedup_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def insert_observation(
    conn,
    category,
    source_name,
    source_type,
    confidence,
    obs_date,
    dedup_key,
    metric_key=None,
    company=None,
    value=None,
    unit=None,
    text_excerpt=None,
    period_end=None,
    source_url=None,
):
    """Insert an observation. Returns the row id, or None if it was a
    duplicate (dedup_key already present) — duplicates are silently skipped,
    not errors, since collectors run repeatedly over overlapping windows.
    """
    assert source_type in VALID_SOURCE_TYPES, f"bad source_type {source_type}"
    assert confidence in VALID_CONFIDENCE, f"bad confidence {confidence}"
    try:
        cur = conn.execute(
            """
            INSERT INTO observations
                (category, metric_key, company, value, unit, text_excerpt,
                 obs_date, period_end, source_name, source_url, source_type,
                 confidence, fetched_at, dedup_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                category, metric_key, company, value, unit, text_excerpt,
                obs_date, period_end, source_name, source_url, source_type,
                confidence, _now_iso(), dedup_key,
            ),
        )
        return cur.lastrowid
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            return None
        raise


def upsert_metric(conn, metric_key, company, period_end, value, period_label=None,
                   derived_from=None, source_observation_id=None):
    conn.execute(
        """
        INSERT INTO metrics (metric_key, company, period_end, period_label, value,
                              derived_from, source_observation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(metric_key, company, period_end)
        DO UPDATE SET value=excluded.value, period_label=excluded.period_label,
                       derived_from=excluded.derived_from,
                       source_observation_id=excluded.source_observation_id
        """,
        (metric_key, company, period_end, period_label, value, derived_from,
         source_observation_id),
    )
