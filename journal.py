"""
Trade Journal & Outcome Tracking
================================
Strategy-agnostic daily setup log with hypothetical intraday outcome tracking.

For every triggered setup found during a session it records:
  - entry time / price, stop loss, TP1, TP2
  - whether TP1 / TP2 / SL was hit first (conservative: SL wins a same-bar tie)
  - maximum favourable / adverse excursion
  - end-of-day close and whether the stock finished above or below entry
  - hypothetical gross / net P&L using a fixed notional per trade

The journal is persisted to ``state/trade_journal.json`` and is shared by the
Alerts page, the Trade Journal dashboard and the ``journal_sync.py`` CLI.

Assumptions (documented in the UI as well):
  - 50% booked at TP1, stop moved to entry + 0.15%.
  - 25% booked at TP2 when the strategy defines one.
  - Remaining quantity trails at break-even and exits at square-off / EOD.
  - Square-off defaults to 14:55 IST (the 3-Candle strategy's own rule).
  - Costs are estimated at COST_PER_SIDE (0.1%) per side.
  - When a single candle touches both SL and TP, the SL is assumed first.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable, Optional

import pandas as pd

from config import COST_PER_SIDE, DEFAULT_NOTIONAL, IST, JOURNAL_FILE
from strategies import get_display_name, get_strategy_param, load_strategy

POST_TP1_LOCK_PCT = 0.0015
TP1_BOOK_FRACTION = 0.50
TP2_BOOK_FRACTION = 0.25
DEFAULT_SQUARE_OFF = time(14, 55)
SESSION_END = time(15, 30)
EOD_FLAT_TOL_PCT = 0.0005

OUTCOME_FIELDS = (
    "status",
    "settled",
    "outcome",
    "outcome_label",
    "result",
    "tp1_hit",
    "tp2_hit",
    "sl_hit",
    "ambiguous",
    "exit_time",
    "exit_price",
    "exit_reason",
    "gross_pnl",
    "costs",
    "net_pnl",
    "return_pct",
    "mfe",
    "mae",
    "mfe_pct",
    "mae_pct",
    "eod_close",
    "eod_zone",
    "eod_move_pct",
    "last_updated",
)


# ── Small helpers ─────────────────────────────────────────────────────────

def _as_ts(value) -> Optional[pd.Timestamp]:
    """Parse a timestamp/string and normalise it to IST."""
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if ts is pd.NaT:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)
    return ts


def _iso(value) -> Optional[str]:
    ts = _as_ts(value)
    return ts.isoformat() if ts is not None else None


def _minute_key(value) -> str:
    ts = _as_ts(value)
    if ts is None:
        return str(value or "")
    return ts.strftime("%Y-%m-%dT%H:%M")


def _f(value) -> Optional[float]:
    """Convert numpy / pandas numbers to plain rounded floats."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return round(number, 4)


def _ensure_timestamp_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    out = df.copy()
    if "timestamp" not in out.columns:
        if isinstance(out.index, pd.DatetimeIndex):
            out = out.reset_index()
            out = out.rename(columns={out.columns[0]: "timestamp"})
        else:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    if out["timestamp"].dt.tz is None:
        out["timestamp"] = out["timestamp"].dt.tz_localize(IST)
    else:
        out["timestamp"] = out["timestamp"].dt.tz_convert(IST)
    rename = {
        c: str(c).lower()
        for c in out.columns
        if str(c).lower() in ("open", "high", "low", "close", "volume")
    }
    out = out.rename(columns=rename)
    return out.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def _day_bars(df: pd.DataFrame, day: date) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    out = _ensure_timestamp_column(df)
    if out.empty:
        return out
    return out.loc[out["timestamp"].dt.date == day].reset_index(drop=True)


# ── Persistence ───────────────────────────────────────────────────────────

def load_journal() -> list[dict]:
    """Load journal entries (newest first). Corrupt/missing file -> []."""
    if not JOURNAL_FILE.exists():
        return []
    try:
        data = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("entries", [])
        entries = [e for e in data if isinstance(e, dict)]
        entries.sort(
            key=lambda e: (str(e.get("date", "")), str(e.get("entry_time", ""))),
            reverse=True,
        )
        return entries
    except Exception:
        return []


def save_journal(entries: Iterable[dict]) -> None:
    """Atomically persist the journal, newest first."""
    entries = list(entries)
    entries.sort(
        key=lambda e: (str(e.get("date", "")), str(e.get("entry_time", ""))),
        reverse=True,
    )
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOURNAL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
    tmp.replace(JOURNAL_FILE)


