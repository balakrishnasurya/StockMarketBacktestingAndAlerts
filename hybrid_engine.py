"""Hybrid engine: runs the 3-candle and VWAP detectors side by side on one stock (bullish only).

Building blocks - each hybrid_h*.py preset switches some of them on:
- entry_mode "any"        : take whichever setup triggers first.
- entry_mode "confluence" : enter only when both setups agree (same candle, or the other setup fired
                            within `confluence_window_bars` candles).
- confluence_plan         : exit plan used when the entry itself is confluent (A+ trade).
- extend_on_confirmation  : the other setup triggers while we hold -> add a 2nd target at that setup's own
                            1:1.5 projection and switch to `confirmed_plan` (looser runner trail).
- pyramid                 : a new trigger while the position is risk-free (T1 booked, stop >= every entry)
                            adds a leg whose own risk is `pyramid_risk_r` x the original 1R.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from strategy_core import (
    Bar,
    BaseBullishStrategy,
    ExitPlan,
    Position,
    RiskParams,
    Trigger,
    pick_stop,
    stop_entry_fill,
)
from three_candle_bullish import ThreeCandleDetector, ThreeCandleParams
from vwap_bullish import VwapDetector, VwapParams

# Class rules: 50% at 1:1.5, stop a little above entry, trail swing-to-swing.
TEACHER_PLAN = ExitPlan()
# Same 50% pocket at 1:1.5, but the runner only trails on real swings (>=3 candles, >=0.5R deep)
# and is closed if price closes back under VWAP after having been above it.
RUNNER_PLAN = ExitPlan(trail_min_pullback_bars=3, trail_min_pullback_r=0.5, vwap_floor=True)
# A+ trades: 1/3 at 1.5R, 1/3 at 3R, 1/3 runner with the loose trail + VWAP floor.
APLUS_PLAN = ExitPlan(
    targets_r=((1.5, 1 / 3), (3.0, 1 / 3)),
    trail_min_pullback_bars=3,
    trail_min_pullback_r=0.5,
    vwap_floor=True,
)


@dataclass(frozen=True)
class HybridConfig:
    name: str
    entry_mode: str = "any"  # "any" | "confluence"
    confluence_window_bars: int = 12  # 12 x 5 min = 1 hour
    base_plan: ExitPlan = TEACHER_PLAN
    confluence_plan: ExitPlan | None = None
    extend_on_confirmation: bool = False
    confirmed_plan: ExitPlan | None = None
    pyramid: bool = False
    pyramid_risk_r: float = 0.5
    pyramid_max_adds: int = 1


class HybridStrategy(BaseBullishStrategy):
    def __init__(
        self,
        cfg: HybridConfig,
        three_params: ThreeCandleParams | None = None,
        vwap_params: VwapParams | None = None,
        risk: RiskParams | None = None,
    ) -> None:
        super().__init__(cfg.base_plan, risk)
        self.cfg = cfg
        self.name = cfg.name
        self.three = ThreeCandleDetector(three_params)
        self.vwap = VwapDetector(vwap_params)
        self.entry_sources: set[str] = set()
        self.confirmed = False
        self.adds = 0

    def reset_signals(self) -> None:
        self.three.reset()
        self.vwap.reset()
        self.entry_sources = set()
        self.confirmed = False
        self.adds = 0

    def handle_bar(self, i: int, bars: Sequence[Bar], flat: bool) -> None:
        updates = (self.three.update(i, bars), self.vwap.update(i, bars))
        triggers = [t for t in updates if t is not None]
        if not triggers:
            return
        if flat:
            self._enter(i, bars, triggers)
        elif self.position is not None:
            self._manage(self.position, bars[i], triggers)

    def _is_confluent(self, i: int, bars: Sequence[Bar], triggers: list[Trigger]) -> bool:
        sources = {t.source for t in triggers}
        if len(sources) == 2:
            return True
        window = self.cfg.confluence_window_bars
        if "3C" in sources:
            k = self.vwap.last_cross_up_index
            return k is not None and i - k <= window and bars[i - 1].close > bars[i - 1].vwap
        k = self.three.last_formation_index
        return k is not None and i - k <= window

    def _enter(self, i: int, bars: Sequence[Bar], triggers: list[Trigger]) -> None:
        confluent = self._is_confluent(i, bars, triggers)
        if self.cfg.entry_mode == "confluence" and not confluent:
            return
        first = min(triggers, key=lambda t: t.price)
        stops = tuple(sorted(s for t in triggers for s in t.stops))  # widest first
        use_aplus = confluent and self.cfg.confluence_plan is not None
        plan = self.cfg.confluence_plan if use_aplus else self.cfg.base_plan
        tag = "+".join(sorted(t.source for t in triggers))
        pos = self.open_long(tag, bars[i], first.price, stops, plan)
        if pos is None or pos.closed:
            return
        self.entry_sources = {t.source for t in triggers}
        self.confirmed = use_aplus
        self.adds = 0
        if confluent:
            pos.notes.append("confluent_entry")
        if self.cfg.extend_on_confirmation and not self.confirmed and len(triggers) > 1:
            self._confirm(pos, bars[i], max(triggers, key=lambda t: t.price))

    def _manage(self, pos: Position, bar: Bar, triggers: list[Trigger]) -> None:
        fresh = [t for t in triggers if t.source not in self.entry_sources]
        if self.cfg.extend_on_confirmation and not self.confirmed and fresh:
            self._confirm(pos, bar, fresh[0])
        if self.cfg.pyramid:
            self._pyramid(pos, bar, min(triggers, key=lambda t: t.price))

    def _confirm(self, pos: Position, bar: Bar, trig: Trigger) -> None:
        """The other setup agrees: hold for at least its own 1:1.5 projection and loosen the trail."""
        self.confirmed = True
        base = pos.base
        stop = pick_stop(trig.price, trig.stops, self.risk)
        if stop is None:
            stop = trig.stops[-1]
        t2 = max(trig.price + 1.5 * (trig.price - stop), base.entry + 2.5 * base.risk)
        if not base.first_target_done:
            t1 = base.targets[0][0] if base.targets else base.entry + 1.5 * base.risk
            pos.set_targets(base, [(t1, base.qty / 3), (t2, base.qty / 3)])
        else:
            if not base.targets:
                pos.set_targets(base, [(t2, base.open_qty / 2)])
            if stop < bar.close:
                pos.raise_stop(stop)
        if self.cfg.confirmed_plan is not None:
            pos.plan = self.cfg.confirmed_plan
        pos.notes.append(f"confirmed_by_{trig.source}@{bar.ts:%H:%M} T2={t2:.2f}")

    def _pyramid(self, pos: Position, bar: Bar, trig: Trigger) -> None:
        """Add only to a risk-free winner: T1 booked and the shared stop at/above every entry."""
        if self.adds >= self.cfg.pyramid_max_adds or not pos.trail_active:
            return
        if pos.sl < max(leg.entry for leg in pos.legs) or bar.t > self.risk.last_entry_time:
            return
        fill = stop_entry_fill(bar, trig.price, self.risk.max_chase_pct)
        if fill is None or fill <= pos.sl:
            return
        risk_pts = max(fill - pos.sl, fill * self.risk.min_risk_pct)
        base = pos.base
        qty = min(base.qty, self.cfg.pyramid_risk_r * base.qty * base.risk / risk_pts)
        self.adds += 1
        pos.add_leg(f"add{self.adds}:{trig.source}", bar.ts, fill, qty, fill - risk_pts, ((1.5, 0.5),))
        pos.notes.append(f"pyramid_{trig.source}@{bar.ts:%H:%M} qty={qty:.2f}")
