"""H3 - A+ confluence only (fewer, higher-quality trades held longer).

Entry : only when both setups agree - they trigger on the same candle, OR a 3-candle trigger comes within
        1 hour of a VWAP reclaim with price above VWAP, OR a VWAP trigger comes within 1 hour of a
        3-candle pattern. Lone setups are skipped. Stop = the wider structural stop of the two.
Exit  : 1/3 at 1.5R, 1/3 at 3R, 1/3 runner trailed on real swings (>=3 candles, >=0.5R deep) and closed
        on a candle close back under VWAP; flat by 14:55.
"""

from __future__ import annotations

from hybrid_engine import APLUS_PLAN, HybridConfig, HybridStrategy
from strategy_core import RiskParams, cli_main

NAME = "H3_aplus_confluence"
CONFIG = HybridConfig(name=NAME, entry_mode="confluence", confluence_window_bars=12, confluence_plan=APLUS_PLAN)


def build(risk: RiskParams | None = None) -> HybridStrategy:
    return HybridStrategy(CONFIG, risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
