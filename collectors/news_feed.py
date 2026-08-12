"""News/qualitative collector via Google News RSS.

This is the raw material for the News/Earnings Intelligence feed and the
qualitative side of the HBM Demand and DRAM Pricing scores (which have no
free structured data feed — see architecture notes). Every item is stored
as a NEWS_REPORT observation: a headline is evidence, not a fact about the
world, and the scoring engine must never treat it as more than that.

Google News RSS requires no API key. Confidence is bumped from LOW to
MEDIUM only when the publisher is on a short allow-list of primary
wire services / specialist trade press, since those are closer to the
"credible industry data" tier the user asked to be weighted higher than
random financial sites.
"""
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime

import requests

from config.loader import load_config
from db.init_db import get_conn
from db.store import insert_observation, make_dedup_key

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

HIGHER_CONFIDENCE_PUBLISHERS = {
    "reuters", "bloomberg", "financial times", "wall street journal", "wsj",
    "trendforce", "digitimes", "nikkei", "counterpoint research", "cnbc",
    "barron's", "investor's business daily", "the information", "sec.gov",
}


def _fetch_query(query, window_days):
    url = RSS_URL.format(query=requests.utils.quote(f"{query} when:{window_days}d"))
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def collect_category(conn, cfg, category, queries, window_days):
    count = 0
    for query in queries:
        try:
            root = _fetch_query(query, window_days)
        except Exception as e:
            print(f"[news_feed]   ERROR fetching '{query}': {e}")
            continue
        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pubdate_raw = item.findtext("pubDate")
            source_el = item.find("source")
            publisher = source_el.text if source_el is not None else "Unknown"
            try:
                obs_date = parsedate_to_datetime(pubdate_raw).date().isoformat()
            except Exception:
                obs_date = date.today().isoformat()

            confidence = "MEDIUM" if publisher and publisher.lower() in HIGHER_CONFIDENCE_PUBLISHERS else "LOW"
            dedup_key = make_dedup_key("news", category, title, obs_date)
            obs_id = insert_observation(
                conn, category=category,
                source_name=f"Google News: {publisher}",
                source_type="NEWS_REPORT", confidence=confidence,
                obs_date=obs_date, dedup_key=dedup_key,
                text_excerpt=title, source_url=link,
            )
            if obs_id:
                count += 1
        time.sleep(0.3)
    return count


def run():
    cfg = load_config()
    conn = get_conn()
    lookback = cfg["lookback_days"]
    for category, queries in cfg["news_queries"].items():
        window = lookback.get(category, lookback.get("news_general", 30))
        print(f"[news_feed] collecting category '{category}' ({len(queries)} queries, {window}d window)...")
        n = collect_category(conn, cfg, category, queries, window)
        conn.commit()
        print(f"[news_feed]   {n} new items")
    conn.close()
    print("[news_feed] done.")


if __name__ == "__main__":
    run()
