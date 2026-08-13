"""Run all collectors, then scoring, then alerts, in order.

Usage: python -m scripts.refresh
"""
import time

from db.init_db import init_db
from collectors import sec_edgar, market_data, news_feed, peer_data
from scoring import engine, alerts


def main():
    t0 = time.time()
    print("=== Micron Monitor: full refresh ===")
    init_db()

    print("\n--- SEC EDGAR (financials) ---")
    sec_edgar.run()

    print("\n--- Market data (price/valuation) ---")
    market_data.run()

    print("\n--- Peer data (SK Hynix, Samsung) ---")
    peer_data.run()

    print("\n--- News feed ---")
    news_feed.run()

    print("\n--- Scoring ---")
    result = engine.compute_and_store()
    print(f"Signal: {result['signal']} | Fundamental: {result['fundamental_score']} | "
          f"Valuation: {result['valuation_score']} | Confidence: {result['confidence']}")

    print("\n--- Alerts ---")
    alerts.run()

    print(f"\n=== Done in {time.time() - t0:.1f}s ===")


if __name__ == "__main__":
    main()
