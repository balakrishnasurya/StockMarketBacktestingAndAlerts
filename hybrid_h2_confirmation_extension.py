"""H2 - First trigger + target extension when the other setup confirms (the class's own idea).

Entry : whichever setup triggers first.
Exit  : class rules until the OTHER setup also triggers. Then:
        - before T1: book 1/3 at 1:1.5, 1/3 at T2, keep 1/3 as runner;
        - after T1 : half of the runner is parked at T2, stop raised to the confirming setup's stop;
        T2 = max(confirming setup's own 1:1.5 target, entry + 2.5R). The runner then trails only on
        real swings (>=3 candles, >=0.5R deep). No extra size is added.
"""

from __future__ import annotations

from hybrid_engine import HybridConfig, HybridStrategy
from strategy_core import ExitPlan, RiskParams, cli_main

NAME = "H2_confirm_extend"
CONFIG = HybridConfig(
    name=NAME,
    extend_on_confirmation=True,
    confirmed_plan=ExitPlan(trail_min_pullback_bars=3, trail_min_pullback_r=0.5),
)


def build(risk: RiskParams | None = None) -> HybridStrategy:
    return HybridStrategy(CONFIG, risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
