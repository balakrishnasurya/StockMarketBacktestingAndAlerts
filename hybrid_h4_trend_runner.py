"""H4 - First trigger + trend-day runner (VWAP as the regime floor).

Entry : whichever setup triggers first.
Exit  : 50% at 1:1.5 (money in pocket, as taught). The runner ignores small wiggles - the stop only trails
        to swing lows of pullbacks that last >=3 candles and are >=0.5R deep - and it is closed when a
        candle closes back below VWAP (after having been above it), or at 14:55.
"""

from __future__ import annotations

from hybrid_engine import RUNNER_PLAN, HybridConfig, HybridStrategy
from strategy_core import RiskParams, cli_main

NAME = "H4_trend_runner"
CONFIG = HybridConfig(name=NAME, base_plan=RUNNER_PLAN)


def build(risk: RiskParams | None = None) -> HybridStrategy:
    return HybridStrategy(CONFIG, risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
