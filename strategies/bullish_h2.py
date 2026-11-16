"""H2 - First trigger + target extension when the other setup confirms (5-minute intraday).

Entry on whichever setup triggers first with class rules. If the OTHER setup triggers while holding,
the trade becomes a runner: 1/3 at 1.5R, 1/3 at T2 (the confirming setup's own 1:1.5 projection or
entry + 2.5R, whichever is higher), 1/3 trailed only on real swings. No extra size is added.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import hybrid_h2_confirmation_extension

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=hybrid_h2_confirmation_extension.build,
    name=hybrid_h2_confirmation_extension.NAME,
    display_name="H2 · Confirmation Extension (5m)",
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
