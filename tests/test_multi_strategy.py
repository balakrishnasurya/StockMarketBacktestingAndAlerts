"""
test_multi_strategy.py — Offline tests for multi-strategy scanning and logging.

Proves:
  (a) one candle fetch evaluates every selected strategy
  (b) a multi-strategy scan fetches candles once per symbol
  (c) a multi-strategy sync fetches once and logs each setup with its strategy
  (d) the journal API records strategies separately in the standard format
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

import journal
from config import IST
from scanner import LAST_ALERTS_FILE, scan_symbol_multi, scan_watchlist_multi
from strategies import list_strategies

STRATS = [sid for sid, _ in list_strategies()]

SOLDIERS = [
    (100.0, 101.0, 99.8, 100.8, 1000),
    (100.8, 102.0, 100.6, 101.8, 1200),
    (101.8, 103.0, 101.6, 102.9, 1500),
    (102.9, 103.2, 102.2, 102.4, 900),
    (102.4, 103.4, 102.3, 103.3, 2000),
    (103.3, 103.5, 103.1, 103.4, 100),  # in-progress candle
]


def rows_to_df(rows, start="2024-06-03 09:30:00"):
    t0 = pd.Timestamp(start, tz=IST)
    return pd.DataFrame(
        [
            {
                "timestamp": t0 + pd.Timedelta(minutes=5 * i),
                "open": o, "high": h, "low": l, "close": c, "volume": v,
            }
            for i, (o, h, l, c, v) in enumerate(rows)
        ]
    )


def test_multi_scan_uses_one_fetch_for_all_strategies():
    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()

    df = rows_to_df(SOLDIERS)
    calls = {"hist": 0}

    def hist(*args, **kwargs):
        calls["hist"] += 1
        return df

    def intra(*args, **kwargs):
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    now = datetime(2024, 6, 3, 10, 27, tzinfo=IST)
    out = scan_symbol_multi(
        "MULTI_TEST",
        "NSE_EQ|TEST",
        STRATS,
        unit="minutes",
        interval=5,
        fetch_historical_fn=hist,
        fetch_intraday_fn=intra,
        now=now,
    )
    assert set(out) == set(STRATS)
    assert calls["hist"] == 1, "candles must be fetched once for every strategy"
    assert out["three_candle_bullish"]["status"] == "NEW"
    assert out["three_candle_bullish"]["side"] == "BUY"
    assert all(r["strategy"] == sid for sid, r in out.items())

    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()
    print("✅ multi-strategy scan fetches candles once")


def test_watchlist_multi_fetches_once_per_symbol():
    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()

    df = rows_to_df(SOLDIERS)
    calls = {"hist": 0}

    def hist(*args, **kwargs):
        calls["hist"] += 1
        return df

    def intra(*args, **kwargs):
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    symbols = {"AAA": "NSE_EQ|AAA", "BBB": "NSE_EQ|BBB"}
    out = scan_watchlist_multi(
        symbols,
        ["three_candle_bullish", "vwap_bullish", "h1_pyramid"],
        unit="minutes",
        interval=5,
        fetch_historical_fn=hist,
        fetch_intraday_fn=intra,
        now=datetime(2024, 6, 3, 10, 27, tzinfo=IST),
    )
    assert set(out) == {"three_candle_bullish", "vwap_bullish", "h1_pyramid"}
    assert all(len(rows) == 2 for rows in out.values())
    assert calls["hist"] == 2, "one candle fetch per symbol, not per strategy"
    assert out["three_candle_bullish"][0]["strategy"] == "three_candle_bullish"

    if LAST_ALERTS_FILE.exists():
        LAST_ALERTS_FILE.unlink()
    print("✅ watchlist multi-scan fetches once per symbol")


def test_multi_sync_uses_one_fetch_and_logs_by_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.json")
    df = rows_to_df(SOLDIERS)
    calls = {"hist": 0}

    def hist(*args, **kwargs):
        calls["hist"] += 1
        return df

    def intra(*args, **kwargs):
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    summary = journal.sync_journal_multi(
        {"MULTI_TEST": "NSE_EQ|TEST"},
        [date(2024, 6, 3)],
        STRATS,
        "minutes",
        5,
        fetch_historical_fn=hist,
        fetch_intraday_fn=intra,
        now=datetime(2024, 6, 4, 18, 0, tzinfo=IST),
    )
    assert calls["hist"] == 1, "multi-strategy sync must fetch candles once"
    assert summary["added"] >= 1

    entries = journal.load_journal()
    assert entries
    assert {e["strategy"] for e in entries} <= set(STRATS)
    assert any(e["strategy"] == "three_candle_bullish" for e in entries)
    assert all(e["date"] == "2024-06-03" for e in entries)
    assert summary["strategies"] == STRATS
    print("✅ multi-strategy sync fetches once and logs with strategy ids")


def test_record_multi_scan_alerts_keeps_standard_format(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.json")
    ts = datetime(2024, 6, 3, 9, 45, tzinfo=IST)
    results = {
        "vwap_bullish": [
            {
                "symbol": "MULTI_TEST",
                "status": "NEW",
                "side": "BUY",
                "entry_price": 100.0,
                "stop_loss": 99.0,
                "tp1": 101.0,
                "tp2": 102.0,
                "rr": "1:2",
                "candle_time": ts,
            }
        ],
    }
    added, _ = journal.record_multi_scan_alerts(results, ts, "minutes", 5)
    assert added == 1
    entry = journal.load_journal()[0]
    assert entry["strategy"] == "vwap_bullish"
    assert entry["strategy_name"]
    assert (entry["unit"], entry["interval"]) == ("minutes", 5)
    assert entry["id"].split("|")[2] == "vwap_bullish"
    print("✅ multi-strategy journal rows keep the standard format")


if __name__ == "__main__":
    print("=" * 60)
    print("MULTI-STRATEGY TESTS")
    print("=" * 60)
    test_multi_scan_uses_one_fetch_for_all_strategies()
    test_watchlist_multi_fetches_once_per_symbol()
    test_record_multi_scan_alerts_keeps_standard_format()
    print("\nALL MULTI-STRATEGY TESTS PASSED ✅")
