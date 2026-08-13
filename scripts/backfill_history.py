"""One-time(ish) backfill: reconstruct component/overall scores for past
dates using data we already have (SEC filings going back years, price
history going back ~1 year, news observations spanning however far back the
collectors' lookback windows reached). This is NOT fabricated data — every
backfilled score is computed by the same scoring.engine rubric applied to
real, already-collected, point-in-time-correct data (see
scoring/engine.py's get_metric_series for the point-in-time logic).

Two honest limits, by design, not oversight:
  - HBM Demand / DRAM Pricing will show "insufficient data" for any
    backfilled date beyond how far back our one-time news collection
    reached (~90-120 days) — we have no way to know what news existed
    before we started collecting it.
  - Valuation is never backfilled (see scoring.engine.score_valuation) —
    we only ever capture today's forward P/E snapshot, never a history of
    it, and approximating with a different methodology would silently mix
    two incompatible approaches into one series.

Never overwrites a date that already has a real (live-collected) row —
only fills gaps. Run with: python3 -m scripts.backfill_history [--days N]
"""
import argparse
import sys
from datetime import date, timedelta

from db.init_db import get_conn
from scoring import engine

BACKFILL_NOTE = "[Backfilled from historical data — see scripts/backfill_history.py for methodology/limits] "


def existing_dates(conn):
    rows = conn.execute("SELECT as_of_date FROM overall_scores").fetchall()
    return {r["as_of_date"] for r in rows}


def mark_backfilled(conn, as_of_date):
    # The '%' LIKE-wildcard must be part of a bound parameter, not literal
    # SQL text -- psycopg2 uses Python %-style formatting to bind params, so
    # a bare '%' sitting in the SQL string itself (e.g. from `|| '%'`) gets
    # misread as a stray format specifier instead of a SQL wildcard.
    like_pattern = BACKFILL_NOTE + "%"
    conn.execute(
        "UPDATE overall_scores SET explanation = ? || explanation WHERE as_of_date = ? "
        "AND explanation NOT LIKE ?",
        (BACKFILL_NOTE, as_of_date, like_pattern),
    )
    conn.execute(
        "UPDATE component_scores SET rationale = ? || rationale WHERE as_of_date = ? "
        "AND rationale NOT LIKE ?",
        (BACKFILL_NOTE, as_of_date, like_pattern),
    )


def run(days_back):
    conn = get_conn()
    already = existing_dates(conn)
    conn.close()

    today = date.today()
    targets = [(today - timedelta(days=n)).isoformat() for n in range(days_back, 0, -1)]
    targets = [d for d in targets if d not in already]

    if not targets:
        print(f"Nothing to backfill — all dates in the last {days_back} days already have real data.")
        return

    print(f"Backfilling {len(targets)} date(s) from {targets[0]} to {targets[-1]}...")
    for i, d in enumerate(targets, 1):
        try:
            result = engine.compute_and_store(as_of_date=d)
            conn = get_conn()
            mark_backfilled(conn, d)
            conn.commit()
            conn.close()
            print(f"  [{i}/{len(targets)}] {d}: fundamental={result['fundamental_score']}, "
                  f"signal={result['signal']}")
        except Exception as e:
            print(f"  [{i}/{len(targets)}] {d}: ERROR {e}", file=sys.stderr)

    print("Backfill done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=380,
                         help="How many days back to backfill (default 380, matching ~1y price history)")
    args = parser.parse_args()
    run(args.days)
