"""
Alert / Scan Logic
==================
Shared by the UI and tests — no Streamlit imports.

Evaluates only the latest CLOSED candle.
- Intraday: drops the last candle if still in progress.
- Daily: drops today's candle while the market is open.

Handles warm-up by fetching recent history to satisfy MIN_CANDLES,
appending today's intraday candles, and deduplicating.
"""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from config import IST, REQUEST_DELAY, STATE_DIR
from strategies import get_strategy_param, load_strategy


LAST_ALERTS_FILE = STATE_DIR / "last_alerts.json"


def _load_last_alerts() -> dict:
    """Load {symbol_strategy: last_candle_timestamp} from state."""
    if LAST_ALERTS_FILE.exists():
        try:
            return json.loads(LAST_ALERTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_last_alerts(alerts: dict) -> None:
    """Save last alert timestamps."""
    LAST_ALERTS_FILE.write_text(json.dumps(alerts, indent=2, default=str), encoding="utf-8")


def is_market_open(now: Optional[datetime] = None) -> bool:
    """Check if NSE market is currently open (Mon-Fri 09:15 - 15:30 IST)."""
    if now is None:
        now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def _drop_in_progress_candle(df: pd.DataFrame, unit: str,
                              interval: int, now: Optional[datetime] = None) -> pd.DataFrame:
    """
    Drop the last candle if it is still in progress.
    - For intraday (minutes/hours): drop the last candle if its expected end
      time hasn't passed yet.
    - For daily: drop today's candle if market is still open.
    """
    if df.empty:
        return df

    if now is None:
        now = datetime.now(IST)

    if unit in ("days", "weeks", "months"):
        # Drop today's candle while market is open
        if is_market_open(now):
            today = now.date()
            mask = df["timestamp"].dt.date < today
            return df.loc[mask].reset_index(drop=True)
        return df
    else:
        # Intraday: drop the last candle
        # The last candle may still be forming
        if len(df) <= 1:
            return df.iloc[:0].reset_index(drop=True)  # empty
        return df.iloc[:-1].reset_index(drop=True)


def get_strategy_trade_levels(
    mod,
    df: pd.DataFrame,
    sig: Optional[str],
    last_price: float,
    mode: str = "INTRADAY_5M",
) -> dict:
    """
    Extract or calculate trade levels:
    entry_price, stop_loss, tp1, tp2, rr, risk_per_share, direction, state.
    Delegates to strategy's get_trade_levels if defined, or uses STOP_LOSS_PCT/TAKE_PROFIT_PCT.
    """
    if hasattr(mod, "get_trade_levels"):
        try:
            levels = mod.get_trade_levels(df, sig=sig, mode=mode)
            if levels and any(v is not None for v in (levels.get("entry_price"), levels.get("stop_loss"))):
                return levels
        except Exception:
            pass

    # Generic fallback based on STOP_LOSS_PCT and TAKE_PROFIT_PCT
    sl_pct = getattr(mod, "STOP_LOSS_PCT", 0.005) or 0.005
    tp_pct = getattr(mod, "TAKE_PROFIT_PCT", 0.015) or 0.015

    if sig == "BUY":
        entry = round(float(last_price), 2)
        sl = round(entry * (1.0 - sl_pct), 2)
        risk = round(entry - sl, 2)
        tp1 = round(entry * (1.0 + tp_pct), 2)
        tp2 = round(entry * (1.0 + tp_pct * 1.5), 2) if mode == "SWING_DAILY" else None
        rr = f"1:{tp_pct / sl_pct:.1f}" if sl_pct else "1:1.5"
        return {
            "entry_price": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "direction": "BUY",
            "risk_per_share": risk,
            "state": "TRIGGERED",
        }
    elif sig == "SELL":
        entry = round(float(last_price), 2)
        sl = round(entry * (1.0 + sl_pct), 2)
        risk = round(sl - entry, 2)
        tp1 = round(entry * (1.0 - tp_pct), 2)
        tp2 = round(entry * (1.0 - tp_pct * 1.5), 2) if mode == "SWING_DAILY" else None
        rr = f"1:{tp_pct / sl_pct:.1f}" if sl_pct else "1:1.5"
        return {
            "entry_price": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "direction": "SELL",
            "risk_per_share": risk,
            "state": "TRIGGERED",
        }

    return {
        "entry_price": None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "rr": None,
        "direction": None,
        "risk_per_share": None,
        "state": "NONE",
    }


def _find_unalerted_trade(
    mod,
    df: pd.DataFrame,
    mode: str,
    last_alerts: dict,
    entry_alert_key: str,
) -> Optional[dict]:
    """
    Return the newest trade whose entry has not been alerted yet, or None.

    Strategies opt in by exposing ``get_recent_trades(df)`` (the 3-candle
    modules do). This lets a scan that runs after the entry candle has closed
    still report the trigger instead of silently losing it.
    """
    getter = getattr(mod, "get_recent_trades", None)
    if getter is None:
        return None
    try:
        try:
            trades = getter(df, mode=mode)
        except TypeError:
            trades = getter(df)
    except Exception:
        return None
    if not trades:
        return None

    trade = max(trades, key=lambda t: t.get("entry_time"))
    entry_key = str(trade.get("entry_time"))
    if last_alerts.get(entry_alert_key) == entry_key:
        return None

    last_alerts[entry_alert_key] = entry_key
    _save_last_alerts(last_alerts)
    return trade


def _apply_catchup_result(
    result: dict,
    symbol: str,
    strategy_id: str,
    price: float,
    trade: dict,
) -> dict:
    """Populate a scan result from a trade discovered by the catch-up check."""
    side = trade["direction"]
    entry = trade.get("entry_price")
    sl = trade.get("stop_loss")
    tp1 = trade.get("tp1")
    tp2 = trade.get("tp2")
    entry_time = trade.get("entry_time")
    is_open = bool(trade.get("is_open"))

    result["side"] = side
    result["price"] = price
    result["entry_price"] = entry
    result["stop_loss"] = sl
    result["tp1"] = tp1
    result["tp2"] = tp2
    result["rr"] = trade.get("rr") or ("1:1.5 / 1:3" if tp2 else "1:1.5")
    result["candle_time"] = entry_time

    sl_txt = f"₹{sl:.2f}" if sl is not None else "—"
    tp_txt = f"₹{tp1:.2f}" if tp1 is not None else "—"

    if is_open:
        result["status"] = "NEW"
        result["message"] = (
            f"🔔 {symbol}: {side} triggered @ ₹{entry:.2f} on {entry_time} "
            f"(catch-up: entry candle already closed | SL: {sl_txt} | TP1: {tp_txt}). "
            f"Alert only — place manually, no auto-order."
        )
    else:
        result["status"] = "MISSED"
        result["message"] = (
            f"🕘 {symbol}: {side} setup triggered @ ₹{entry:.2f} on {entry_time} but the "
            f"scan window was missed — already closed ({trade.get('exit_reason')}, "
            f"P&L ₹{trade.get('realized_pnl', 0):.2f}). Not actionable now."
        )
    return result


def _empty_result(symbol: str, strategy_id: str, status: str = "none", message: str = "") -> dict:
    return {
        "symbol": symbol,
        "side": None,
        "price": None,
        "entry_price": None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "rr": None,
        "candle_time": None,
        "strategy": strategy_id,
        "message": message,
        "status": status,
    }


def _scrub_error(exc: Exception) -> str:
    """Remove the Upstox token from an exception message."""
    err_msg = str(exc)
    from config import UPSTOX_ACCESS_TOKEN
    if UPSTOX_ACCESS_TOKEN:
        err_msg = err_msg.replace(UPSTOX_ACCESS_TOKEN, "***")
    return err_msg


def _fetch_scan_data(
    symbol: str,
    instrument_key: str,
    unit: str,
    interval: int,
    min_candles: int,
    fetch_historical_fn: Optional[Callable],
    fetch_intraday_fn: Optional[Callable],
    now: datetime,
) -> tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]:
    """Fetch and trim the candle frame shared by every strategy in a scan.

    Returns ``(df, status, message)``. ``df`` is None when ``status`` is set.
    """
    if unit in ("days", "weeks", "months"):
        # For daily+, use the historical endpoint with enough warm-up.
        lookback_days = max(min_candles * 2, 100)
        from_date = (now - timedelta(days=lookback_days)).date()
        to_date = now.date()
        df = fetch_historical_fn(instrument_key, symbol, unit, interval, from_date, to_date)
    else:
        # For intraday: fetch history + today's intraday, combine.
        # NSE has 375 minutes per session (09:15-15:30).
        candles_per_day = max(1, 375 // max(interval, 1))
        days_needed = int(np.ceil(min_candles / candles_per_day)) + 3  # +3 covers the weekend
        lookback_days = max(days_needed, 5)
        from_date = (now - timedelta(days=lookback_days)).date()
        to_date = (now - timedelta(days=1)).date()

        df_hist = fetch_historical_fn(instrument_key, symbol, unit, interval, from_date, to_date)
        df_intra = fetch_intraday_fn(instrument_key, symbol, unit, interval)

        if df_hist is not None and not df_hist.empty and df_intra is not None and not df_intra.empty:
            df = pd.concat([df_hist, df_intra]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        elif df_intra is not None and not df_intra.empty:
            df = df_intra
        else:
            df = df_hist

    if df is None or df.empty:
        return None, "no_candles", f"{symbol}: No candle data available."

    # Drop the in-progress candle.
    df = _drop_in_progress_candle(df, unit, interval, now)
    if df.empty:
        return None, "no_candles", f"{symbol}: No closed candles available."
    return df, None, None


def _evaluate_strategy(
    strategy_id: str,
    symbol: str,
    df: pd.DataFrame,
    mode: str,
    now: datetime,
) -> dict:
    """Evaluate a single strategy on an already-fetched candle frame."""
    mod = load_strategy(strategy_id)
    min_candles = int(get_strategy_param(strategy_id, "MIN_CANDLES", 50) or 1)
    result = _empty_result(symbol, strategy_id)

    if len(df) < min_candles:
        result["status"] = "insufficient_data"
        result["message"] = f"{symbol}: Only {len(df)} candles, need {min_candles}."
        return result

    # ── Evaluate the signal on the last closed candle ────────────────────
    sig = mod.signal(df)
    last_candle = df.iloc[-1]
    candle_time = last_candle["timestamp"]
    price = float(last_candle["close"])
    strat_mode = mode

    levels = get_strategy_trade_levels(mod, df, sig, price, mode=strat_mode)

    alert_key = f"{symbol}_{strategy_id}"
    entry_alert_key = f"{alert_key}::entry"
    last_alerts = _load_last_alerts()

    if sig not in ("BUY", "SELL"):
        # ── Catch-up: report a trigger whose entry candle already closed ──
        if strat_mode == "INTRADAY_5M":
            catchup = _find_unalerted_trade(mod, df, strat_mode, last_alerts, entry_alert_key)
            if catchup is not None:
                return _apply_catchup_result(result, symbol, strategy_id, price, catchup)

        if levels.get("state") == "ARMED" and levels.get("entry_price"):
            result["status"] = "ARMED"
            result["side"] = f"ARMED ({levels.get('direction', 'SETUP')})"
            result["price"] = price
            result["entry_price"] = levels.get("entry_price")
            result["stop_loss"] = levels.get("stop_loss")
            result["tp1"] = levels.get("tp1")
            result["tp2"] = levels.get("tp2")
            result["rr"] = levels.get("rr")
            result["candle_time"] = candle_time
            result["message"] = (
                f"⚡ {symbol}: ARMED for {levels.get('direction')} breakout @ ₹{levels['entry_price']:.2f} "
                f"(SL: ₹{levels['stop_loss']:.2f} | TP1: ₹{levels['tp1']:.2f})"
            )
            return result

        result["status"] = "none"
        result["message"] = f"{symbol}: No signal."
        return result

    # ── Trade levels populated for BUY / SELL ────────────────────────────
    result["side"] = sig
    result["price"] = price
    result["entry_price"] = levels.get("entry_price", price)
    result["stop_loss"] = levels.get("stop_loss")
    result["tp1"] = levels.get("tp1")
    result["tp2"] = levels.get("tp2")
    result["rr"] = levels.get("rr")
    result["candle_time"] = candle_time

    # ── De-dupe check ────────────────────────────────────────────────────
    last_ts = last_alerts.get(alert_key)

    candle_time_str = str(candle_time)
    if last_ts == candle_time_str:
        result["status"] = "already_alerted"
        result["message"] = f"{symbol}: {sig} already alerted for {candle_time_str}."
        return result

    # ── New alert ────────────────────────────────────────────────────────
    last_alerts[alert_key] = candle_time_str
    last_alerts[entry_alert_key] = candle_time_str
    _save_last_alerts(last_alerts)

    result["status"] = "NEW"

    if hasattr(mod, "get_alert_message"):
        custom_msg = mod.get_alert_message(df, sig, symbol)
        if custom_msg:
            result["message"] = custom_msg
        else:
            result["message"] = (
                f"🔔 {symbol}: {sig} at ₹{price:.2f} "
                f"(candle: {candle_time_str}, strategy: {strategy_id}). "
                f"Alert only — place manually, no auto-order."
            )
    else:
        result["message"] = (
            f"🔔 {symbol}: {sig} at ₹{price:.2f} "
            f"(candle: {candle_time_str}, strategy: {strategy_id}). "
            f"Alert only — place manually, no auto-order."
        )
    return result


def scan_symbol(
    symbol: str,
    instrument_key: str,
    strategy_id: str,
    unit: str = "minutes",
    interval: int = 15,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Scan a single symbol for alerts.

    Args:
        symbol: Trading symbol (e.g. "RELIANCE")
        instrument_key: Upstox instrument key
        strategy_id: Strategy ID from registry
        unit: Candle unit
        interval: Candle interval
        fetch_historical_fn: Callable(instrument_key, symbol, unit, interval, from_date, to_date) -> DataFrame
        fetch_intraday_fn: Callable(instrument_key, symbol, unit, interval) -> DataFrame
        now: Override current time for testing

    Returns:
        dict with keys: symbol, side, price, entry_price, stop_loss, tp1, tp2, rr, candle_time, strategy, message, status
    """
    if now is None:
        now = datetime.now(IST)
    min_candles = int(get_strategy_param(strategy_id, "MIN_CANDLES", 50) or 1)
    mode = "SWING_DAILY" if unit in ("days", "weeks", "months") else "INTRADAY_5M"

    try:
        df, status, message = _fetch_scan_data(
            symbol, instrument_key, unit, interval, min_candles,
            fetch_historical_fn, fetch_intraday_fn, now,
        )
        if status is not None:
            return _empty_result(symbol, strategy_id, status=status, message=message or "")
        return _evaluate_strategy(strategy_id, symbol, df, mode, now)
    except Exception as e:
        return _empty_result(
            symbol, strategy_id, status="error",
            message=f"{symbol}: Error — {_scrub_error(e)}",
        )


def scan_symbol_multi(
    symbol: str,
    instrument_key: str,
    strategy_ids: list[str],
    unit: str = "minutes",
    interval: int = 5,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    now: Optional[datetime] = None,
) -> dict[str, dict]:
    """Scan one symbol with several strategies using a single candle download.

    Returns ``{strategy_id: result}`` where each result has the same shape as
    :func:`scan_symbol`. This is what lets the Alerts page evaluate the
    3-Candle, VWAP and H1–H5 engines side by side without re-fetching candles
    for every strategy.
    """
    if now is None:
        now = datetime.now(IST)
    strategy_ids = list(dict.fromkeys(strategy_ids))
    if not strategy_ids:
        return {}

    mode = "SWING_DAILY" if unit in ("days", "weeks", "months") else "INTRADAY_5M"
    min_candles = max(
        int(get_strategy_param(sid, "MIN_CANDLES", 50) or 1) for sid in strategy_ids
    )

    try:
        df, status, message = _fetch_scan_data(
            symbol, instrument_key, unit, interval, min_candles,
            fetch_historical_fn, fetch_intraday_fn, now,
        )
    except Exception as e:
        error = _empty_result(
            symbol, strategy_ids[0], status="error",
            message=f"{symbol}: Error — {_scrub_error(e)}",
        )
        return {sid: {**error, "strategy": sid} for sid in strategy_ids}

    if status is not None:
        base = _empty_result(symbol, strategy_ids[0], status=status, message=message or "")
        return {sid: {**base, "strategy": sid} for sid in strategy_ids}

    out: dict[str, dict] = {}
    for strategy_id in strategy_ids:
        try:
            out[strategy_id] = _evaluate_strategy(strategy_id, symbol, df, mode, now)
        except Exception as e:
            out[strategy_id] = _empty_result(
                symbol, strategy_id, status="error",
                message=f"{symbol}: Error — {_scrub_error(e)}",
            )
    return out


def _progress_message(symbol: str, result: dict) -> str:
    status_code = result.get("status")
    if status_code == "NEW":
        return f"🔔 {symbol}: New Signal -> {result['side']} @ ₹{result['price']:.2f}"
    if status_code == "ARMED":
        p_disp = result.get('entry_price') or result.get('price')
        return f"⚡ {symbol}: Setup Armed -> {result['side']} @ ₹{p_disp:.2f}"
    if status_code == "already_alerted":
        return f"ℹ️ {symbol}: {result['side']} already alerted ({result.get('candle_time')})"
    if status_code == "MISSED":
        return f"🕘 {symbol}: Missed {result['side']} trigger ({result.get('candle_time')}) — setup already closed"
    if status_code == "none":
        return f"⚪ {symbol}: Evaluated closed candles · No active setup"
    if status_code == "insufficient_data":
        return f"⚠️ {symbol}: Insufficient candles"
    return f"⚠️ {symbol}: {result.get('message', 'Completed')}"


def scan_watchlist(
    symbols: dict[str, str],  # {symbol: instrument_key}
    strategy_id: str,
    unit: str = "minutes",
    interval: int = 15,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    now: Optional[datetime] = None,
    progress_callback: Optional[Callable[[int, int, str, str, Optional[dict]], None]] = None,
) -> list[dict]:
    """
    Scan all symbols in the watchlist.
    Returns list of alert result dicts.
    Optional progress_callback(index_1_based, total_symbols, current_symbol, status_message, result_dict)
    """
    results = []
    symbol_items = list(symbols.items())
    total_count = len(symbol_items)

    for idx, (symbol, ikey) in enumerate(symbol_items, start=1):
        if progress_callback:
            progress_callback(idx, total_count, symbol, f"Fetching candle data for {symbol} ({unit} {interval})...", None)

        if ikey is None:
            err_res = _empty_result(
                symbol, strategy_id, status="error",
                message=f"{symbol}: Could not resolve instrument key.",
            )
            results.append(err_res)
            if progress_callback:
                progress_callback(idx, total_count, symbol, f"⚠️ {symbol}: Unresolved instrument key (set in fallback)", err_res)
            continue

        result = scan_symbol(
            symbol=symbol,
            instrument_key=ikey,
            strategy_id=strategy_id,
            unit=unit,
            interval=interval,
            fetch_historical_fn=fetch_historical_fn,
            fetch_intraday_fn=fetch_intraday_fn,
            now=now,
        )
        results.append(result)

        if progress_callback:
            progress_callback(idx, total_count, symbol, _progress_message(symbol, result), result)

        time.sleep(REQUEST_DELAY)

    return results


def scan_watchlist_multi(
    symbols: dict[str, str],  # {symbol: instrument_key}
    strategy_ids: list[str],
    unit: str = "minutes",
    interval: int = 5,
    fetch_historical_fn: Optional[Callable] = None,
    fetch_intraday_fn: Optional[Callable] = None,
    now: Optional[datetime] = None,
    progress_callback: Optional[Callable[[int, int, str, str, Optional[dict]], None]] = None,
    progress_strategy: Optional[str] = None,
) -> dict[str, list[dict]]:
    """Scan every symbol once and evaluate all requested strategies.

    Returns ``{strategy_id: [results in symbol order]}``. Candles are fetched
    once per symbol, so a 7-strategy 5-day forward test costs the same API
    calls as a single-strategy scan.
    """
    strategy_ids = list(dict.fromkeys(strategy_ids))
    results: dict[str, list[dict]] = {sid: [] for sid in strategy_ids}
    symbol_items = list(symbols.items())
    total_count = len(symbol_items)
    label = progress_strategy if progress_strategy in strategy_ids else (strategy_ids[0] if strategy_ids else None)

    for idx, (symbol, ikey) in enumerate(symbol_items, start=1):
        if progress_callback:
            progress_callback(idx, total_count, symbol, f"Fetching candle data for {symbol} ({unit} {interval})...", None)

        if ikey is None:
            err_res = _empty_result(
                symbol, label or "", status="error",
                message=f"{symbol}: Could not resolve instrument key.",
            )
            for sid in strategy_ids:
                results[sid].append({**err_res, "strategy": sid})
            if progress_callback:
                progress_callback(idx, total_count, symbol, f"⚠️ {symbol}: Unresolved instrument key (set in fallback)", err_res)
            continue

        per_symbol = scan_symbol_multi(
            symbol=symbol,
            instrument_key=ikey,
            strategy_ids=strategy_ids,
            unit=unit,
            interval=interval,
            fetch_historical_fn=fetch_historical_fn,
            fetch_intraday_fn=fetch_intraday_fn,
            now=now,
        )
        shown = None
        for sid in strategy_ids:
            result = per_symbol.get(sid) or _empty_result(symbol, sid)
            results[sid].append(result)
            if sid == label:
                shown = result

        if progress_callback:
            shown = shown or (results[strategy_ids[0]][-1] if strategy_ids else None)
            msg = _progress_message(symbol, shown) if shown else f"⚠️ {symbol}: Completed"
            if shown and shown.get("status") in ("NEW", "ARMED"):
                msg += f" · {len(strategy_ids)} strategies checked"
            progress_callback(idx, total_count, symbol, msg, shown)

        time.sleep(REQUEST_DELAY)

    return results
