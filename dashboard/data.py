"""Read-only query helpers for the dashboard. The dashboard never computes a
score — it only formats what scoring.engine already wrote to the database.
"""
import json
from datetime import date, timedelta

from db.init_db import get_conn


def latest_overall_score(conn):
    row = conn.execute("SELECT * FROM overall_scores ORDER BY as_of_date DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def overall_score_history(conn, limit=180):
    rows = conn.execute(
        "SELECT as_of_date, fundamental_score, valuation_score, signal, confidence "
        "FROM overall_scores ORDER BY as_of_date ASC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def score_delta_vs(history, days_ago, tolerance_days=2):
    """Compare the latest fundamental_score in `history` (as returned by
    overall_score_history, ascending by date) against the reading closest to
    `days_ago` days before it. Returns None if there's no reading within
    `tolerance_days` of that target — i.e. not enough history yet, rather
    than guessing from whatever's available regardless of how stale it is.
    """
    if len(history) < 2:
        return None
    latest = history[-1]
    if latest["fundamental_score"] is None:
        return None
    latest_date = date.fromisoformat(latest["as_of_date"])
    target_date = latest_date - timedelta(days=days_ago)

    best, best_diff = None, None
    for h in history[:-1]:
        if h["fundamental_score"] is None:
            continue
        d = date.fromisoformat(h["as_of_date"])
        diff = abs((d - target_date).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = h, diff
    if best is None:
        return None
    return {
        "delta": latest["fundamental_score"] - best["fundamental_score"],
        "reference_date": best["as_of_date"],
        "reference_score": best["fundamental_score"],
        "latest_score": latest["fundamental_score"],
    }


def latest_component_scores(conn):
    rows = conn.execute(
        """
        SELECT cs.* FROM component_scores cs
        INNER JOIN (
            SELECT component, MAX(as_of_date) as max_date FROM component_scores GROUP BY component
        ) latest ON cs.component = latest.component AND cs.as_of_date = latest.max_date
        """
    ).fetchall()
    return {r["component"]: dict(r) for r in rows}


def component_score_history(conn, component, limit=180):
    rows = conn.execute(
        "SELECT as_of_date, score FROM component_scores WHERE component=? AND score IS NOT NULL "
        "ORDER BY as_of_date ASC LIMIT ?", (component, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_metric(conn, metric_key, company):
    row = conn.execute(
        "SELECT period_end, value FROM metrics WHERE metric_key=? AND company=? ORDER BY period_end DESC LIMIT 1",
        (metric_key, company),
    ).fetchone()
    return dict(row) if row else None


def price_history(conn, company, limit=365):
    rows = conn.execute(
        "SELECT period_end as obs_date, value FROM metrics WHERE metric_key='price_usd' AND company=? "
        "ORDER BY period_end ASC LIMIT ?", (company, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_alerts(conn, limit=25):
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY triggered_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def recent_observations(conn, category, limit=15):
    rows = conn.execute(
        "SELECT * FROM observations WHERE category=? ORDER BY obs_date DESC, id DESC LIMIT ?",
        (category, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def evidence_for(conn, ids_json, limit=8):
    ids = json.loads(ids_json) if ids_json else []
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids[:limit]))
    rows = conn.execute(
        f"SELECT * FROM observations WHERE id IN ({placeholders}) ORDER BY obs_date DESC",
        ids[:limit],
    ).fetchall()
    return [dict(r) for r in rows]
