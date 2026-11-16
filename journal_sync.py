"""
Daily journal sync CLI
======================
Rebuild the trade journal for a date (or the last N days) from 5-minute
candles and settle TP / SL / EOD outcomes.

Examples
--------
    python journal_sync.py                     # today's session
    python journal_sync.py --days 5            # last 5 calendar days (weekdays)
    python journal_sync.py --date 2026-11-10
    python journal_sync.py --symbols RELIANCE,INFY --days 7
    python journal_sync.py --strategies all    # every registered engine
    python journal_sync.py --strategies three_candle_bullish,vwap_bullish

Schedule it with Windows Task Scheduler at ~15:35 IST to keep a full week
of logs even when the Streamlit app is closed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DEFAULT_NOTIONAL, IST, WATCHLIST_FILE
from instruments import InstrumentResolver
from journal import sync_journal_multi
from strategies import list_strategies

ALL_STRATEGY_IDS: tuple[str, ...] = tuple(sid for sid, _ in list_strategies())


def _load_watchlist() -> list[str]:
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(s).strip().upper() for s in data if str(s).strip()]
        except Exception:
            pass
    return []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync the daily trade journal.")
    parser.add_argument("--date", help="Session date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Sync the last N calendar days ending today (weekdays only)",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols (default: state/watchlist.json)",
    )
    parser.add_argument(
        "--strategy", default="three_candle_bullish", help="Strategy id to rebuild"
    )
    parser.add_argument(
        "--strategies",
        default="",
        help="Comma-separated strategy ids, or 'all' for every registered engine",
    )
    parser.add_argument("--unit", default="minutes", help="Candle unit")
    parser.add_argument("--interval", type=int, default=5, help="Candle interval")
    parser.add_argument(
        "--notional",
        type=float,
        default=DEFAULT_NOTIONAL,
        help="₹ notional per hypothetical trade",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Force fresh candle downloads"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    today = datetime.now(IST).date()

    if args.date:
        try:
            anchor = date.fromisoformat(args.date)
        except ValueError:
            print(f"Invalid --date: {args.date}")
            return 2
        dates = [anchor]
    elif args.days:
        start = today - timedelta(days=max(args.days, 1) - 1)
        dates = [
            start + timedelta(days=i)
            for i in range((today - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        ]
    else:
        dates = [today]

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _load_watchlist()
    if not symbols:
        print("No symbols. Add a watchlist in the app or pass --symbols.")
        return 1

    if args.strategies:
        if args.strategies.strip().lower() == "all":
            strategies = list(ALL_STRATEGY_IDS)
        else:
            chosen = [s.strip() for s in args.strategies.split(",") if s.strip()]
            strategies = [
                sid for sid in dict.fromkeys(chosen) if sid in ALL_STRATEGY_IDS
            ]
            strategies = strategies or list(ALL_STRATEGY_IDS)
    else:
        strategies = [args.strategy]

    print(
        f"Journal sync · {len(strategies)} "
        f"strateg{'y' if len(strategies) == 1 else 'ies'} · "
        f"{args.interval} {args.unit}"
    )
    for sid in strategies:
        print(f"  • {sid}")
    print(f"Dates: {dates[0]} → {dates[-1]} ({len(dates)} weekday(s))")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Notional: ₹{args.notional:,.0f}\n")

    resolver = InstrumentResolver()
    resolved = resolver.resolve_many(symbols)
    for symbol, key in resolved.items():
        if not key:
            print(f"  ⚠️  {symbol}: could not resolve instrument key")

    def progress(idx, total, symbol, message):
        print(f"  [{idx}/{total}] {message}")

    summary = sync_journal_multi(
        resolved,
        dates,
        strategies,
        args.unit,
        args.interval,
        notional=args.notional,
        no_cache=args.no_cache,
        progress=progress,
    )

    print("\n── Summary ─────────────────────────────────────────────")
    print(f"  Strategies      : {len(summary.get('strategies') or strategies)}")
    print(f"  New setups      : {summary['added']}")
    print(f"  Refreshed       : {summary['updated']}")
    print(f"  Settled         : {summary['settled']}")
    print(f"  Symbols failed  : {summary['errors']}")
    print(f"  Total in journal: {summary['total']}")
    return 0 if summary["symbols"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
