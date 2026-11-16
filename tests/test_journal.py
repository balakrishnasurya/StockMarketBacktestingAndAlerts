"""
test_journal.py — Offline trade-journal tests.

Proves:
  (a) TP1/TP2 and SL outcomes are detected from candles
  (b) same-candle SL/TP ambiguity is resolved conservatively (SL first)
  (c) EOD profit/loss zone is computed separately
  (d) short setups mirror long setups
  (e) pending scan records do not overwrite settled outcomes
  (f) a 3-candle setup is recovered from the strategy state machine
"""
import sys
from datetime import date, datetime
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from config import IST
import journal

DAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=IST)


def bar(hh, mm, o, h, l, c):
    return {
        "timestamp": pd.Timestamp(f"{DAY} {hh:02d}:{mm:02d}:00", tz=IST),
        "open": o, "high": h, "low": l, "close": c, "volume": 1000,
    }


def long_session(tp2=True):
    rows = [
        bar(9, 15, 100.0, 100.4, 99.8, 100.1),
        bar(9, 30, 100.1, 100.4, 99.9, 100.0),
        bar(10, 0, 100.0, 100.5, 99.5, 100.2),
    ]
    if tp2:
        rows += [
            bar(10, 5, 100.2, 101.6, 100.2, 101.4),
            bar(10, 10, 101.4, 103.1, 101.2, 103.0),
            bar(10, 15, 103.0, 103.2, 102.9, 103.1),
            bar(15, 20, 102.6, 102.8, 102.4, 102.5),
        ]
    else:
        rows += [
            bar(10, 5, 99.8, 99.9, 98.9, 99.0),
            bar(15, 20, 99.1, 99.2, 99.0, 99.1),
        ]
    return pd.DataFrame(rows)


BASE = {
    "date": DAY.isoformat(),
    "symbol": "TEST",
    "strategy": "three_candle_bullish",
    "side": "BUY",
    "entry_time": pd.Timestamp(f"{DAY} 10:00", tz=IST).isoformat(),
    "entry_price": 100.0,
    "stop_loss": 99.0,
    "tp1": 101.5,
    "tp2": 103.0,
}


def test_targets():
    rec = journal.evaluate_entry(dict(BASE), long_session(), now=NOW)
    assert rec["outcome"] == "TP2_HIT", rec["outcome"]
    assert rec["tp1_hit"] and rec["tp2_hit"] and not rec["sl_hit"]
    assert rec["gross_pnl"] > 0
    assert rec["eod_zone"] == "PROFIT"
    assert rec["settled"] is True
    print("✅ TP1/TP2 outcome detected")


def test_stop_loss():
    rec = journal.evaluate_entry(dict(BASE), long_session(tp2=False), now=NOW)
    assert rec["outcome"] == "SL_HIT", rec["outcome"]
    assert rec["sl_hit"] and not rec["tp1_hit"]
    assert rec["gross_pnl"] < 0
    assert rec["eod_zone"] == "LOSS"
    print("✅ SL outcome detected")


def test_same_candle_ambiguity():
    bars = pd.DataFrame([
        bar(10, 0, 100.0, 101.6, 98.9, 100.0),
        bar(15, 20, 100.0, 100.2, 99.9, 100.0),
    ])
    rec = journal.evaluate_entry(dict(BASE), bars, now=NOW)
    assert rec["outcome"] == "SL_HIT"
    assert rec["ambiguous"] is True
    print("✅ Same-candle SL/TP tie resolved conservatively")


def test_short_side():
    rec = {
        "date": DAY.isoformat(), "symbol": "TEST", "strategy": "three_candle_bullish",
        "side": "SELL", "entry_time": pd.Timestamp(f"{DAY} 10:00", tz=IST).isoformat(),
        "entry_price": 100.0, "stop_loss": 101.0, "tp1": 98.5, "tp2": 97.0,
    }
    bars = pd.DataFrame([
        bar(10, 0, 100.0, 100.5, 99.5, 100.2),
        bar(10, 5, 99.0, 99.2, 98.4, 98.6),
        bar(10, 10, 98.6, 98.8, 96.9, 97.0),
        bar(15, 20, 97.4, 97.6, 97.2, 97.5),
    ])
    out = journal.evaluate_entry(rec, bars, now=NOW)
    assert out["outcome"] == "TP2_HIT", out["outcome"]
    assert out["gross_pnl"] > 0 and out["eod_zone"] == "PROFIT"
    print("✅ Short-side outcome mirrors long-side")


def test_merge_preserves_settled():
    settled = {**BASE, "settled": True, "status": "CLOSED",
               "outcome": "SL_HIT", "gross_pnl": -250.0, "net_pnl": -300.0}
    pending = {**BASE, "settled": False, "status": "PENDING",
               "outcome": "PENDING", "source": "scan"}
    entries, _, _ = journal.upsert_entries([settled], [])
    entries, _, _ = journal.upsert_entries([pending], entries)
    assert entries[0]["outcome"] == "SL_HIT"
    assert entries[0]["settled"] is True
    print("✅ Pending scan record cannot wipe a settled result")


def test_state_machine_recovery():
    prices = [
        (9, 30, 100.0, 101.0, 99.8, 100.5),
        (9, 35, 100.6, 102.0, 100.4, 101.5),
        (9, 40, 101.6, 103.0, 101.4, 102.5),
        (9, 45, 102.5, 102.8, 101.8, 102.0),
        (9, 50, 102.0, 103.2, 101.9, 103.0),
        (9, 55, 103.0, 104.7, 102.9, 104.5),
        (15, 20, 104.4, 104.6, 104.2, 104.3),
    ]
    df = pd.DataFrame([bar(*p) for p in prices])
    found = journal.build_setups_for_date(
        df, "TEST", "three_candle_bullish", DAY, "minutes", 5
    )
    assert len(found) == 1, found
    assert found[0]["side"] == "BUY"
    assert abs(found[0]["entry_price"] - 102.8) < 0.01
    out = journal.evaluate_entry(found[0], df, now=NOW)
    assert out["outcome"] in ("TP1_HIT", "TP1_TRAIL", "TP2_HIT")
    print("✅ 3-Candle setup recovered and evaluated")


if __name__ == "__main__":
    print("=" * 60)
    print("JOURNAL TESTS")
    print("=" * 60)
    test_targets()
    test_stop_loss()
    test_same_candle_ambiguity()
    test_short_side()
    test_merge_preserves_settled()
    test_state_machine_recovery()
    print("\nALL JOURNAL TESTS PASSED ✅")
