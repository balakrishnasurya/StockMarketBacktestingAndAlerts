"""H3 - A+ confluence only: both setups must agree (5-minute intraday).

Lone setups are skipped. The entry must be a same-candle 3C + VWAP trigger, or one setup triggering
within an hour of the other (VWAP reclaim with price above VWAP, or a fresh 3-candle pattern). Stop is
the wider structural stop. Exits: 1/3 at 1.5R, 1/3 at 3R, 1/3 runner with the loose swing trail and a
VWAP floor; flat by 14:55.
"""

from __future__ import annotations

from strategies.bullish_adapter import StrategyApi
import hybrid_h3_aplus_confluence

MIN_CANDLES = 5
STOP_LOSS_PCT = 0.010
TAKE_PROFIT_PCT = 0.015
DIRECTION_BIAS = "BULLISH"
MODE = "INTRADAY_5M"

_api = StrategyApi(
    build=hybrid_h3_aplus_confluence.build,
    name=hybrid_h3_aplus_confluence.NAME,
    display_name="H3 · A+ Confluence (5m)",
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
