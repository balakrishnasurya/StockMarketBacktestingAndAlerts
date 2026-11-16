"""Adapter between the root-level bullish backtest engine and the app's strategy registry.

The standalone strategies in ``strategy_core.py`` / ``hybrid_engine.py`` work on ``Bar`` objects and
``Position`` state.  The Streamlit app expects a module-level interface:

    signal(df)                  -> "BUY" | None for the latest closed candle
    get_strategy_state(df)      -> dict for the inspector
    get_recent_trades(df)       -> list of triggers for scanner catch-up / journal
    get_trade_levels(df, sig)   -> entry / stop / targets for alerts
    get_alert_message(df, ...)  -> human readable alert text
    run_custom_backtest(df,...) -> dict used by backtest/engine.py

Every wrapper module in this package instantiates ``StrategyApi`` with one ``build`` function and
re-exports its methods.  The adapter replays the engine over the candles and reports the trigger that
the engine itself would have taken, so the app and the backtester share one source of truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Optional

import pandas as pd

import strategy_core as core
from config import COST_PER_SIDE, RESULTS_DIR

MIN_CANDLES = 5


def _empty_levels(state: str = "NONE") -> dict[str, Any]:
    return {
        "entry_price": None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "rr": None,
        "risk_per_share": None,
        "direction": None,
        "state": state,
    }


def _round(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else round(float(value), 2)


class StrategyApi:
    """Registry-facing facade around one root-level bullish strategy factory."""

    def __init__(
        self,
        build: Callable[..., core.BaseBullishStrategy],
        name: str,
        display_name: str,
        description: str = "",
    ) -> None:
        self.build = build
        self.name = name
        self.display_name = display_name
        self.description = description
        self.risk = core.RiskParams()

    # ------------------------------------------------------------------ data helpers

    @staticmethod
    def _prepare(df: pd.DataFrame) -> pd.DataFrame | None:
        if df is None or len(df) == 0:
            return None
        try:
            data = core.add_session_vwap(core.ensure_5min(core.normalize_ohlcv(df)))
        except Exception:
            return None
        return data if not data.empty else None

    @staticmethod
    def _to_bars(group: pd.DataFrame) -> list[core.Bar]:
        return [
            core.Bar(
                ts=row["datetime"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                vwap=float(row["vwap"]),
            )
            for _, row in group.iterrows()
        ]

    @staticmethod
    def _filter_last_day(data: pd.DataFrame) -> pd.DataFrame:
        last_day = data["datetime"].dt.date.iloc[-1]
        return data[data["datetime"].dt.date == last_day].reset_index(drop=True)

    def _capture(self, entries: dict[pd.Timestamp, dict], pos: core.Position) -> None:
        base = pos.base
        snap = entries.get(base.entry_ts)
        if snap is None:
            snap = {
                "entry_ts": base.entry_ts,
                "entry": float(base.entry),
                "initial_sl": float(pos.initial_sl),
                "initial_plan_targets": tuple(pos.plan.targets_r),
                "initial_targets": [list(t) for t in base.targets],
            }
            entries[base.entry_ts] = snap
        snap.update(
            {
                "initial_sl": float(pos.initial_sl),
                "sl": float(pos.sl),
                "plan_targets": tuple(pos.plan.targets_r),
                "targets": [list(t) for t in base.targets],
                "legs": len(pos.legs),
                "notes": list(pos.notes),
            }
        )

    def _simulate(
        self,
        df: pd.DataFrame,
        symbol: str = "SYMBOL",
        last_day_only: bool = False,
    ) -> tuple[core.BaseBullishStrategy | None, list[core.TradeRecord], dict, pd.DataFrame | None]:
        data = self._prepare(df)
        if data is None:
            return None, [], {}, None
        if last_day_only:
            data = self._filter_last_day(data)
        try:
            strat = self.build()
        except Exception:
            return None, [], {}, data

        entries: dict[pd.Timestamp, dict] = {}
        original_record = strat._record

        def capture_then_record() -> None:
            if strat.position is not None and strat.position.legs:
                self._capture(entries, strat.position)
            original_record()

        strat._record = capture_then_record  # type: ignore[method-assign]

        for day, group in data.groupby(data["datetime"].dt.date, sort=True):
            bars = self._to_bars(group)
            if not bars:
                continue
            strat.on_day_start(symbol, day)
            for i in range(len(bars)):
                strat.on_bar(i, bars)
                if strat.position is not None and strat.position.legs:
                    self._capture(entries, strat.position)
            if strat.position is not None and strat.position.legs:
                self._capture(entries, strat.position)
            strat.on_day_end(bars)
        return strat, list(strat.trades), entries, data

    # ------------------------------------------------------------------ target helpers

    @staticmethod
    def _targets(snap: dict | None, trade: core.TradeRecord) -> tuple[float | None, float | None]:
        risk = trade.entry - trade.initial_sl
        plan: Sequence[tuple[float, float]] = (
            snap.get("initial_plan_targets", ((1.5, 0.5),)) if snap else ((1.5, 0.5),)
        )
        original = [trade.entry + r * risk for r, _ in plan]
        tp1 = original[0] if original else trade.entry + 1.5 * risk
        tp2 = original[1] if len(original) > 1 else None

        # H2/H5 may install a custom T2 after confirmation (e.g. entry + 2.5R).
        latest = [float(t[0]) for t in (snap.get("targets", []) if snap else [])]
        for price in latest:
            if price > tp1 + core.EPS and (tp2 is None or price > tp2):
                tp2 = price
        return _round(tp1), _round(tp2)

    @staticmethod
    def _last_reason(exits: str) -> str:
        if not exits:
            return "eod"
        tail = exits.split("|")[-1].strip()
        return tail.split("@", 1)[0] or "eod"

    # ------------------------------------------------------------------ detector state

    @staticmethod
    def _detectors(strat: core.BaseBullishStrategy) -> list:
        detector = getattr(strat, "detector", None)
        if detector is not None:
            return [detector]
        return [d for d in (getattr(strat, "three", None), getattr(strat, "vwap", None)) if d is not None]

    def _confluence_ready(self, strat: core.BaseBullishStrategy, data: pd.DataFrame) -> bool:
        three = getattr(strat, "three", None)
        vwap = getattr(strat, "vwap", None)
        if three is None or vwap is None:
            return True
        if three.state == "armed" and vwap.armed:
            return True
        window = getattr(getattr(strat, "cfg", None), "confluence_window_bars", 12)
        i = len(data) - 1
        last = data.iloc[-1]
        if three.state == "armed":
            k = vwap.last_cross_up_index
            return k is not None and i - k <= window and last["close"] > last["vwap"]
        if vwap.armed:
            k = three.last_formation_index
            return k is not None and i - k <= window
        return False

    def _armed_levels(self, strat: core.BaseBullishStrategy, data: pd.DataFrame) -> dict | None:
        candidates: list[tuple[str, float, tuple[float, ...]]] = []
        for det in self._detectors(strat):
            armed = getattr(det, "armed", getattr(det, "state", None) == "armed")
            trigger = float(getattr(det, "trigger", math.nan))
            if armed and math.isfinite(trigger):
                candidates.append((det.source, trigger, tuple(det._stops())))
        if not candidates:
            return None

        entry_mode = getattr(getattr(strat, "cfg", None), "entry_mode", "any")
        if entry_mode == "confluence" and not self._confluence_ready(strat, data):
            return None

        # The engine enters on the lowest trigger when both setups are armed.
        source, trigger, stops = min(candidates, key=lambda c: c[1])
        stop = core.pick_stop(trigger, stops, self.risk)
        if stop is None:
            return None
        risk = trigger - stop
        plan = getattr(strat, "plan", core.ExitPlan())
        tp1 = trigger + plan.targets_r[0][0] * risk
        tp2 = trigger + plan.targets_r[1][0] * risk if len(plan.targets_r) > 1 else None
        return {
            "entry_price": _round(trigger),
            "stop_loss": _round(stop),
            "tp1": _round(tp1),
            "tp2": _round(tp2),
            "rr": "1:1.5" if tp2 is None else "1:1.5 / 1:3",
            "risk_per_share": _round(risk),
            "direction": "BUY",
            "state": "ARMED",
            "source": source,
        }

    def _waiting_state(self, strat: core.BaseBullishStrategy) -> str:
        if any(getattr(d, "state", None) == "wait_red" for d in self._detectors(strat)):
            return "WAITING_PULLBACK"
        return "SCANNING"

    # ------------------------------------------------------------------ public API

    def signal(self, df: pd.DataFrame) -> Optional[str]:
        """Return ``"BUY"`` when the engine entered on the latest closed candle."""
        _, trades, _, data = self._simulate(df, last_day_only=True)
        if data is None or data.empty:
            return None
        last_ts = data["datetime"].iloc[-1]
        return "BUY" if any(t.entry_ts == last_ts for t in trades) else None

    def _levels_from_sim(
        self,
        strat: core.BaseBullishStrategy | None,
        trades: list[core.TradeRecord],
        entries: dict,
        data: pd.DataFrame | None,
    ) -> dict[str, Any]:
        if strat is None or data is None or data.empty:
            return _empty_levels("INSUFFICIENT_DATA")

        last_ts = data["datetime"].iloc[-1]
        hit = next((t for t in trades if t.entry_ts == last_ts), None)
        if hit is not None:
            snap = entries.get(hit.entry_ts)
            tp1, tp2 = self._targets(snap, hit)
            risk = float(hit.entry - hit.initial_sl)
            return {
                "entry_price": _round(hit.entry),
                "stop_loss": _round(hit.initial_sl),
                "tp1": tp1,
                "tp2": tp2,
                "rr": "1:1.5" if tp2 is None else "1:1.5 / 1:3",
                "risk_per_share": _round(risk),
                "direction": "BUY",
                "state": "TRIGGERED",
            }

        armed = self._armed_levels(strat, data)
        if armed is not None:
            return armed
        return _empty_levels(self._waiting_state(strat))

    def get_trade_levels(self, df: pd.DataFrame, sig: str | None = None, mode: str | None = None) -> dict:
        strat, trades, entries, data = self._simulate(df, last_day_only=True)
        return self._levels_from_sim(strat, trades, entries, data)

    def get_strategy_state(self, df: pd.DataFrame) -> dict:
        strat, trades, entries, data = self._simulate(df, last_day_only=True)
        if data is None or data.empty:
            return {
                "state": "INSUFFICIENT_DATA",
                "message": "Need 5-minute candles (at least a few bars in the current session).",
                "trigger_price": None,
                "pullback_extreme": None,
                "active_bias": None,
                "signal": None,
                "last_close": None,
                "active_trade": None,
            }

        last_close = float(data["close"].iloc[-1])
        last_ts = data["datetime"].iloc[-1]
        sig = "BUY" if any(t.entry_ts == last_ts for t in trades) else None
        levels = self._levels_from_sim(strat, trades, entries, data)
        state = levels.get("state", "SCANNING")

        if levels.get("state") == "TRIGGERED":
            msg = (
                f"🚀 IN TRADE: BUY @ ₹{levels['entry_price']:.2f} | SL: ₹{levels['stop_loss']:.2f} "
                f"| TP1: ₹{levels['tp1']:.2f}"
            )
        elif levels.get("state") == "ARMED":
            msg = (
                f"⚡ ARMED ({levels.get('source', '3C/VWAP')}): Trigger @ ₹{levels['entry_price']:.2f} "
                f"| SL @ ₹{levels['stop_loss']:.2f} (Current: ₹{last_close:.2f})"
            )
        elif state == "WAITING_PULLBACK":
            msg = "⏳ WAITING PULLBACK: impulse confirmed. Waiting for the red pullback candle."
        else:
            msg = "🔍 SCANNING: Looking for a bullish 5-minute setup."

        active_trade = None
        if levels.get("state") == "TRIGGERED":
            active_trade = {
                "entry_time": last_ts,
                "direction": "BUY",
                "entry_price": levels["entry_price"],
                "initial_sl": levels["stop_loss"],
                "current_sl": levels["stop_loss"],
                "tp1_price": levels["tp1"],
                "tp2_price": levels["tp2"],
                "tp1_hit": False,
            }

        return {
            "state": state,
            "active_bias": "BULLISH",
            "trigger_price": levels.get("entry_price") if state in ("ARMED", "TRIGGERED") else None,
            "pullback_extreme": levels.get("stop_loss") if state in ("ARMED", "TRIGGERED") else None,
            "current_sl": levels.get("stop_loss") if state in ("ARMED", "TRIGGERED") else None,
            "signal": sig,
            "message": msg,
            "last_close": last_close,
            "active_trade": active_trade,
        }

    def get_recent_trades(self, df: pd.DataFrame, mode: str | None = None) -> list[dict]:
        strat, trades, entries, data = self._simulate(df, last_day_only=True)
        if data is None or data.empty:
            return []
        last_day = data["datetime"].dt.date.iloc[-1]
        last_ts = data["datetime"].iloc[-1]

        out: list[dict] = []
        for trade in trades:
            if not hasattr(trade.entry_ts, "date") or trade.entry_ts.date() != last_day:
                continue
            snap = entries.get(trade.entry_ts)
            tp1, tp2 = self._targets(snap, trade)
            risk = float(trade.entry - trade.initial_sl)
            out.append(
                {
                    "entry_time": trade.entry_ts,
                    "direction": "BUY",
                    "bias": "BULLISH",
                    "entry_price": _round(trade.entry),
                    "stop_loss": _round(trade.initial_sl),
                    "tp1": tp1,
                    "tp2": tp2,
                    "rr": "1:1.5" if tp2 is None else "1:1.5 / 1:3",
                    "exit_time": trade.exit_ts,
                    "exit_reason": self._last_reason(trade.exits),
                    "realized_pnl": round(trade.net_r * risk, 2),
                    "is_open": self._last_reason(trade.exits) == "eod" and trade.exit_ts == last_ts,
                }
            )
        return out

    def get_alert_message(self, df: pd.DataFrame, sig: str, symbol: str) -> Optional[str]:
        if sig != "BUY":
            return None
        levels = self.get_trade_levels(df, sig=sig)
        last = df.iloc[-1]
        price = float(last["close"])
        candle_time = last.get("timestamp", "")
        sl = levels.get("stop_loss") or price * 0.995
        tp1 = levels.get("tp1") or price + (price - sl) * 1.5
        return (
            f"🔔 {symbol}: BUY at ₹{price:.2f} ({self.display_name} | "
            f"SL: ₹{sl:.2f} | TP1 50%: ₹{tp1:.2f} | candle: {candle_time}). "
            f"Alert only — place manually, no auto-order."
        )

    def _empty_backtest(self, capital: float, n_rows: int = 0) -> dict:
        metrics = {
            "trades": 0,
            "wins": 0,
            "win_rate_pct": 0.0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown_pct": 0.0,
            "cost_note": f"Costs: {COST_PER_SIDE * 100:.1f}% per side (estimate — real brokerage/STT/GST differ)",
            "drawdown_note": "Max drawdown computed on closed-trade equity curve",
            "note": "These strategies are defined on 5-minute intraday candles.",
        }
        return {"metrics": metrics, "trades": [], "equity_curve": [capital], "signals": [None] * n_rows}

    def run_custom_backtest(
        self,
        df: pd.DataFrame,
        capital: float = 100_000,
        qty: int = 10,
        symbol: str = "UNKNOWN",
        **kwargs,
    ) -> dict:
        strat, trades, entries, data = self._simulate(df, symbol=symbol, last_day_only=False)
        if data is None or data.empty:
            return self._empty_backtest(capital, len(df) if df is not None else 0)

        ts_to_idx = {ts: idx for idx, ts in enumerate(data["datetime"])}
        out: list[dict] = []
        equity_curve: list[float] = [capital]
        running = capital

        for trade in trades:
            snap = entries.get(trade.entry_ts)
            risk_per_share = float(trade.entry - trade.initial_sl)
            gross = trade.gross_r * risk_per_share * qty
            cost = trade.cost_r * risk_per_share * qty
            net = trade.net_r * risk_per_share * qty
            entry_idx = ts_to_idx.get(trade.entry_ts, 0)
            exit_idx = ts_to_idx.get(trade.exit_ts, len(data) - 1)
            tp1, tp2 = self._targets(snap, trade)
            notes = "; ".join(snap.get("notes", [])) if snap else ""
            out.append(
                {
                    "entry_idx": entry_idx,
                    "entry_time": trade.entry_ts,
                    "entry_price": _round(trade.entry),
                    "exit_idx": exit_idx,
                    "exit_time": trade.exit_ts,
                    "exit_price": _round(trade.entry + trade.gross_r * risk_per_share),
                    "qty": qty,
                    "direction": "BUY",
                    "gross_pnl": round(gross, 2),
                    "costs": round(cost, 2),
                    "net_pnl": round(net, 2),
                    "reason": self._last_reason(trade.exits),
                    "events": f"SL ₹{trade.initial_sl:.2f} -> TP1 ₹{tp1 or 0:.2f} -> exits: {trade.exits}"
                    + (f" | {notes}" if notes else ""),
                }
            )
            running += net
            equity_curve.append(round(running, 2))

        signals: list[str | None] = [None] * len(data)
        for trade in trades:
            idx = ts_to_idx.get(trade.entry_ts)
            if idx is not None:
                signals[idx] = "BUY"

        metrics = self._metrics(out, equity_curve, capital)
        if out:
            try:
                pd.DataFrame(out).to_csv(RESULTS_DIR / f"{symbol}_{self.name}_trades.csv", index=False)
            except Exception:
                pass
        return {"metrics": metrics, "trades": out, "equity_curve": equity_curve, "signals": signals}

    @staticmethod
    def _metrics(trades: list[dict], equity_curve: list[float], initial_capital: float) -> dict:
        if not trades:
            return StrategyApi._empty_metrics()
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        total_pnl = sum(t["net_pnl"] for t in trades)
        peak = equity_curve[0]
        max_dd = 0.0
        for value in equity_curve:
            peak = max(peak, value)
            dd = (peak - value) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return {
            "trades": len(trades),
            "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_pnl / initial_capital * 100, 2),
            "avg_win": round(sum(t["net_pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t["net_pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "cost_note": f"Costs: {COST_PER_SIDE * 100:.1f}% per side (estimate — real brokerage/STT/GST differ)",
            "drawdown_note": "Max drawdown computed on closed-trade equity curve",
        }

    @staticmethod
    def _empty_metrics() -> dict:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate_pct": 0.0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown_pct": 0.0,
            "cost_note": f"Costs: {COST_PER_SIDE * 100:.1f}% per side (estimate — real brokerage/STT/GST differ)",
            "drawdown_note": "Max drawdown computed on closed-trade equity curve",
        }
