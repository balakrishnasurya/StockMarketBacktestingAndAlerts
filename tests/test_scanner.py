"""
test_scanner.py — Offline scanner tests with mocked data fetcher.
Proves:
  (a) In-progress last candle is dropped
  (b) Same signal not alerted twice
  (c) Too little history gives "insufficient data"
  (d) get_signal_series produces no look-ahead
  (e) Entry / stop / target levels are populated and persist across alerts
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
import numpy as np

from config import IST
from scanner import (
    _drop_in_progress_candle,
    scan_symbol,
    LAST_ALERTS_FILE,
)
from strategies import get_signal_series, load_strategy

STRATEGY_ID = "three_candle_bullish"


def _next_session(ts: pd.Timestamp) -> pd.Timestamp:
    """Roll an intraday timestamp to the next weekday's 09:15 open."""
    ts = (ts.normalize() + pd.Timedelta(days=1)).replace(hour=9, minute=15)
    while ts.weekday() >= 5:
        ts += pd.Timedelta(days=1)
    return ts


def make_candles(n: int, base_price: float = 100.0,
                 start: str = "2024-06-01 09:15:00") -> pd.DataFrame:
    """Generate n valid 5-minute candles, rolling into the next session when needed."""
    np.random.seed(42)
    rows = []
    p = base_price
    ts = pd.Timestamp(start, tz=IST)
    for i in range(n):
        if ts.hour > 15 or (ts.hour == 15 and ts.minute >= 30):
            ts = _next_session(ts)

        o = p
        c = p + np.random.uniform(-1.0, 1.2)
        h = max(o, c) + abs(np.random.uniform(0.0, 0.6))
        l = min(o, c) - abs(np.random.uniform(0.0, 0.6))
        rows.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": int(np.random.uniform(500, 2000)),
        })
        p = c
        ts = ts + pd.Timedelta(minutes=5)
    return pd.DataFrame(rows)


def rows_to_df(rows: list[tuple], start: str = "2024-06-03 10:00:00") -> pd.DataFrame:
    """Build a 5-minute DataFrame from (open, high, low, close, volume) tuples."""
    t0 = pd.Timestamp(start, tz=IST)
    return pd.DataFrame([
        {
            "timestamp": t0 + pd.Timedelta(minutes=5 * i),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        }
        for i, (o, h, l, c, v) in enumerate(rows)
    ])


def empty_intraday() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


# 3 green impulse candles, a red pullback candle (high 103.2 -> armed) and a
# breakout candle (high 103.4 -> BUY). The final row is an in-progress candle.
# Risk is ~1.07%, inside the engine's 2% cap, so the setup is tradable.
THREE_CANDLE_DAY = [
    (100.0, 101.0, 99.8, 100.8, 1000),
    (100.8, 102.0, 100.6, 101.8, 1200),
    (101.8, 103.0, 101.6, 102.9, 1500),
    (102.9, 103.2, 102.2, 102.4, 900),
    (102.4, 103.4, 102.3, 103.3, 2000),
    (103.3, 103.5, 103.1, 103.4, 100),  # dropped as in-progress
]


def test_drop_in_progress_candle_intraday():
    """(a) Intraday: last candle should be dropped."""
    df = make_candles(10)
    now = pd.Timestamp("2024-06-01 11:30:00", tz=IST)

    result = _drop_in_progress_candle(df, unit="minutes", interval=5, now=now)
    assert len(result) == len(df) - 1, \
        f"Expected {len(df) - 1} candles after drop, got {len(result)}"
    print(f"  Intraday: {len(df)} -> {len(result)} candles (last dropped)")
    print("✅ test_drop_in_progress_candle_intraday PASSED")


def test_drop_in_progress_candle_daily():
    """(a) Daily: today's candle dropped when market is open."""
    # 3 days of candles: Mon 06-03, Tue 06-04, Wed 06-05
    rows = []
    for i in range(3):
        ts = pd.Timestamp("2024-06-03", tz=IST) + pd.Timedelta(days=i)
        rows.append({
            "timestamp": ts, "open": 100, "high": 101,
            "low": 99, "close": 100, "volume": 1000,
        })
    df = pd.DataFrame(rows)

    now_open = datetime(2024, 6, 5, 10, 0, 0, tzinfo=IST)
    result_open = _drop_in_progress_candle(df, unit="days", interval=1, now=now_open)
    assert len(result_open) == 2, f"Expected 2 candles (today dropped), got {len(result_open)}"
    assert (result_open["timestamp"].dt.date < now_open.date()).all()

    now_closed = datetime(2024, 6, 5, 18, 0, 0, tzinfo=IST)
    result_closed = _drop_in_progress_candle(df, unit="days", interval=1, now=now_closed)
    assert len(result_closed) == 3, f"Expected 3 candles (market closed), got {len(result_closed)}"

    print(f"  Daily: 3 -> {len(result_open)} (market open), 3 -> {len(result_closed)} (market closed)")
    print("✅ test_drop_in_progress_candle_daily PASSED")


