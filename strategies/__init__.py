"""
Strategy Registry
=================
Add a new strategy with ONE line in the REGISTRY dict:
    "my_strategy": ("strategies.my_strategy", "My Strategy Name"),

The tuple is (module_path, display_name).
Each strategy module must define:
    - signal(df) -> 'BUY' | 'SELL' | None
And may optionally define:
    - STOP_LOSS_PCT (float)
    - TAKE_PROFIT_PCT (float)
    - MIN_CANDLES (int)

Only the bullish 5-minute strategies from the standalone engine are registered here. They are wrapped by
``strategies/bullish_*.py`` adapters so the scanner, journal and backtester all use the same engine.
"""
import importlib
from typing import Optional
import pandas as pd


# ── Registry: strategy_id -> (module_path, display_name) ────────────────
REGISTRY: dict[str, tuple[str, str]] = {
    "three_candle_bullish": ("strategies.bullish_3c", "3-Candle Bullish (5m)"),
    "vwap_bullish": ("strategies.bullish_vwap", "VWAP Reclaim Bullish (5m)"),
    "h1_pyramid": ("strategies.bullish_h1", "H1 · Pyramid (5m)"),
    "h2_confirm_extend": ("strategies.bullish_h2", "H2 · Confirmation Extension (5m)"),
    "h3_aplus_confluence": ("strategies.bullish_h3", "H3 · A+ Confluence (5m)"),
    "h4_trend_runner": ("strategies.bullish_h4", "H4 · Trend Runner (5m)"),
    "h5_adaptive_master": ("strategies.bullish_h5", "H5 · Adaptive Master (5m)"),
}


def load_strategy(strategy_id: str):
    """Import and return the strategy module."""
    if strategy_id not in REGISTRY:
        raise ValueError(f"Unknown strategy '{strategy_id}'. Available: {list(REGISTRY.keys())}")
    module_path, _ = REGISTRY[strategy_id]
    return importlib.import_module(module_path)


def get_display_name(strategy_id: str) -> str:
    """Return the human-readable name for a strategy."""
    return REGISTRY.get(strategy_id, (None, strategy_id))[1]


def get_strategy_param(strategy_id: str, param: str, default=None):
    """Read a constant from a strategy module (e.g. STOP_LOSS_PCT)."""
    mod = load_strategy(strategy_id)
    return getattr(mod, param, default)


def get_signal_series(df: pd.DataFrame, strategy_id: str) -> list[Optional[str]]:
    """
    Return a per-row signal list by calling signal() on df.iloc[:i+1] for each row.
    This ensures NO look-ahead: each signal sees only data up to that row.
    """
    mod = load_strategy(strategy_id)
    min_candles = getattr(mod, "MIN_CANDLES", 1)
    signals: list[Optional[str]] = []
    for i in range(len(df)):
        if i + 1 < min_candles:
            signals.append(None)
        else:
            sig = mod.signal(df.iloc[: i + 1])
            signals.append(sig)
    return signals


def list_strategies() -> list[tuple[str, str]]:
    """Return list of (strategy_id, display_name) tuples."""
    return [(k, v[1]) for k, v in REGISTRY.items()]
