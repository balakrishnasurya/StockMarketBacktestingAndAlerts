"""H1 - First trigger + pyramid: add 0.5R to a risk-free winner (5-minute intraday).

Entry on whichever setup triggers first. 50% off at 1:1.5, stop to entry + 0.25R, swing trail, flat by
14:55. Once T1 is booked and the shared stop is at/above every entry, a new setup trigger adds a leg
risking 0.5R (max 2 adds), so open risk never exceeds the original 1R.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import hybrid_h1_pyramid

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=hybrid_h1_pyramid.build,
    name=hybrid_h1_pyramid.NAME,
    display_name="H1 · Pyramid (5m)",
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
