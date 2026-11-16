"""Bullish VWAP reclaim setup - 5-minute intraday.

Rules extracted from the class:
1. Same stock selection as the 3-candle setup; only the bullish side is traded.
2. VWAP = broker default (session anchored, typical price, no other settings). Only candles from 09:30 count.
3. Price must first close BELOW VWAP. The setup candle is the first candle that then closes ABOVE VWAP
   (colour does not matter - the close versus VWAP does). If price never dips under VWAP: no VWAP trade.
4. Buy when a later candle trades above the setup candle's high (a touch is enough, no close needed).
   If a candle closes back below VWAP before that, the setup is cancelled - wait for a fresh reclaim.
5. Stop: just under the setup candle's low (small buffer). T1 = 1:1.5 book 50%, then trail like the
   3-candle setup; square off at 14:55. VWAP usually gives 1-2 good trades a day -> max 2 entries.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time

from strategy_core import Bar, BaseBullishStrategy, ExitPlan, RiskParams, Trigger, cli_main

NAME = "VWAP_bullish"


@dataclass(frozen=True)
class VwapParams:
    signal_start: time = time(9, 30)
    max_entries_per_day: int = 2
    entry_buffer_pct: float = 0.0
    sl_buffer_pct: float = 0.001
    shift_trigger_on_red: bool = False  # variant: a red candle (still above VWAP) lowers the trigger
    allow_pre_start_cross: bool = False  # variant: a reclaim before 09:30 arms on the first 09:30+ candle


class VwapDetector:
    """Causal state machine: below VWAP -> close above VWAP (arm) -> break of setup high -> Trigger."""

    source = "VWAP"

    def __init__(self, params: VwapParams | None = None) -> None:
        self.p = params or VwapParams()
        self.reset()

    def reset(self) -> None:
        self.side: str | None = None
        self.armed = False
        self.trigger = math.nan
        self.signal_low = math.nan
        self.signal_vwap = math.nan
        self.entries_today = 0
        self.last_cross_up_index: int | None = None
        self._pre_start_cross = False

    def update(self, i: int, bars: Sequence[Bar]) -> Trigger | None:
        bar, p = bars[i], self.p
        hit: Trigger | None = None
        if self.armed:
            if bar.high >= self.trigger:
                self.entries_today += 1
                hit = Trigger(self.source, i, self.trigger, self._stops(), self.entries_today)
                self.armed = False
            elif bar.close < bar.vwap:
                self.armed = False
            elif p.shift_trigger_on_red and bar.is_red:
                self.trigger = bar.high * (1 + p.entry_buffer_pct)
                self.signal_low = min(self.signal_low, bar.low)

        side = "above" if bar.close > bar.vwap else "below" if bar.close < bar.vwap else self.side
        crossed_up = self.side == "below" and side == "above"
        if crossed_up:
            self.last_cross_up_index = i
        after_start = bar.t >= p.signal_start
        reclaim = crossed_up or (p.allow_pre_start_cross and self._pre_start_cross)
        if (
            hit is None
            and not self.armed
            and after_start
            and side == "above"
            and reclaim
            and self.entries_today < p.max_entries_per_day
        ):
            self._arm(bar)
        self._pre_start_cross = (not after_start) and (crossed_up or (self._pre_start_cross and side == "above"))
        self.side = side
        return hit

    def _arm(self, bar: Bar) -> None:
        self.armed = True
        self.trigger = bar.high * (1 + self.p.entry_buffer_pct)
        self.signal_low = bar.low
        self.signal_vwap = bar.vwap

    def _stops(self) -> tuple[float, ...]:
        b = 1 - self.p.sl_buffer_pct
        # Preferred: setup candle low; fallback (if too wide): just under VWAP at the setup candle.
        return (self.signal_low * b, max(self.signal_vwap, self.signal_low) * b)


class VwapBullishStrategy(BaseBullishStrategy):
    name = NAME

    def __init__(
        self,
        params: VwapParams | None = None,
        plan: ExitPlan | None = None,
        risk: RiskParams | None = None,
    ) -> None:
        super().__init__(plan, risk)
        self.detector = VwapDetector(params)

    def reset_signals(self) -> None:
        self.detector.reset()

    def handle_bar(self, i: int, bars: Sequence[Bar], flat: bool) -> None:
        trig = self.detector.update(i, bars)
        if trig is not None and flat:
            self.open_long(trig.source, bars[i], trig.price, trig.stops)


def build(risk: RiskParams | None = None) -> VwapBullishStrategy:
    return VwapBullishStrategy(risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
