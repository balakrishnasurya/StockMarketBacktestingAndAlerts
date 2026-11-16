"""Shared engine for the 5-minute intraday BULLISH strategies (3-candle, VWAP and hybrids).

Conventions used by every strategy file:
- Candles are 5-minute bars labelled by their START time in IST (09:15 = the 09:15-09:20 candle).
- P&L is reported in R: 1R = (entry - initial stop) x quantity of the first (base) leg.
- Fills are conservative: if a stop and a target sit inside the same candle, the stop is assumed first.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
EPS = 1e-9


# ----------------------------------------------------------------------------- market data


@dataclass(frozen=True, slots=True)
class Bar:
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float

    @property
    def t(self) -> time:
        return self.ts.time()

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case columns, build a naive IST `datetime` column and keep the regular session only."""
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    if "datetime" not in out.columns:
        if {"date", "time"} <= set(out.columns):
            out["datetime"] = out["date"].astype(str) + " " + out["time"].astype(str)
        else:
            for alt in ("timestamp", "date_time", "date"):
                if alt in out.columns:
                    out = out.rename(columns={alt: "datetime"})
                    break
    missing = {"datetime", "open", "high", "low", "close"} - set(out.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    ts = pd.to_datetime(out["datetime"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out["datetime"] = ts
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out[["datetime", "open", "high", "low", "close", "volume"]]
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out.sort_values("datetime").drop_duplicates("datetime")
    clock = out["datetime"].dt.time
    out = out[(clock >= SESSION_OPEN) & (clock < SESSION_CLOSE)]
    return out.reset_index(drop=True)


def ensure_5min(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1/2/3-minute data to 5-minute candles aligned to 09:15."""
    step = df["datetime"].diff().dropna().median()
    if pd.isna(step) or step == pd.Timedelta(minutes=5):
        return df
    if step > pd.Timedelta(minutes=5):
        raise ValueError(f"data interval is {step}; these strategies are defined on 5-minute candles")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.set_index("datetime").resample("5min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"]).reset_index()


def add_session_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Session-anchored VWAP on typical price (H+L+C)/3 - the broker's default VWAP settings."""
    out = df.copy()
    day = out["datetime"].dt.date
    tp = (out["high"] + out["low"] + out["close"]) / 3.0
    vol = out["volume"].fillna(0.0).clip(lower=0.0)
    cum_v = vol.groupby(day).cumsum()
    vwap = (tp * vol).groupby(day).cumsum() / cum_v.where(cum_v > 0)
    # Feeds without volume fall back to the running mean of the typical price.
    fallback = tp.groupby(day).cumsum() / (tp.groupby(day).cumcount() + 1)
    out["vwap"] = vwap.fillna(fallback)
    return out


def load_ohlcv_csv(path: str | Path) -> pd.DataFrame:
    return normalize_ohlcv(pd.read_csv(path))


def fetch_yfinance(symbol: str, period: str = "60d") -> pd.DataFrame:
    """Last ~60 days of 5-minute NSE candles from Yahoo, e.g. symbol='TATAELXSI.NS'."""
    import yfinance as yf  # optional dependency

    raw = yf.download(symbol, period=period, interval="5m", auto_adjust=False, progress=False)
    if raw.empty:
        raise ValueError(f"no data returned for {symbol}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return normalize_ohlcv(raw.reset_index())


def synthetic_5min(days: int = 40, seed: int = 7, start_price: float = 1500.0) -> pd.DataFrame:
    """Random-walk candles with a per-day drift - only for smoke-testing the pipeline."""
    rng = np.random.default_rng(seed)
    rows = []
    price = start_price
    for day in pd.bdate_range("2026-01-05", periods=days):
        price *= 1 + rng.normal(0, 0.004)
        drift = rng.normal(0, 0.00015)
        for k in range(75):
            ts = day + pd.Timedelta(minutes=9 * 60 + 15 + 5 * k)
            o = price
            c = o * (1 + drift + rng.normal(0, 0.0022))
            hi = max(o, c) * (1 + abs(rng.normal(0, 0.001)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.001)))
            rows.append((ts, o, hi, lo, c, float(rng.integers(5_000, 60_000))))
            price = c
    return pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])


# ----------------------------------------------------------------------------- parameters


@dataclass(frozen=True, slots=True)
class Trigger:
    """A setup whose entry level was traded through on candle `index`."""

    source: str  # "3C" or "VWAP"
    index: int
    price: float  # buy-stop level
    stops: tuple[float, ...]  # candidate stop levels, preferred first
    setup_no: int


@dataclass(frozen=True)
class ExitPlan:
    targets_r: tuple[tuple[float, float], ...] = ((1.5, 0.5),)  # (R multiple, fraction of leg qty)
    lock_r: float = 0.25  # once a leg's first target fills: stop -> entry + lock_r * R
    trail: bool = True
    trail_min_pullback_bars: int = 2  # a swing needs this many candles below the last high
    trail_min_pullback_r: float = 0.0  # ...and at least this depth (in R) to move the stop
    vwap_floor: bool = False  # after T1: exit the runner on a candle close back below VWAP
    buffer_pct: float = 0.001  # placed under swing lows when trailing
    exit_time: time = time(14, 55)


@dataclass(frozen=True)
class RiskParams:
    min_risk_pct: float = 0.0025  # tighter stops are widened to this (noise buffer)
    max_risk_pct: float = 0.02  # wider stops -> next candidate, else skip the trade
    max_chase_pct: float = 0.002  # skip if the candle opens this far above the trigger
    last_entry_time: time = time(14, 30)
    cost_pct_per_side: float = 0.0003


@dataclass(frozen=True)
class DayFilter:
    min_price: float = 100.0
    max_gap_up_pct: float = 0.05

    def allows(self, bars: Sequence[Bar], prev_close: float | None) -> bool:
        first = bars[0]
        if first.open < self.min_price:
            return False
        return prev_close is None or first.open / prev_close - 1 <= self.max_gap_up_pct


def stop_entry_fill(bar: Bar, trigger: float, max_chase_pct: float) -> float | None:
    """Buy-stop fill: at the trigger, or at the open if the candle gapped over it (unless too far)."""
    if bar.high < trigger:
        return None
    if bar.open >= trigger:
        return bar.open if bar.open <= trigger * (1 + max_chase_pct) else None
    return trigger


def pick_stop(entry: float, candidates: Iterable[float], risk: RiskParams) -> float | None:
    """First candidate whose risk fits max_risk_pct; very tight stops are widened to min_risk_pct."""
    for stop in candidates:
        if not math.isfinite(stop) or stop >= entry:
            continue
        stop = min(stop, entry * (1 - risk.min_risk_pct))
        if (entry - stop) / entry <= risk.max_risk_pct:
            return stop
    return None


# ----------------------------------------------------------------------------- position


@dataclass
class Fill:
    ts: pd.Timestamp
    price: float
    qty: float  # + buy, - sell
    reason: str


@dataclass
class Leg:
    tag: str
    entry_ts: pd.Timestamp
    entry: float
    qty: float
    risk: float
    open_qty: float
    targets: list[list[float]] = field(default_factory=list)  # [price, qty] ascending
    targets_filled: int = 0

    @property
    def first_target_done(self) -> bool:
        return self.targets_filled > 0


class Position:
    """One long position (base leg + optional pyramid legs) sharing a single stop."""

    def __init__(self, symbol: str, plan: ExitPlan, cost_pct: float) -> None:
        self.symbol = symbol
        self.plan = plan
        self.cost_pct = cost_pct
        self.legs: list[Leg] = []
        self.fills: list[Fill] = []
        self.sl = math.nan
        self.initial_sl = math.nan
        self.trail_active = False
        self.closed = False
        self.max_high = -math.inf
        self.notes: list[str] = []
        self._ref_high: float | None = None
        self._pb_low: float | None = None
        self._pb_bars = 0
        self._seen_above_vwap = False

    @property
    def base(self) -> Leg:
        return self.legs[0]

    @property
    def open_qty(self) -> float:
        return sum(leg.open_qty for leg in self.legs)

    def add_leg(
        self,
        tag: str,
        ts: pd.Timestamp,
        price: float,
        qty: float,
        stop: float,
        targets_r: tuple[tuple[float, float], ...] | None = None,
    ) -> Leg:
        risk = price - stop
        if risk <= 0:
            raise ValueError("stop must be below the entry price")
        plan_targets = self.plan.targets_r if targets_r is None else targets_r
        targets = [[price + r * risk, qty * f] for r, f in sorted(plan_targets)]
        leg = Leg(tag, ts, price, qty, risk, qty, targets)
        if not self.legs:
            self.sl = self.initial_sl = stop
            self.max_high = price
        else:
            self.sl = max(self.sl, stop)
        self.legs.append(leg)
        self.fills.append(Fill(ts, price, qty, f"entry:{tag}"))
        return leg

    def set_targets(self, leg: Leg, targets: Iterable[tuple[float, float]]) -> None:
        leg.targets = sorted([p, q] for p, q in targets if q > EPS)

    def raise_stop(self, price: float) -> None:
        self.sl = max(self.sl, price)

    def process_bar(self, bar: Bar, entry_bar: bool = False) -> None:
        if self.closed:
            return
        if not entry_bar:
            if bar.t >= self.plan.exit_time:
                self.exit_all(bar.ts, bar.open, "time_exit")
                return
            if bar.open <= self.sl:
                self.exit_all(bar.ts, bar.open, self._stop_reason())
                return
            if bar.low <= self.sl:
                self.exit_all(bar.ts, self.sl, self._stop_reason())
                return
        elif bar.close <= self.sl:
            # On the entry candle only a close under the stop proves it traded there after the fill.
            self.exit_all(bar.ts, self.sl, "stop")
            return
        self.max_high = max(self.max_high, bar.high)
        self._fill_targets(bar, entry_bar)
        if self.closed:
            return
        if bar.close > bar.vwap:
            self._seen_above_vwap = True
        elif self.plan.vwap_floor and self.trail_active and self._seen_above_vwap and bar.close < bar.vwap:
            self.exit_all(bar.ts, bar.close, "vwap_floor")
            return
        self._update_trail(bar)

    def exit_all(self, ts: pd.Timestamp, price: float, reason: str) -> None:
        for leg in self.legs:
            if leg.open_qty > EPS:
                self._sell(leg, ts, price, leg.open_qty, reason)
        self.closed = True

    def _fill_targets(self, bar: Bar, entry_bar: bool) -> None:
        for leg in self.legs:
            while leg.targets and leg.open_qty > EPS and bar.high >= leg.targets[0][0]:
                price, qty = leg.targets.pop(0)
                fill = price if entry_bar else max(price, bar.open)
                leg.targets_filled += 1
                self._sell(leg, bar.ts, fill, min(qty, leg.open_qty), f"{leg.tag}:T{leg.targets_filled}")
                if leg.targets_filled == 1:
                    self.trail_active = True
                    self.sl = max(self.sl, leg.entry + self.plan.lock_r * leg.risk)
        if self.open_qty <= EPS:
            self.closed = True

    def _update_trail(self, bar: Bar) -> None:
        """Stop moves only when price breaks the last swing high, to just under the pullback low."""
        if self._ref_high is None:
            self._ref_high = bar.high
            return
        if bar.high > self._ref_high:
            swing_low = bar.low if self._pb_low is None else min(self._pb_low, bar.low)
            p = self.plan
            if (
                p.trail
                and self.trail_active
                and self._pb_bars >= p.trail_min_pullback_bars
                and self._ref_high - swing_low >= p.trail_min_pullback_r * self.base.risk
            ):
                self.sl = max(self.sl, swing_low * (1 - p.buffer_pct))
            self._ref_high = bar.high
            self._pb_low, self._pb_bars = None, 0
        else:
            self._pb_bars += 1
            self._pb_low = bar.low if self._pb_low is None else min(self._pb_low, bar.low)

    def _sell(self, leg: Leg, ts: pd.Timestamp, price: float, qty: float, reason: str) -> None:
        leg.open_qty -= qty
        self.fills.append(Fill(ts, price, -qty, reason))

    def _stop_reason(self) -> str:
        return "trail_stop" if self.trail_active else "stop"


@dataclass
class TradeRecord:
    strategy: str
    symbol: str
    day: date
    setup: str
    entry_ts: pd.Timestamp
    entry: float
    initial_sl: float
    risk_pct: float
    exit_ts: pd.Timestamp
    legs: int
    gross_r: float
    cost_r: float
    net_r: float
    mfe_r: float
    exits: str
    notes: str

    @classmethod
    def from_position(cls, pos: Position, strategy: str, day: date) -> TradeRecord:
        base = pos.base
        unit_risk = base.qty * base.risk
        pnl = -sum(f.price * f.qty for f in pos.fills)
        cost = sum(abs(f.qty) * f.price for f in pos.fills) * pos.cost_pct
        exits = [f for f in pos.fills if f.qty < 0]
        return cls(
            strategy=strategy,
            symbol=pos.symbol,
            day=day,
            setup=base.tag,
            entry_ts=base.entry_ts,
            entry=base.entry,
            initial_sl=pos.initial_sl,
            risk_pct=100 * base.risk / base.entry,
            exit_ts=exits[-1].ts,
            legs=len(pos.legs),
            gross_r=pnl / unit_risk,
            cost_r=cost / unit_risk,
            net_r=(pnl - cost) / unit_risk,
            mfe_r=(pos.max_high - base.entry) / base.risk,
            exits=" | ".join(f"{f.reason}@{f.price:.2f}x{-f.qty:.2f}" for f in exits),
            notes="; ".join(pos.notes),
        )


# ----------------------------------------------------------------------------- strategy driver


class BaseBullishStrategy:
    """Candle-by-candle driver: one long position at a time per symbol, flat by 14:55."""

    name = "base"

    def __init__(self, plan: ExitPlan | None = None, risk: RiskParams | None = None) -> None:
        self.plan = plan or ExitPlan()
        self.risk = risk or RiskParams()
        self.trades: list[TradeRecord] = []
        self.position: Position | None = None
        self.symbol = ""
        self.day: date | None = None

    def reset_signals(self) -> None:
        raise NotImplementedError

    def handle_bar(self, i: int, bars: Sequence[Bar], flat: bool) -> None:
        raise NotImplementedError

    def on_day_start(self, symbol: str, day: date) -> None:
        self.symbol, self.day = symbol, day
        self.position = None
        self.reset_signals()

    def on_bar(self, i: int, bars: Sequence[Bar]) -> None:
        just_exited = False
        if self.position is not None:
            self.position.process_bar(bars[i])
            if self.position.closed:
                self._record()
                just_exited = True
        self.handle_bar(i, bars, flat=self.position is None and not just_exited)

    def on_day_end(self, bars: Sequence[Bar]) -> None:
        if self.position is not None:
            self.position.exit_all(bars[-1].ts, bars[-1].close, "eod")
            self._record()

    def open_long(
        self,
        tag: str,
        bar: Bar,
        trigger: float,
        stops: Iterable[float],
        plan: ExitPlan | None = None,
    ) -> Position | None:
        if bar.t > self.risk.last_entry_time:
            return None
        fill = stop_entry_fill(bar, trigger, self.risk.max_chase_pct)
        stop = None if fill is None else pick_stop(fill, stops, self.risk)
        if fill is None or stop is None:
            return None
        pos = Position(self.symbol, plan or self.plan, self.risk.cost_pct_per_side)
        pos.add_leg(tag, bar.ts, fill, 1.0, stop)
        self.position = pos
        pos.process_bar(bar, entry_bar=True)
        if pos.closed:
            self._record()
        return pos

    def _record(self) -> None:
        if self.position is None or self.day is None:
            return
        self.trades.append(TradeRecord.from_position(self.position, self.name, self.day))
        self.position = None


def run_backtest(
    df: pd.DataFrame,
    strategy: BaseBullishStrategy,
    symbol: str = "SYMBOL",
    allowed_days: set[date] | None = None,
    day_filter: DayFilter | None = None,
) -> list[TradeRecord]:
    """Feed one symbol's history day by day; `allowed_days` = days the stock was on the bullish list."""
    data = add_session_vwap(ensure_5min(normalize_ohlcv(df)))
    day_filter = day_filter or DayFilter()
    start = len(strategy.trades)
    prev_close: float | None = None
    for day, g in data.groupby(data["datetime"].dt.date, sort=True):
        bars = [
            Bar(
                ts=r["datetime"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=float(r["volume"]),
                vwap=float(r["vwap"]),
            )
            for _, r in g.iterrows()
        ]
        if allowed_days is not None and day not in allowed_days:
            continue
        if day_filter and not day_filter.allows(bars, prev_close):
            prev_close = bars[-1].close if bars else prev_close
            continue
        strategy.on_day_start(symbol, day)
        for i, _ in enumerate(bars):
            strategy.on_bar(i, bars)
        strategy.on_day_end(bars)
        prev_close = bars[-1].close if bars else prev_close
    return strategy.trades[start:]


def cli_main(factories: dict[str, Callable[..., BaseBullishStrategy]] | None = None, doc: str | None = None, argv: list[str] | None = None) -> pd.DataFrame:
    """CLI for running the bullish strategies on CSV or Yahoo Finance data."""
    parser = argparse.ArgumentParser(description=doc or "Backtest bullish intraday strategies")
    parser.add_argument("--csv", nargs="+", default=None, help="CSV OHLCV files to backtest")
    parser.add_argument("--yf", nargs="+", default=None, help="Yahoo Finance symbols to fetch, e.g. COFORGE.NS")
    parser.add_argument("--save-dir", default=None, help="Directory to save downloaded CSVs")
    parser.add_argument("--demo", action="store_true", help="Run a synthetic demo backtest")
    parser.add_argument("--watchlist", default=None, help="Optional bullish-list CSV of symbols")
    args = parser.parse_args(argv)

    if args.demo:
        df = synthetic_5min(days=30, seed=3)
        table = []
        for name, build in (factories or {}).items():
            strat = build()
            trades = run_backtest(df, strat, symbol=name)
            total = sum(t.net_r for t in trades)
            table.append({"strategy": name, "trades": len(trades), "total_R": total, "avg_R": total / max(len(trades), 1)})
        out = pd.DataFrame(table).set_index("strategy")
        print(out)
        return out

    if args.csv:
        files = args.csv
        missing = [f for f in files if not Path(f).exists()]
        if missing:
            parser.error(f"file(s) not found: {', '.join(missing)}")
        dfs = [load_ohlcv_csv(f) for f in files]
        combined = pd.concat(dfs, ignore_index=True)
        strategy = (next(iter((factories or {}).values()))())
        trades = run_backtest(combined, strategy, symbol="COMBINED")
        print(pd.DataFrame([t.__dict__ for t in trades]))
        return pd.DataFrame([t.__dict__ for t in trades])

    if args.yf:
        if not factories:
            raise ValueError("factories required for --yf runs")
        save_dir = Path(args.save_dir) if args.save_dir else None
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
        merged = []
        for sym in args.yf:
            df = fetch_yfinance(sym, period="60d")
            merged.append(df)
            if save_dir is not None:
                out_path = save_dir / f"{sym.split('.')[0]}.csv"
                df.to_csv(out_path, index=False)
        combined = pd.concat(merged, ignore_index=True)
        for name, build in factories.items():
            strat = build()
            trades = run_backtest(combined, strat, symbol=name)
            print(f"{name}: {len(trades)} trades, total_R={sum(t.net_r for t in trades):.2f}")
        return pd.DataFrame([t.__dict__ for t in trades])

    parser.print_help()
    return pd.DataFrame()
