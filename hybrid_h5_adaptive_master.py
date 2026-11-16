"""H5 - Adaptive master: every component, switched on by what the market confirms.

Entry : whichever setup triggers first.
Plan  : - confluent entry (both setups agree)  -> A+ plan: 1/3 at 1.5R, 1/3 at 3R, runner + VWAP floor;
        - single-setup entry                   -> class plan: 50% at 1:1.5, swing trail;
        - other setup confirms while holding   -> T2 at its own 1:1.5 projection + loose runner + VWAP floor;
        - any new trigger once risk-free       -> pyramid leg risking 0.5R (max 2).
Flat by 14:55. Worst case of any trade stays -1R (barring gaps/slippage): adds only happen once risk-free.
"""

from __future__ import annotations

from hybrid_engine import APLUS_PLAN, RUNNER_PLAN, TEACHER_PLAN, HybridConfig, HybridStrategy
from strategy_core import RiskParams, cli_main

NAME = "H5_adaptive_master"
CONFIG = HybridConfig(
    name=NAME,
    base_plan=TEACHER_PLAN,
    confluence_plan=APLUS_PLAN,
    extend_on_confirmation=True,
    confirmed_plan=RUNNER_PLAN,
    pyramid=True,
    pyramid_risk_r=0.5,
    pyramid_max_adds=2,
)


def build(risk: RiskParams | None = None) -> HybridStrategy:
    return HybridStrategy(CONFIG, risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
