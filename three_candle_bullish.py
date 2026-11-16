"""Bullish 3-candle setup ("three white soldiers" + pullback breakout) - 5-minute intraday.

Rules extracted from the class:
1. Trade only stocks on the day's bullish list (NSE %-change-in-OI sorted green at ~09:30); skip indices,
   sub-Rs 100 stocks and big gap-ups (use --watchlist / DayFilter).
2. Pattern (only candles from 09:30 on): three green candles in a row where candle 2 closes above
   candle 1's high and candle 3 closes above candle 2's high. More green candles -> keep waiting.
3. A red candle after the pattern is compulsory; its high is the trigger. If the next candle does not break
   it and another red candle forms, the newest red candle's high becomes the trigger.
4. Buy the moment price trades above the trigger (no close needed). Do not chase far above it.
5. Stop: just under the nearest swing low (the pullback low) with a small buffer.
6. T1 = 1:1.5 -> book 50%, stop to a few points above entry. Trail the rest only after price breaks the
   previous swing high, to just under the pullback low. The stop is never touched before T1.
7. Square off at 14:55. Only the first two pattern occurrences of the day are tradable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time

from strategy_core import Bar, BaseBullishStrategy, ExitPlan, RiskParams, Trigger, cli_main

NAME = "3C_bullish"


@dataclass(frozen=True)
class ThreeCandleParams:
    pattern_start: time = time(9, 30)
    max_setups_per_day: int = 2
    entry_buffer_pct: float = 0.0  # optional 1-2 point buffer above the red candle's high
    sl_buffer_pct: float = 0.001
    max_wait_bars: int | None = None  # the class gave no expiry; set e.g. 6 to cancel stale setups


class ThreeCandleDetector:
    """Causal state machine: scan -> wait_red -> armed -> Trigger."""

    source = "3C"

    def __init__(self, params: ThreeCandleParams | None = None) -> None:
        self.p = params or ThreeCandleParams()
        self.reset()

    def reset(self) -> None:
        self.state = "scan"
        self.setups_today = 0
        self.trigger = math.nan
        self.pullback_low = math.nan
        self.last_red_low = math.nan
        self.bars_waited = 0
        self.arm_index = -1
        self.last_formation_index: int | None = None

    def update(self, i: int, bars: Sequence[Bar]) -> Trigger | None:
        bar = bars[i]
        hit: Trigger | None = None
        if self.state == "armed":
            if bar.high >= self.trigger:
                hit = Trigger(self.source, i, self.trigger, self._stops(), self.setups_today)
                self.state = "scan"
            else:
                self.pullback_low = min(self.pullback_low, bar.low)
                self.bars_waited += 1
                if bar.is_red:
                    self.trigger = bar.high * (1 + self.p.entry_buffer_pct)
                    self.last_red_low = bar.low
                    self.arm_index = i
                if self.p.max_wait_bars is not None and self.bars_waited > self.p.max_wait_bars:
                    self.state = "scan"
        elif self.state == "wait_red" and bar.is_red:
            self._arm(i, bar)

        fresh_pattern = self.state == "scan" or (self.state == "armed" and i - 2 > self.arm_index)
        if hit is None and fresh_pattern and self._pattern_at(i, bars):
            if self.setups_today < self.p.max_setups_per_day:
                self.setups_today += 1
                self.last_formation_index = i
                self.state = "wait_red"
        return hit

    def _arm(self, i: int, bar: Bar) -> None:
        self.state = "armed"
        self.trigger = bar.high * (1 + self.p.entry_buffer_pct)
        self.pullback_low = bar.low
        self.last_red_low = bar.low
        self.bars_waited = 0
        self.arm_index = i

    def _stops(self) -> tuple[float, ...]:
        b = 1 - self.p.sl_buffer_pct
        # Preferred: whole pullback swing low; fallback (if too wide): the trigger red candle's low.
        return (self.pullback_low * b, self.last_red_low * b)

    def _pattern_at(self, i: int, bars: Sequence[Bar]) -> bool:
        if i < 2:
            return False
        a, b, c = bars[i - 2], bars[i - 1], bars[i]
        return (
            a.t >= self.p.pattern_start
            and a.is_green
            and b.is_green
            and c.is_green
            and b.close > a.high
            and c.close > b.high
        )


class ThreeCandleBullishStrategy(BaseBullishStrategy):
    name = NAME

    def __init__(
        self,
        params: ThreeCandleParams | None = None,
        plan: ExitPlan | None = None,
        risk: RiskParams | None = None,
    ) -> None:
        super().__init__(plan, risk)
        self.detector = ThreeCandleDetector(params)

    def reset_signals(self) -> None:
        self.detector.reset()

    def handle_bar(self, i: int, bars: Sequence[Bar], flat: bool) -> None:
        trig = self.detector.update(i, bars)
        if trig is not None and flat:
            self.open_long(trig.source, bars[i], trig.price, trig.stops)


def build(risk: RiskParams | None = None) -> ThreeCandleBullishStrategy:
    return ThreeCandleBullishStrategy(risk=risk)


if __name__ == "__main__":
    cli_main({NAME: build}, __doc__)
