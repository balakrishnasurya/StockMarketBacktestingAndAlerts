"""H4 - First trigger + trend-day runner with VWAP as the regime floor (5-minute intraday).

Entry on whichever setup triggers first. 50% is booked at 1:1.5. The runner ignores small wiggles:
the stop only trails to swing lows of pullbacks lasting at least 3 candles and at least 0.5R deep, and
the runner is closed on a candle close back below VWAP (after having been above it), or at 14:55.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import hybrid_h4_trend_runner

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=hybrid_h4_trend_runner.build,
    name=hybrid_h4_trend_runner.NAME,
    display_name="H4 · Trend Runner (5m)",
)


def signal(df):
    return _api.signal(df)


def get_strategy_state(df):
    return _api.get_strategy_state(df)


def get_recent_trades(df, mode=None):
    return _api.get_recent_trades(df, mode=mode)


def get_trade_levels(df, sig=None, mode=None):
    return _api.get_trade_levels(df, sig=sig, mode=mode)


def get_alert_message(df, sig, symbol):
    return _api.get_alert_message(df, sig, symbol)


def run_custom_backtest(df, capital=100_000, qty=10, symbol="UNKNOWN", **kwargs):
    return _api.run_custom_backtest(df, capital=capital, qty=qty, symbol=symbol, **kwargs)
