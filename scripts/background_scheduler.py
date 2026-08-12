"""In-process daily scheduler, for deployments (Render/Railway/etc.) where
there's one persistent web-service process but no separate cron/launchd
mechanism attached to it. Runs scripts.refresh.main() once per day at a
configured hour, in a background thread inside the same process that serves
the Streamlit app — so it shares the same persistent disk, no cross-service
coordination needed.

Guarded at module level so it only starts once per process even though
Streamlit re-executes app.py on every user interaction/page load.
"""
import threading
import time
from datetime import datetime

_started = False
_lock = threading.Lock()

REFRESH_HOUR_UTC = 12  # ~7am US Eastern; adjust to taste
CHECK_INTERVAL_SECONDS = 300


def _loop():
    last_run_date = None
    while True:
        now = datetime.utcnow()
        if now.hour == REFRESH_HOUR_UTC and now.date() != last_run_date:
            try:
                print(f"[background_scheduler] running daily refresh at {now.isoformat()}")
                from scripts import refresh
                refresh.main()
                last_run_date = now.date()
            except Exception as e:
                print(f"[background_scheduler] refresh failed: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_background_scheduler():
    """No-op unless MICRON_MONITOR_ENABLE_SCHEDULER=1 is set — kept opt-in so
    local runs (which typically use launchd or manual refresh instead) don't
    silently pick up an extra daily background job."""
    import os
    if os.environ.get("MICRON_MONITOR_ENABLE_SCHEDULER") != "1":
        return

    global _started
    with _lock:
        if _started:
            return
        _started = True
        t = threading.Thread(target=_loop, daemon=True, name="micron-monitor-scheduler")
        t.start()
        print("[background_scheduler] started")