def entry_id(rec: dict) -> str:
    return "|".join(
        [
            str(rec.get("date") or ""),
            str(rec.get("symbol") or ""),
            str(rec.get("strategy") or ""),
            str(rec.get("side") or ""),
            _minute_key(rec.get("entry_time")),
        ]
    )


def _merge_records(old: dict, new: dict) -> dict:
    """Merge a refreshed record into an existing one.

    A pending scan record must never wipe a settled result; a settled sync
    record may replace an older settled record (e.g. after a data correction).
    """
    merged = {**old, **new}
    if old.get("settled") and not new.get("settled"):
        for key in OUTCOME_FIELDS:
            if key in old:
                merged[key] = old[key]
    return merged


def upsert_entries(
    new_entries: Iterable[dict], existing: Optional[list[dict]] = None
) -> tuple[list[dict], int, int]:
    """Insert/update entries by id. Returns (entries, added, updated)."""
    entries = list(existing if existing is not None else load_journal())
    by_id = {e.get("id") or entry_id(e): i for i, e in enumerate(entries)}
    # Secondary index for fuzzy matching when a scan timestamp differs slightly.
    by_setup: dict[tuple, list[int]] = {}
    for i, e in enumerate(entries):
        key = (e.get("date"), e.get("symbol"), e.get("strategy"), e.get("side"))
        by_setup.setdefault(key, []).append(i)

    added = updated = 0
    for rec in new_entries:
        rec = dict(rec)
        rec["id"] = entry_id(rec)
        idx = by_id.get(rec["id"])
        if idx is None:
            key = (rec.get("date"), rec.get("symbol"), rec.get("strategy"), rec.get("side"))
            rec_ts = _as_ts(rec.get("entry_time"))
            rec_entry = _f(rec.get("entry_price"))
            for candidate in by_setup.get(key, []):
                old = entries[candidate]
                old_ts = _as_ts(old.get("entry_time"))
                if rec_ts is not None and old_ts is not None:
                    # Only bridge formatting/timezone differences, never two
                    # genuinely different 5-minute setups.
                    if abs((rec_ts - old_ts).total_seconds()) <= 120:
                        idx = candidate
                        break
                else:
                    old_entry = _f(old.get("entry_price"))
                    if (
                        rec_entry is not None
                        and old_entry is not None
                        and abs(rec_entry - old_entry) <= max(0.05, old_entry * 0.001)
                    ):
                        idx = candidate
                        break

        if idx is None:
            entries.append(rec)
            by_id[rec["id"]] = len(entries) - 1
            key = (rec.get("date"), rec.get("symbol"), rec.get("strategy"), rec.get("side"))
            by_setup.setdefault(key, []).append(len(entries) - 1)
            added += 1
        else:
            entries[idx] = _merge_records(entries[idx], rec)
            by_id[entries[idx].get("id") or rec["id"]] = idx
            updated += 1

    return entries, added, updated


# ── Setup construction ────────────────────────────────────────────────────

def _raw_record(
    *,
    day: date,
    symbol: str,
    strategy_id: str,
    unit: str,
    interval: int,
    side: str,
    entry_time,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    tp1: Optional[float],
    tp2: Optional[float],
    rr: Optional[str],
    source: str,
    notional: float,
) -> dict:
    entry = _f(entry_price)
    qty = max(1, int(notional / entry)) if entry and entry > 0 else 1
    return {
        "id": "",
        "date": day.isoformat(),
        "symbol": symbol,
        "strategy": strategy_id,
        "strategy_name": get_display_name(strategy_id),
        "unit": unit,
        "interval": int(interval),
        "side": side.upper(),
        "entry_time": _iso(entry_time),
        "entry_price": entry,
        "stop_loss": _f(stop_loss),
        "tp1": _f(tp1),
        "tp2": _f(tp2),
        "rr": rr,
        "qty": qty,
        "notional": round(qty * entry, 2) if entry else round(float(notional), 2),
        "source": source,
        "status": "PENDING",
        "settled": False,
        "outcome": "PENDING",
        "outcome_label": "Awaiting result",
        "result": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "sl_hit": False,
        "ambiguous": False,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "gross_pnl": None,
        "costs": None,
        "net_pnl": None,
        "return_pct": None,
        "mfe": None,
        "mae": None,
        "mfe_pct": None,
        "mae_pct": None,
        "eod_close": None,
        "eod_zone": None,
        "eod_move_pct": None,
        "last_updated": None,
    }


