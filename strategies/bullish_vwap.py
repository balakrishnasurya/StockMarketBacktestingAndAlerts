"""VWAP Reclaim Bullish - first candle that closes back above session VWAP (5-minute intraday).

The stock must first close below VWAP. The setup candle is the first candle that closes above it; the
engine buys when a later candle trades through that candle's high. A close back below VWAP cancels the
setup. Stop under the setup candle's low, 50% booked at 1:1.5, runner trailed on swing lows, flat by 14:55.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import vwap_bullish

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=vwap_bullish.build,
    name=vwap_bullish.NAME,
    display_name="VWAP Reclaim Bullish (5m)",
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
