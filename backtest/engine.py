"""
Backtest Engine
===============
Pure logic — no Streamlit imports.
Long-only backtester with stop-loss, take-profit, and transaction costs.

Costs: COST_PER_SIDE = 0.001 (0.1% per side) — this is an ESTIMATE.
Real brokerage, STT, stamp duty, and GST differ. Always verify with your broker.
"""
import pandas as pd
from typing import Optional
from config import COST_PER_SIDE, RESULTS_DIR
from strategies import get_signal_series, get_strategy_param, load_strategy


def run_backtest(
    df: pd.DataFrame,
    strategy_id: str,
    capital: float = 100_000,
    qty: int = 10,
    symbol: str = "UNKNOWN",
    **kwargs,
) -> dict:
    """
    Run a long-only backtest (or delegates to strategy's custom backtest if provided).

    Entry: on the candle AFTER a BUY signal, at that candle's open.
    Exit priority:
        1. Stop-loss (checked against candle low)
        2. Take-profit (checked against candle high)
        3. SELL signal (exit at NEXT candle's open)
        4. End of data (exit at last close)
    If SL and TP both hit in same candle, assume stop-loss first (conservative).
    If candle gaps open beyond a level, fill at the open, not at the level.

    Returns dict with:
        - metrics: dict of performance metrics
        - trades: list of trade dicts
        - equity_curve: list of equity values after each trade
        - signals: list of per-row signals
    """
    # Check if strategy provides a custom backtest engine (e.g. state machine / multi-target)
    mod = load_strategy(strategy_id)
    if hasattr(mod, "run_custom_backtest"):
        return mod.run_custom_backtest(df, capital=capital, qty=qty, symbol=symbol, **kwargs)

    stop_loss_pct = get_strategy_param(strategy_id, "STOP_LOSS_PCT", 0.03)
    take_profit_pct = get_strategy_param(strategy_id, "TAKE_PROFIT_PCT", 0.08)

    # Generate signals (no look-ahead)
    signals = get_signal_series(df, strategy_id)

    trades: list[dict] = []
    equity_curve: list[float] = [capital]
    current_capital = capital

    in_position = False
    entry_price = 0.0
    entry_idx = 0
    pending_buy = False
    pending_sell_idx: Optional[int] = None

    for i in range(len(df)):
        row = df.iloc[i]

        # 1. Entry on the candle AFTER a BUY signal, at that candle's open
        if not in_position and pending_buy:
            entry_price = row["open"]
            entry_idx = i
            in_position = True
            pending_buy = False

        if in_position:
            sl_price = entry_price * (1 - stop_loss_pct)
            tp_price = entry_price * (1 + take_profit_pct)

            exit_price = None
            reason = None

            # Check stop-loss first (conservative)
            if row["low"] <= sl_price:
                # If gaps open below SL, fill at open
                exit_price = min(row["open"], sl_price)
                reason = "stop_loss"
            # Then take-profit
            elif row["high"] >= tp_price:
                # If gaps open above TP, fill at open
                exit_price = max(row["open"], tp_price)
                reason = "take_profit"
            # Check for pending sell signal exit (at next candle's open)
            elif pending_sell_idx is not None and i == pending_sell_idx:
                exit_price = row["open"]
                reason = "sell_signal"
                pending_sell_idx = None

            if exit_price is not None:
                # Calculate PnL with costs
                entry_cost = entry_price * qty * COST_PER_SIDE
                exit_cost = exit_price * qty * COST_PER_SIDE
                gross_pnl = (exit_price - entry_price) * qty
                net_pnl = gross_pnl - entry_cost - exit_cost

                trades.append({
                    "entry_idx": entry_idx,
                    "entry_time": df.iloc[entry_idx]["timestamp"],
                    "entry_price": entry_price,
                    "exit_idx": i,
                    "exit_time": row["timestamp"],
                    "exit_price": exit_price,
                    "qty": qty,
                    "gross_pnl": round(gross_pnl, 2),
                    "costs": round(entry_cost + exit_cost, 2),
                    "net_pnl": round(net_pnl, 2),
                    "reason": reason,
                })

                current_capital += net_pnl
                equity_curve.append(round(current_capital, 2))
                in_position = False
                pending_sell_idx = None
            else:
                # Still in position: check for SELL signal -> schedule exit at next candle open
                if signals[i] == "SELL" and pending_sell_idx is None:
                    if i + 1 < len(df):
                        pending_sell_idx = i + 1

        else:
            # Not in position: check for BUY signal -> schedule entry at next candle
            if signals[i] == "BUY" and not in_position:
                if i + 1 < len(df):
                    pending_buy = True

        # End-of-data exit: if still in position on the last candle and didn't exit above
        if in_position and i == len(df) - 1:
            exit_price = row["close"]
            reason = "end_of_data"
            entry_cost = entry_price * qty * COST_PER_SIDE
            exit_cost = exit_price * qty * COST_PER_SIDE
            gross_pnl = (exit_price - entry_price) * qty
            net_pnl = gross_pnl - entry_cost - exit_cost

            trades.append({
                "entry_idx": entry_idx,
                "entry_time": df.iloc[entry_idx]["timestamp"],
                "entry_price": entry_price,
                "exit_idx": i,
                "exit_time": row["timestamp"],
                "exit_price": exit_price,
                "qty": qty,
                "gross_pnl": round(gross_pnl, 2),
                "costs": round(entry_cost + exit_cost, 2),
                "net_pnl": round(net_pnl, 2),
                "reason": reason,
            })

            current_capital += net_pnl
            equity_curve.append(round(current_capital, 2))
            in_position = False

    # ── Compute metrics ──────────────────────────────────────────────────
    metrics = _compute_metrics(trades, equity_curve, capital)

    # ── Save trades CSV ──────────────────────────────────────────────────
    if trades:
        trades_df = pd.DataFrame(trades)
        csv_path = RESULTS_DIR / f"{symbol}_{strategy_id}_trades.csv"
        trades_df.to_csv(csv_path, index=False)

    return {
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity_curve,
        "signals": signals,
    }


def _compute_metrics(trades: list[dict], equity_curve: list[float],
                     initial_capital: float) -> dict:
    """
    Compute backtest metrics.
    Max drawdown is computed on the closed-trade equity curve.
    """
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate_pct": 0.0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown_pct": 0.0,
            "cost_note": f"Costs: {COST_PER_SIDE*100:.1f}% per side (estimate — real brokerage/STT/GST differ)",
            "drawdown_note": "Max drawdown computed on closed-trade equity curve",
        }

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    total_pnl = sum(t["net_pnl"] for t in trades)

    # Max drawdown on equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_pnl / initial_capital * 100, 2),
        "avg_win": round(sum(t["net_pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["net_pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "cost_note": f"Costs: {COST_PER_SIDE*100:.1f}% per side (estimate — real brokerage/STT/GST differ)",
        "drawdown_note": "Max drawdown computed on closed-trade equity curve",
    }
