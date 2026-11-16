"""
test_api_tracker.py — Offline test for API call tracker and metrics.
No network calls.
"""
import sys
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from api_tracker import ApiTracker, ApiCallRecord


def test_tracker_record_and_metrics():
    tracker = ApiTracker()

    # 1. Record live call
    tracker.record_call(
        call_type="Historical",
        symbol="RELIANCE",
        instrument_key="NSE_EQ|INE002A01018",
        endpoint="/v3/historical-candle/...",
        unit_interval="days/1",
        range_str="2024-01-01 to 2024-01-30",
        status_code=200,
        latency_ms=180.5,
        candles_count=20,
        status="SUCCESS",
        details="Chunk 1/1",
        df=pd.DataFrame({"timestamp": [1, 2], "close": [100, 105]}),
    )

    # 2. Record cache hit
    tracker.record_call(
        call_type="Cache Hit",
        symbol="RELIANCE",
        instrument_key="",
        endpoint="data/RELIANCE_days1.csv",
        unit_interval="days/1",
        range_str="2024-01-01 to 2024-01-30",
        status_code=200,
        latency_ms=2.0,
        candles_count=20,
        status="CACHED",
        details="Loaded from disk",
    )

    # 3. Record 429 rate limit
    tracker.record_call(
        call_type="Historical",
        symbol="INFY",
        instrument_key="NSE_EQ|INE009A01021",
        endpoint="/v3/historical-candle/...",
        unit_interval="minutes/15",
        range_str="Today",
        status_code=429,
        latency_ms=50.0,
        candles_count=0,
        status="429_RETRY",
        details="Rate limit hit, retried",
    )

    m = tracker.get_metrics()
    print("Tracker metrics:", m)

    assert m["total_events"] == 3
    assert m["total_live_calls"] == 2
    assert m["cache_hits"] == 1
    assert m["cache_savings_pct"] == 33.3
    assert m["rate_limits_429"] == 1
    assert m["total_candles"] == 40
    assert "RELIANCE" in m["symbols"]
    assert "INFY" in m["symbols"]

    # Check DataFrame export
    df = tracker.get_calls_df()
    assert len(df) == 3
    assert "HTTP Status" in df.columns
    assert "Latency (ms)" in df.columns

    # Check token sanitization
    sanitized = tracker.sanitize_text("https://api.upstox.com?test=1")
    assert "Bearer" not in sanitized

    # Check clear
    tracker.clear()
    assert len(tracker.get_calls()) == 0
    assert len(tracker.get_latest_datasets()) == 0

    print("✅ test_tracker_record_and_metrics PASSED")


if __name__ == "__main__":
    print("=" * 60)
    print("API TRACKER TESTS (offline)")
    print("=" * 60)
    test_tracker_record_and_metrics()
    print("=" * 60)
    print("ALL API TRACKER TESTS PASSED ✅")
    print("=" * 60)
