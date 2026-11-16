"""
test_engine.py — Offline backtest engine tests using synthetic 5-minute candles.
No Upstox API calls.

The app-level engine delegates to each strategy's ``run_custom_backtest``. These
tests drive the 3-candle bullish engine through the same path the UI uses and
verify the aggregate contract (metrics, trades, equity curve, signals).
"""
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

import strategy_core
from backtest.engine import run_backtest
from config import IST
from strategies import list_strategies, load_strategy

STRATEGY_ID = "three_candle_bullish"

# Three white soldiers, a red pullback candle and a breakout candle.
# 3C arms at the red high (103.2), risks the pullback low (102.2 * 0.999 = 102.1)
# and books T1 at 103.2 + 1.5R = 104.85.
BREAKOUT_DAY = [
    (100.0, 101.0, 99.8, 100.8, 1000),
    (100.8, 102.0, 100.6, 101.8, 1200),
    (101.8, 103.0, 101.6, 102.9, 1500),
    (102.9, 103.2, 102.2, 102.4, 900),   # red pullback -> armed
    (102.4, 103.4, 102.3, 103.3, 2000),  # trades through 103.2 -> BUY
    (103.3, 103.5, 103.1, 103.4, 1000),  # next bar
]


def make_candles(prices: list[tuple], start="2024-06-03 09:30:00") -> pd.DataFrame:
    """Create a 5-minute candle DataFrame from (open, high, low, close, volume) tuples."""
    t0 = pd.Timestamp(start, tz=IST)
    rows = []
    for i, (o, h, l, c, v) in enumerate(prices):
        rows.append({
            "timestamp": t0 + pd.Timedelta(minutes=5 * i),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
    return pd.DataFrame(rows)


def test_basic_trade():
    """A known 3-candle breakout produces exactly one trade with the expected entry."""
    df = make_candles(BREAKOUT_DAY)

    result = run_backtest(df, STRATEGY_ID, capital=100000, qty=10, symbol="TEST")

    assert result["metrics"]["trades"] == 1, result["metrics"]
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_price"] == 103.2, trade
    assert trade["direction"] == "BUY"
    assert trade["exit_price"] > trade["entry_price"]
    assert trade["gross_pnl"] > 0

    assert result["signals"][4] == "BUY"
    assert len(result["signals"]) == len(df)
    print(f"  Trade: {trade['entry_price']} -> {trade['exit_price']} "
          f"(net ₹{trade['net_pnl']:.2f}, reason {trade['reason']})")
    print("✅ test_basic_trade PASSED")


def test_stop_loss():
    """A candle trading through the pullback low exits at the stop."""
    rows = BREAKOUT_DAY[:-1] + [(103.3, 103.4, 101.5, 101.8, 1000)]
    result = run_backtest(make_candles(rows), STRATEGY_ID, capital=100000, qty=10, symbol="TEST_SL")

    assert len(result["trades"]) == 1, result["trades"]
    trade = result["trades"][0]
    assert trade["reason"] == "stop", trade
    assert trade["exit_price"] == 102.1, trade
    assert trade["net_pnl"] < 0
    print(f"  Stop trade: entry {trade['entry_price']} -> exit {trade['exit_price']} "
          f"(net ₹{trade['net_pnl']:.2f})")
    print("✅ test_stop_loss PASSED")


def test_take_profit():
    """A rally through T1 books half and leaves the runner profitable."""
    rows = BREAKOUT_DAY[:-1] + [
        (103.3, 105.5, 103.2, 105.4, 1000),  # T1 at 104.85
        (105.4, 105.6, 105.2, 105.5, 1000),
    ]
    result = run_backtest(make_candles(rows), STRATEGY_ID, capital=100000, qty=10, symbol="TEST_TP")

    assert len(result["trades"]) == 1, result["trades"]
    trade = result["trades"][0]
    assert trade["exit_price"] > 104.85, trade
    assert trade["gross_pnl"] > 0 and trade["net_pnl"] > 0
    print(f"  Target trade: entry {trade['entry_price']} -> exit {trade['exit_price']} "
          f"(net ₹{trade['net_pnl']:.2f})")
    print("✅ test_take_profit PASSED")


def test_empty_data():
    """Empty input returns a flat result instead of raising."""
    df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    result = run_backtest(df, STRATEGY_ID, capital=100000, qty=10, symbol="EMPTY")

    assert result["metrics"]["trades"] == 0
    assert result["trades"] == []
    assert result["equity_curve"] == [100000]
    print("✅ test_empty_data PASSED")


def test_costs_applied():
    """Every executed trade reports non-zero estimated costs."""
    result = run_backtest(make_candles(BREAKOUT_DAY), STRATEGY_ID,
                          capital=100000, qty=10, symbol="TEST_COST")

    assert result["trades"], "Expected at least one trade"
    for trade in result["trades"]:
        assert trade["costs"] > 0, "Costs should be > 0"
        assert trade["net_pnl"] != trade["gross_pnl"], "Net PnL should differ from gross due to costs"
        print(f"  Trade costs: ₹{trade['costs']:.2f}, gross: ₹{trade['gross_pnl']:.2f}, "
              f"net: ₹{trade['net_pnl']:.2f}")
    print("✅ test_costs_applied PASSED")


def test_equity_curve_length():
    """Equity curve = initial capital + one point per closed trade."""
    result = run_backtest(make_candles(BREAKOUT_DAY), STRATEGY_ID,
                          capital=100000, qty=10, symbol="TEST_EQ")

    expected_len = len(result["trades"]) + 1
    assert len(result["equity_curve"]) == expected_len, \
        f"Expected {expected_len}, got {len(result['equity_curve'])}"
    assert result["equity_curve"][0] == 100000
    print(f"  Equity curve length: {len(result['equity_curve'])} "
          f"(trades: {len(result['trades'])})")
    print("✅ test_equity_curve_length PASSED")


def test_every_registered_strategy_runs():
    """All seven registered strategies execute on 5-minute synthetic data."""
    df = strategy_core.synthetic_5min(days=15, seed=3)

    for strategy_id, display_name in list_strategies():
        result = run_backtest(df, strategy_id, capital=100000, qty=10, symbol="SYNTH")

        assert set(("trades", "equity_curve", "signals", "metrics")).issubset(result)
        assert result["metrics"]["trades"] == len(result["trades"])
        assert len(result["equity_curve"]) == len(result["trades"]) + 1
        assert len(result["signals"]) == len(df)
        assert result["metrics"]["win_rate_pct"] >= 0
        print(f"  {display_name}: {len(result['trades'])} trades, "
              f"win rate {result['metrics']['win_rate_pct']}%")

    print("✅ test_every_registered_strategy_runs PASSED")


def test_no_signal_on_daily_bars():
    """Strategies are 5-minute only: daily bars must not raise or fabricate trades."""
    mod = load_strategy(STRATEGY_ID)
    rows = []
    t0 = pd.Timestamp("2024-06-03 09:15:00", tz=IST)
    for i in range(10):
        rows.append({
            "timestamp": t0 + pd.Timedelta(days=i),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 1000,
        })
    df = pd.DataFrame(rows)

    assert mod.signal(df) is None
    levels = mod.get_trade_levels(df)
    assert levels["entry_price"] is None

    result = mod.run_custom_backtest(df, capital=100000, qty=10, symbol="DAILY")
    assert result["trades"] == []
    assert result["equity_curve"] == [100000]
    print("✅ test_no_signal_on_daily_bars PASSED")


if __name__ == "__main__":
    print("=" * 60)
    print("BACKTEST ENGINE TESTS (offline, synthetic 5-minute data)")
    print("=" * 60)

    test_empty_data()
    test_basic_trade()
    test_stop_loss()
    test_take_profit()
    test_costs_applied()
    test_equity_curve_length()
    test_every_registered_strategy_runs()
    test_no_signal_on_daily_bars()

    print("\n" + "=" * 60)
    print("ALL ENGINE TESTS PASSED ✅")
    print("=" * 60)