def test_insufficient_data():
    """(c) Too few candles should return 'insufficient_data'."""
    # Five candles, then the in-progress candle is dropped -> only 4 closed (< MIN_CANDLES 5)
    df_short = make_candles(5)

    def mock_hist(*args, **kwargs):
        return df_short

    def mock_intra(*args, **kwargs):
        return empty_intraday()

    now = datetime(2024, 6, 1, 16, 0, 0, tzinfo=IST)

    result = scan_symbol(
        symbol="TEST",
        instrument_key="NSE_EQ|TEST",
        strategy_id=STRATEGY_ID,
        unit="minutes",
        interval=5,
        fetch_historical_fn=mock_hist,
        fetch_intraday_fn=mock_intra,
        now=now,
    )

    assert result["status"] == "insufficient_data", \
        f"Expected 'insufficient_data', got '{result['status']}'"
    print(f"  Result: {result['status']} — {result['message']}")
    print("✅ test_insufficient_data PASSED")


def test_no_duplicate_alerts():
    """(b) Same signal should not be alerted twice."""
    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()

    df = rows_to_df(THREE_CANDLE_DAY)

    def mock_hist(*args, **kwargs):
        return df

    def mock_intra(*args, **kwargs):
        return empty_intraday()

    now = datetime(2024, 6, 3, 10, 27, 0, tzinfo=IST)

    params = dict(
        symbol="DEDUP_TEST",
        instrument_key="NSE_EQ|TEST",
        strategy_id=STRATEGY_ID,
        unit="minutes",
        interval=5,
        fetch_historical_fn=mock_hist,
        fetch_intraday_fn=mock_intra,
        now=now,
    )

    r1 = scan_symbol(**params)
    print(f"  First scan: status={r1['status']}, side={r1['side']}")
    assert r1["status"] == "NEW", f"Expected NEW, got {r1['status']}"

    r2 = scan_symbol(**params)
    print(f"  Second scan: status={r2['status']}, side={r2['side']}")
    assert r2["status"] == "already_alerted", \
        f"Expected 'already_alerted' on second scan, got '{r2['status']}'"

    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()
    print("✅ test_no_duplicate_alerts PASSED")


def test_no_lookahead():
    """
    (d) get_signal_series produces no look-ahead.
    The signal at row i must be identical when computed on data truncated at i.
    """
    df = make_candles(80)
    mod = load_strategy(STRATEGY_ID)

    signals_full = get_signal_series(df, STRATEGY_ID)

    mismatches = 0
    for i in [49, 55, 60, 70, 79]:
        truncated = df.iloc[: i + 1].copy()
        sig_truncated = mod.signal(truncated)

        if signals_full[i] != sig_truncated:
            print(f"  MISMATCH at row {i}: full={signals_full[i]}, truncated={sig_truncated}")
            mismatches += 1
        else:
            print(f"  Row {i}: {signals_full[i]} == {sig_truncated} ✓")

    assert mismatches == 0, f"{mismatches} look-ahead mismatches found!"
    print("✅ test_no_lookahead PASSED")


def test_trade_levels_populated():
    """Verify entry_price, stop_loss, tp1, tp2, rr are returned and persist."""
    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()

    df = rows_to_df(THREE_CANDLE_DAY)

    def mock_hist(*args, **kwargs):
        return df

    def mock_intra(*args, **kwargs):
        return empty_intraday()

    now = datetime(2024, 6, 3, 10, 27, 0, tzinfo=IST)

    params = dict(
        symbol="SETUP_TEST",
        instrument_key="NSE_EQ|TEST",
        strategy_id=STRATEGY_ID,
        unit="minutes",
        interval=5,
        fetch_historical_fn=mock_hist,
        fetch_intraday_fn=mock_intra,
        now=now,
    )

    r1 = scan_symbol(**params)

    assert r1["status"] == "NEW", f"Expected NEW, got {r1['status']}"
    assert r1["side"] == "BUY"
    assert r1["entry_price"] is not None and r1["entry_price"] > 0
    assert r1["stop_loss"] is not None and r1["stop_loss"] < r1["entry_price"]
    assert r1["tp1"] is not None and r1["tp1"] > r1["entry_price"]
    assert r1["rr"] is not None
    print(f"  First scan (NEW): Entry={r1['entry_price']}, SL={r1['stop_loss']}, "
          f"TP1={r1['tp1']}, RR={r1['rr']}")

    r2 = scan_symbol(**params)

    assert r2["status"] == "already_alerted", f"Expected already_alerted, got {r2['status']}"
    assert r2["side"] == "BUY"
    assert r2["entry_price"] == r1["entry_price"], "Expected entry_price to persist on already_alerted"
    assert r2["stop_loss"] == r1["stop_loss"], "Expected stop_loss to persist on already_alerted"
    assert r2["tp1"] == r1["tp1"], "Expected tp1 to persist on already_alerted"
    assert r2["rr"] == r1["rr"], "Expected rr to persist on already_alerted"
    print(f"  Second scan (already_alerted): Entry={r2['entry_price']}, SL={r2['stop_loss']}, "
          f"TP1={r2['tp1']}, RR={r2['rr']}")

    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()
    print("✅ test_trade_levels_populated PASSED")


if __name__ == "__main__":
    print("=" * 60)
    print("SCANNER TESTS (offline, mocked data)")
    print("=" * 60)

    test_drop_in_progress_candle_intraday()
    test_drop_in_progress_candle_daily()
    test_insufficient_data()
    test_no_duplicate_alerts()
    test_no_lookahead()
    test_trade_levels_populated()

    print("\n" + "=" * 60)
    print("ALL SCANNER TESTS PASSED ✅")
    print("=" * 60)
