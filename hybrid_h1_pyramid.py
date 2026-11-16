"""H1 - First trigger + pyramid ("add only to a risk-free winner").

Entry : whichever setup (3-candle or VWAP) triggers first.
Exit  : class rules - 50% at 1:1.5, stop to entry + 0.25R, swing-to-swing trail, flat by 14:55.
Edge  : when ANY new setup triggers after T1 is booked and the stop sits at/above every entry, add a leg
        risking 0.5R (max 2 adds). Open risk never exceeds the original 1R, but on trend days the size
        grows exactly where the move is proven.
"""

from __future__ import annotations

from hybrid_engine import HybridConfig, HybridStrategy
from strategy_core import RiskParams, cli_main

NAME = "H1_pyramid"
CONFIG = HybridConfig(name=NAME, pyramid=True, pyramid_risk_r=0.5, pyramid_max_adds=2)


def build(risk: RiskParams | None = None) -> HybridStrategy:
    return HybridStrategy(CONFIG, risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
