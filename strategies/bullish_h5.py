"""H5 - Adaptive master: every component switched on by what the market confirms (5-minute intraday).

Confluent entries use the A+ plan (1/3 at 1.5R, 1/3 at 3R, runner with VWAP floor). Single-setup
entries use the class plan (50% at 1:1.5, swing trail). If the other setup confirms while holding, T2
is extended and the runner trail loosens. Once risk-free, any new trigger can add a 0.5R pyramid leg
(max 2). Worst case stays about -1R per trade, barring gaps and slippage. Flat by 14:55.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import hybrid_h5_adaptive_master

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=hybrid_h5_adaptive_master.build,
    name=hybrid_h5_adaptive_master.NAME,
    display_name="H5 · Adaptive Master (5m)",
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