def _build_from_state_machine(
    module,
    df: pd.DataFrame,
    symbol: str,
    strategy_id: str,
    day: date,
    unit: str,
    interval: int,
    notional: float,
    source: str,
) -> list[dict]:
    """Use a strategy's get_recent_trades() to recover every trigger for a day."""
    getter = getattr(module, "get_recent_trades", None)
    if getter is None:
        return []

    df_up = df.loc[df["timestamp"].dt.date <= day]
    if df_up.empty:
        return []

    mode = "SWING_DAILY" if unit in ("days", "weeks", "months") else "INTRADAY_5M"
    trades = None
    try:
        trades = getter(df_up, mode=mode)
    except TypeError:
        try:
            trades = getter(df_up)
        except Exception:
            trades = None
    except Exception:
        trades = None
    if not trades:
        return []

    records: list[dict] = []
    for trade in trades:
        ts = _as_ts(trade.get("entry_time"))
        if ts is None or ts.date() != day:
            continue
        side = str(trade.get("direction") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        entry = _f(trade.get("entry_price"))
        if entry is None:
            continue
        records.append(
            _raw_record(
                day=day,
                symbol=symbol,
                strategy_id=strategy_id,
                unit=unit,
                interval=interval,
                side=side,
                entry_time=ts,
                entry_price=entry,
                stop_loss=_f(trade.get("stop_loss")),
                tp1=_f(trade.get("tp1")),
                tp2=_f(trade.get("tp2")),
                rr=trade.get("rr") or ("1:1.5" if trade.get("tp2") is None else "1:1.5 / 1:3"),
                source=source,
                notional=notional,
            )
        )
    return records


def _build_from_signal(
    module,
    df: pd.DataFrame,
    symbol: str,
    strategy_id: str,
    day: date,
    unit: str,
    interval: int,
    notional: float,
    source: str,
) -> list[dict]:
    """Replay a simple signal() strategy candle by candle for one session."""
    day_positions = df.index[df["timestamp"].dt.date == day].tolist()
    if not day_positions:
        return []

    min_candles = int(get_strategy_param(strategy_id, "MIN_CANDLES", 1) or 1)
    mode = "SWING_DAILY" if unit in ("days", "weeks", "months") else "INTRADAY_5M"
    records: list[dict] = []

    for pos in day_positions:
        sub = df.iloc[: pos + 1]
        if len(sub) < min_candles:
            continue
        try:
            sig = module.signal(sub)
        except Exception:
            continue
        if sig not in ("BUY", "SELL"):
            continue
        close = _f(sub.iloc[-1].get("close"))
        if close is None:
            continue
        try:
            levels = _levels_for(module, sub, sig, close, mode)
        except Exception:
            levels = {}
        entry = _f(levels.get("entry_price")) or close
        records.append(
            _raw_record(
                day=day,
                symbol=symbol,
                strategy_id=strategy_id,
                unit=unit,
                interval=interval,
                side=sig,
                entry_time=sub.iloc[-1]["timestamp"],
                entry_price=entry,
                stop_loss=_f(levels.get("stop_loss")),
                tp1=_f(levels.get("tp1")),
                tp2=_f(levels.get("tp2")),
                rr=levels.get("rr"),
                source=source,
                notional=notional,
            )
        )
    return records


def _levels_for(module, df, sig, last_price, mode):
    from scanner import get_strategy_trade_levels

    return get_strategy_trade_levels(module, df, sig, last_price, mode=mode)


def build_setups_for_date(
    df: pd.DataFrame,
    symbol: str,
    strategy_id: str,
    day: date,
    unit: str,
    interval: int,
    notional: float = DEFAULT_NOTIONAL,
    source: str = "sync",
) -> list[dict]:
    """Return raw journal records for every triggered setup on ``day``."""
    df = _ensure_timestamp_column(df)
    if df.empty:
        return []
    module = load_strategy(strategy_id)
    try:
        if hasattr(module, "get_recent_trades"):
            return _build_from_state_machine(
                module, df, symbol, strategy_id, day, unit, interval, notional, source
            )
        return _build_from_signal(
            module, df, symbol, strategy_id, day, unit, interval, notional, source
        )
    except Exception:
        return []


# ── Outcome evaluation ────────────────────────────────────────────────────

def _outcome_for(
    tp1_hit: bool, tp2_hit: bool, sl_hit: bool, gross: float, exit_reason: str
) -> tuple[str, str]:
    if tp2_hit:
        return "TP2_HIT", "TP2 hit"
    if tp1_hit:
        if exit_reason == "TRAILING_SL":
            return "TP1_TRAIL", "TP1 hit · trailed out"
        if exit_reason in ("SQUARE_OFF", "EOD_CLOSE"):
            return "TP1_HIT", "TP1 hit · squared off"
        return "TP1_HIT", "TP1 hit"
    if sl_hit:
        return "SL_HIT", "SL hit"
    if gross > 0:
        return "EOD_PROFIT", "Closed in profit"
    if gross < 0:
        return "EOD_LOSS", "Closed in loss"
    return "FLAT", "Flat"


def evaluate_entry(
    rec: dict,
    bars: pd.DataFrame,
    *,
    notional: float = DEFAULT_NOTIONAL,
    square_off: time = DEFAULT_SQUARE_OFF,
    now: Optional[datetime] = None,
) -> dict:
    """Fill outcome/P&L fields on a journal record from the session's candles."""
    if now is None:
        now = datetime.now(IST)
    side = str(rec.get("side") or "").upper()
    entry = _f(rec.get("entry_price"))
    sl = _f(rec.get("stop_loss"))
    tp1 = _f(rec.get("tp1"))
    tp2 = _f(rec.get("tp2"))
    entry_ts = _as_ts(rec.get("entry_time"))

    if side not in ("BUY", "SELL") or entry is None or sl is None or entry_ts is None:
        rec["status"] = "INVALID"
        rec["settled"] = False
        rec["outcome"] = "INVALID"
        rec["outcome_label"] = "Incomplete levels"
        rec["result"] = "OPEN"
        rec["last_updated"] = _iso(now)
        return rec

    qty = max(1, int(notional / entry)) if entry > 0 else int(rec.get("qty") or 1)
    rec["qty"] = qty
    rec["notional"] = round(qty * entry, 2)

    bars = _ensure_timestamp_column(bars)
    raw_day = rec.get("date")
    if isinstance(raw_day, date):
        day = raw_day
    else:
        try:
            day = date.fromisoformat(str(raw_day))
        except Exception:
            day = entry_ts.date()
    eod_bars = _day_bars(bars, day)
    track = eod_bars.loc[eod_bars["timestamp"] >= entry_ts].reset_index(drop=True)

    if track.empty:
        rec["status"] = "OPEN" if day >= now.date() else "NO_DATA"
        rec["settled"] = False
        rec["outcome"] = "PENDING" if day >= now.date() else "NO_DATA"
        rec["outcome_label"] = "Awaiting result" if day >= now.date() else "No candle data"
        rec["result"] = "OPEN"
        rec["last_updated"] = _iso(now)
        return rec

    direction = 1 if side == "BUY" else -1
    fraction = 1.0
    realized_per_share = 0.0
    tp1_hit = tp2_hit = sl_hit = ambiguous = False
    exit_time = None
    exit_price = None
    exit_reason = ""

    for row in track.itertuples(index=False):
        ts = _as_ts(getattr(row, "timestamp"))
        o = _f(getattr(row, "open"))
        h = _f(getattr(row, "high"))
        l = _f(getattr(row, "low"))
        c = _f(getattr(row, "close"))
        if ts is None or None in (o, h, l, c):
            continue

        # Strategy square-off happens before the candle's SL/TP checks.
        if ts.time() >= square_off:
            realized_per_share += direction * (c - entry) * fraction
            exit_price, exit_time, exit_reason = c, ts, "SQUARE_OFF"
            fraction = 0.0
            break

        if direction == 1:  # BUY / long
            if not tp1_hit:
                # Gap through the stop.
                if o <= sl:
                    realized_per_share += (o - entry) * fraction
                    exit_price, exit_time, exit_reason = o, ts, "SL_HIT"
                    sl_hit, fraction = True, 0.0
                    break
                if l <= sl:
                    if tp1 is not None and h >= tp1:
                        ambiguous = True
                    realized_per_share += (sl - entry) * fraction
                    exit_price, exit_time, exit_reason = sl, ts, "SL_HIT"
                    sl_hit, fraction = True, 0.0
                    break
                if tp1 is not None and h >= tp1:
                    fill = o if o > tp1 else tp1
                    book = min(TP1_BOOK_FRACTION, fraction)
                    realized_per_share += (fill - entry) * book
                    fraction -= book
                    tp1_hit = True
                    break_even = round(entry * (1.0 + POST_TP1_LOCK_PCT), 2)
                    # Conservative same-candle stop after TP1.
                    if fraction > 0 and l <= break_even:
                        realized_per_share += (break_even - entry) * fraction
                        exit_price, exit_time, exit_reason = break_even, ts, "TRAILING_SL"
                        fraction = 0.0
                        break
                    if tp2 is not None and h >= tp2:
                        fill2 = o if o > tp2 else tp2
                        book2 = min(TP2_BOOK_FRACTION, fraction)
                        realized_per_share += (fill2 - entry) * book2
                        fraction -= book2
                        tp2_hit = True
            else:
                break_even = round(entry * (1.0 + POST_TP1_LOCK_PCT), 2)
                if l <= break_even:
                    fill = o if o < break_even else break_even
                    realized_per_share += (fill - entry) * fraction
                    exit_price, exit_time, exit_reason = fill, ts, "TRAILING_SL"
                    fraction = 0.0
                    break
                if tp2 is not None and not tp2_hit and h >= tp2:
                    fill2 = o if o > tp2 else tp2
                    book2 = min(TP2_BOOK_FRACTION, fraction)
                    realized_per_share += (fill2 - entry) * book2
                    fraction -= book2
                    tp2_hit = True
        else:  # SELL / short
            if not tp1_hit:
                if o >= sl:
                    realized_per_share += (entry - o) * fraction
                    exit_price, exit_time, exit_reason = o, ts, "SL_HIT"
                    sl_hit, fraction = True, 0.0
                    break
                if h >= sl:
                    if tp1 is not None and l <= tp1:
                        ambiguous = True
                    realized_per_share += (entry - sl) * fraction
                    exit_price, exit_time, exit_reason = sl, ts, "SL_HIT"
                    sl_hit, fraction = True, 0.0
                    break
                if tp1 is not None and l <= tp1:
                    fill = o if o < tp1 else tp1
                    book = min(TP1_BOOK_FRACTION, fraction)
                    realized_per_share += (entry - fill) * book
                    fraction -= book
                    tp1_hit = True
                    break_even = round(entry * (1.0 - POST_TP1_LOCK_PCT), 2)
                    if fraction > 0 and h >= break_even:
                        realized_per_share += (entry - break_even) * fraction
                        exit_price, exit_time, exit_reason = break_even, ts, "TRAILING_SL"
                        fraction = 0.0
                        break
                    if tp2 is not None and l <= tp2:
                        fill2 = o if o < tp2 else tp2
                        book2 = min(TP2_BOOK_FRACTION, fraction)
                        realized_per_share += (entry - fill2) * book2
                        fraction -= book2
                        tp2_hit = True
            else:
                break_even = round(entry * (1.0 - POST_TP1_LOCK_PCT), 2)
                if h >= break_even:
                    fill = o if o > break_even else break_even
                    realized_per_share += (entry - fill) * fraction
                    exit_price, exit_time, exit_reason = fill, ts, "TRAILING_SL"
                    fraction = 0.0
                    break
                if tp2 is not None and not tp2_hit and l <= tp2:
                    fill2 = o if o < tp2 else tp2
                    book2 = min(TP2_BOOK_FRACTION, fraction)
                    realized_per_share += (entry - fill2) * book2
                    fraction -= book2
                    tp2_hit = True

    if fraction > 0:
        last = track.iloc[-1]
        close = _f(last.get("close"))
        if close is not None:
            realized_per_share += direction * (close - entry) * fraction
            exit_price, exit_time = close, _as_ts(last["timestamp"])
            exit_reason = exit_reason or "EOD_CLOSE"
            fraction = 0.0

    gross = realized_per_share * qty
    costs = round((qty * entry) * COST_PER_SIDE * 2.0, 2)
    net = gross - costs
    avg_exit = entry + direction * realized_per_share

    highs = pd.to_numeric(track["high"], errors="coerce")
    lows = pd.to_numeric(track["low"], errors="coerce")
    mfe_per_share = (
        float(highs.max()) - entry if direction == 1 else entry - float(lows.min())
    )
    mae_per_share = (
        entry - float(lows.min()) if direction == 1 else float(highs.max()) - entry
    )

    eod_close = _f(eod_bars.iloc[-1].get("close"))
    if eod_close is not None:
        move = direction * (eod_close - entry)
        if move > entry * EOD_FLAT_TOL_PCT:
            eod_zone = "PROFIT"
        elif move < -entry * EOD_FLAT_TOL_PCT:
            eod_zone = "LOSS"
        else:
            eod_zone = "FLAT"
        eod_move_pct = round(move / entry * 100.0, 2)
    else:
        eod_zone, eod_move_pct = None, None

    outcome, label = _outcome_for(tp1_hit, tp2_hit, sl_hit, gross, exit_reason)
    settled = day < now.date() or (day == now.date() and now.time() >= SESSION_END)

    rec.update(
        {
            "status": "CLOSED" if settled else "OPEN",
            "settled": bool(settled),
            "outcome": outcome,
            "outcome_label": label,
            "result": "WIN" if gross > 0 else ("LOSS" if gross < 0 else "FLAT"),
            "tp1_hit": bool(tp1_hit),
            "tp2_hit": bool(tp2_hit),
            "sl_hit": bool(sl_hit),
            "ambiguous": bool(ambiguous),
            "exit_time": _iso(exit_time),
            "exit_price": _f(avg_exit),
            "exit_reason": exit_reason or None,
            "gross_pnl": round(gross, 2),
            "costs": costs,
            "net_pnl": round(net, 2),
            "return_pct": round(net / rec["notional"] * 100.0, 2) if rec.get("notional") else None,
            "mfe": round(mfe_per_share, 2),
            "mae": round(mae_per_share, 2),
            "mfe_pct": round(mfe_per_share / entry * 100.0, 2) if entry else None,
            "mae_pct": round(mae_per_share / entry * 100.0, 2) if entry else None,
            "eod_close": eod_close,
            "eod_zone": eod_zone,
            "eod_move_pct": eod_move_pct,
            "last_updated": _iso(now),
        }
    )
    return rec


# ── Journal summaries ─────────────────────────────────────────────────────

JOURNAL_TARGET_OUTCOMES = ("TP1_HIT", "TP2_HIT", "TP1_TRAIL")


def summary_stats(entries: Iterable[dict]) -> dict:
    """Aggregate a list of journal entries into headline performance stats.

    Shared by the Trade Journal dashboard and the multi-strategy tracker so
    both report identical numbers.
    """
    entries = list(entries)
    settled = [e for e in entries if e.get("settled")]
    targets = [e for e in entries if e.get("outcome") in JOURNAL_TARGET_OUTCOMES]
    stops = [e for e in entries if e.get("outcome") == "SL_HIT"]
    wins = [e for e in entries if (e.get("net_pnl") or 0) > 0]
    losses = [e for e in entries if (e.get("net_pnl") or 0) < 0]
    gross = sum(float(e.get("gross_pnl") or 0) for e in entries)
    costs = sum(float(e.get("costs") or 0) for e in entries)
    net = sum(float(e.get("net_pnl") or 0) for e in entries)
    win_pnl = sum(float(e.get("net_pnl") or 0) for e in wins)
    loss_pnl = abs(sum(float(e.get("net_pnl") or 0) for e in losses))
    return {
        "count": len(entries),
        "settled": len(settled),
        "open": len(entries) - len(settled),
        "targets": len(targets),
        "stops": len(stops),
        "wins": len(wins),
        "losses": len(losses),
        "gross": gross,
        "costs": costs,
        "net": net,
        "avg": net / len(entries) if entries else 0.0,
        "win_rate": (len(wins) / (len(wins) + len(losses)) * 100.0) if (wins or losses) else 0.0,
        "tp_rate": (len(targets) / len(entries) * 100.0) if entries else 0.0,
        "profit_factor": (win_pnl / loss_pnl) if loss_pnl > 0 else (float("inf") if win_pnl > 0 else 0.0),
    }


# ── Scanner integration ───────────────────────────────────────────────────

def _records_from_scan_results(
    results: Iterable[dict],
    scan_time: datetime,
    strategy_id: str,
    unit: str,
    interval: int,
    notional: float,
) -> list[dict]:
    """Convert NEW scan alerts for one strategy into pending journal records."""
    records: list[dict] = []
    for result in results:
        if result.get("status") != "NEW":
            continue
        side = str(result.get("side") or "").upper()
        entry = _f(result.get("entry_price"))
        if side not in ("BUY", "SELL") or entry is None:
            continue
        ts = _as_ts(result.get("candle_time")) or _as_ts(scan_time)
        try:
            day = ts.date()
        except Exception:
            continue
        records.append(
            _raw_record(
                day=day,
                symbol=str(result.get("symbol") or "").upper(),
                strategy_id=strategy_id,
                unit=unit,
                interval=interval,
                side=side,
                entry_time=ts,
                entry_price=entry,
                stop_loss=_f(result.get("stop_loss")),
                tp1=_f(result.get("tp1")),
                tp2=_f(result.get("tp2")),
                rr=result.get("rr"),
                source="scan",
                notional=notional,
            )
        )
    return records


def record_scan_alerts(
    results: Iterable[dict],
    scan_time: datetime,
    strategy_id: str,
    unit: str,
    interval: int,
    notional: float = DEFAULT_NOTIONAL,
) -> tuple[int, int]:
    """Add NEW scan alerts to the journal immediately (pending outcomes)."""
    records = _records_from_scan_results(
        results, scan_time, strategy_id, unit, interval, notional
    )
    if not records:
        return 0, 0
    entries, added, updated = upsert_entries(records)
    save_journal(entries)
    return added, updated


def record_multi_scan_alerts(
    results_by_strategy: dict[str, Iterable[dict]],
    scan_time: datetime,
    unit: str,
    interval: int,
    notional: float = DEFAULT_NOTIONAL,
) -> tuple[int, int]:
    """Log NEW alerts for many strategies in one atomic journal write.

    ``results_by_strategy`` maps a strategy id to its scan results, so a single
    scan cycle that evaluates the 3-Candle, VWAP and H1–H5 engines can be
    persisted together without re-reading the journal for each strategy.
    """
    records: list[dict] = []
    for strategy_id, results in results_by_strategy.items():
        records.extend(
            _records_from_scan_results(
                results, scan_time, strategy_id, unit, interval, notional
            )
        )
    if not records:
        return 0, 0
    entries, added, updated = upsert_entries(records)
    save_journal(entries)
    return added, updated


# ── Data loading / sync ───────────────────────────────────────────────────

def _empty_candles() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def _warmup_days(unit: str, interval: int) -> int:
    if unit == "minutes":
        return 10
    if unit == "hours":
        return 40
    if unit == "days":
        return 400
    return 800


def load_candles_for_range(
    symbol: str,
    instrument_key: str,
    dates: Iterable[date],
    unit: str,
    interval: int,
    *,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    no_cache: bool = False,
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """Load candles covering ``dates`` plus enough warm-up history."""
    from data_loader import fetch_historical, fetch_intraday

    fetch_hist = fetch_historical_fn or fetch_historical
    fetch_intra = fetch_intraday_fn or fetch_intraday
    if now is None:
        now = datetime.now(IST)

    dates = sorted(set(dates))
    if not dates:
        return _empty_candles()

    start = dates[0] - timedelta(days=_warmup_days(unit, interval))
    end = dates[-1]
    today = now.date()

    frames: list[pd.DataFrame] = []
    hist_end = min(end, today - timedelta(days=1))
    try:
        if start <= hist_end:
            frames.append(
                fetch_hist(
                    instrument_key, symbol, unit, int(interval), start, hist_end,
                    no_cache=no_cache,
                )
            )
    except TypeError:
        # Older/simpler fetch callables used in tests.
        frames.append(fetch_hist(instrument_key, symbol, unit, int(interval), start, hist_end))
    except Exception:
        pass

    if end >= today:
        try:
            frames.append(fetch_intra(instrument_key, symbol, unit, int(interval)))
        except Exception:
            pass

    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return _empty_candles()

    df = pd.concat(frames)
    df = _ensure_timestamp_column(df)

    # Drop today's still-forming candle so outcomes are not judged on partial data.
    if not df.empty and df.iloc[-1]["timestamp"].date() == today:
        bar_minutes = int(interval) if unit == "minutes" else int(interval) * 60
        expected_end = df.iloc[-1]["timestamp"] + timedelta(minutes=bar_minutes)
        if expected_end > now:
            df = df.iloc[:-1].reset_index(drop=True)
    return df


def _sync_symbols(
    symbol_keys: dict[str, Optional[str]],
    dates: Iterable[date],
    strategy_ids: list[str],
    unit: str,
    interval: int,
    *,
    notional: float = DEFAULT_NOTIONAL,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    no_cache: bool = False,
    progress: Optional[Callable[[int, int, str, str], None]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Shared rebuild routine for one or many strategies.

    Candles are downloaded once per symbol and replayed against every strategy
    in ``strategy_ids``, so tracking the 3-Candle, VWAP and H1–H5 engines over
    a multi-day window costs the same number of API calls as a single strategy.
    """
    if now is None:
        now = datetime.now(IST)
    target_dates = sorted(set(dates))
    strategies = [s for s in dict.fromkeys(strategy_ids) if s]
    empty_summary = {
        "added": 0, "updated": 0, "settled": 0, "errors": 0,
        "dates": [d.isoformat() for d in target_dates], "symbols": 0,
        "strategies": strategies, "total": len(load_journal()),
    }
    if not target_dates or not strategies:
        return empty_summary

    entries = load_journal()
    added = updated = settled_count = errors = 0
    total_symbols = len(symbol_keys)

    for idx, (symbol, instrument_key) in enumerate(symbol_keys.items(), start=1):
        symbol = str(symbol).upper()
        if progress:
            progress(idx, total_symbols, symbol, f"Loading candles for {symbol}...")
        if not instrument_key:
            errors += 1
            if progress:
                progress(idx, total_symbols, symbol, f"⚠️ {symbol}: no instrument key")
            continue

        try:
            df = load_candles_for_range(
                symbol,
                instrument_key,
                target_dates,
                unit,
                interval,
                fetch_historical_fn=fetch_historical_fn,
                fetch_intraday_fn=fetch_intraday_fn,
                no_cache=no_cache,
                now=now,
            )
        except Exception:
            errors += 1
            if progress:
                progress(idx, total_symbols, symbol, f"⚠️ {symbol}: candle fetch failed")
            continue

        if df.empty:
            errors += 1
            if progress:
                progress(idx, total_symbols, symbol, f"⚠️ {symbol}: no candle data")
            continue

        new_records: list[dict] = []
        for day in target_dates:
            if _day_bars(df, day).empty:
                continue
            for strategy_id in strategies:
                new_records.extend(
                    build_setups_for_date(
                        df, symbol, strategy_id, day, unit, interval,
                        notional=notional, source="sync",
                    )
                )

        entries, a, u = upsert_entries(new_records, entries)
        added += a
        updated += u

        # Refresh outcomes for every entry we have bars for on these dates.
        wanted = set(target_dates)
        for entry in entries:
            if entry.get("symbol") != symbol:
                continue
            entry_day = entry.get("date")
            if isinstance(entry_day, date):
                pass
            else:
                try:
                    entry_day = date.fromisoformat(str(entry_day))
                except Exception:
                    continue
            if entry_day not in wanted:
                continue
            entry_unit = entry.get("unit")
            entry_interval = entry.get("interval")
            if entry_unit and entry_interval:
                if entry_unit != unit or int(entry_interval) != int(interval):
                    continue
            before = entry.get("last_updated")
            try:
                evaluate_entry(
                    entry,
                    _day_bars(df, entry_day),
                    notional=notional,
                    now=now,
                )
            except Exception:
                continue
            if entry.get("last_updated") != before and entry.get("settled"):
                settled_count += 1

        if progress:
            progress(
                idx, total_symbols, symbol,
                f"✅ {symbol}: {len(new_records)} setup(s) across "
                f"{len(target_dates)} day(s) × {len(strategies)} strateg"
                f"{'y' if len(strategies) == 1 else 'ies'}",
            )

    save_journal(entries)
    return {
        "added": added,
        "updated": updated,
        "settled": settled_count,
        "errors": errors,
        "dates": [d.isoformat() for d in target_dates],
        "symbols": total_symbols,
        "strategies": strategies,
        "total": len(entries),
    }


def sync_journal(
    symbol_keys: dict[str, Optional[str]],
    dates: Iterable[date],
    strategy_id: str,
    unit: str,
    interval: int,
    *,
    notional: float = DEFAULT_NOTIONAL,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    no_cache: bool = False,
    progress: Optional[Callable[[int, int, str, str], None]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Rebuild setups for the given dates and refresh every outcome.

    One candle fetch per symbol covers the whole range (plus warm-up).
    """
    return _sync_symbols(
        symbol_keys,
        dates,
        [strategy_id],
        unit,
        interval,
        notional=notional,
        fetch_historical_fn=fetch_historical_fn,
        fetch_intraday_fn=fetch_intraday_fn,
        no_cache=no_cache,
        progress=progress,
        now=now,
    )


def sync_journal_multi(
    symbol_keys: dict[str, Optional[str]],
    dates: Iterable[date],
    strategy_ids: Iterable[str],
    unit: str,
    interval: int,
    *,
    notional: float = DEFAULT_NOTIONAL,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    no_cache: bool = False,
    progress: Optional[Callable[[int, int, str, str], None]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Rebuild and settle several strategies over the same date range."""
    return _sync_symbols(
        symbol_keys,
        dates,
        list(strategy_ids),
        unit,
        interval,
        notional=notional,
        fetch_historical_fn=fetch_historical_fn,
        fetch_intraday_fn=fetch_intraday_fn,
        no_cache=no_cache,
        progress=progress,
        now=now,
    )
