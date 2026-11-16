"""3-Candle Bullish - "three white soldiers" impulse + red pullback breakout (5-minute intraday).

Only stocks on the bullish list are intended; this module exposes the standalone engine to the app.
The engine buys the moment price trades above the red pullback candle's high, risks the structural
pullback low (fractions above 2% are skipped), books 50% at 1:1.5, then trails the runner on swing lows.
Flat by 14:55. Only the first two pattern occurrences of the day are tradable.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import three_candle_bullish

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=three_candle_bullish.build,
    name=three_candle_bullish.NAME,
    display_name="3-Candle Bullish (5m)",
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
