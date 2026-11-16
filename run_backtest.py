"""
CLI wrapper for the backtest engine.
Usage:
    python run_backtest.py --strategy three_candle_bullish --symbol RELIANCE \
        --unit minutes --interval 5 --from 2024-01-01 --to 2024-12-31 \
        --capital 100000 --qty 10 --no-cache
"""
import argparse
import sys
from datetime import date

from config import token_status
from instruments import InstrumentResolver
from data_loader import fetch_historical
from backtest.engine import run_backtest
from strategies import list_strategies


def main():
    parser = argparse.ArgumentParser(description="Run backtest from CLI")
    parser.add_argument("--strategy", required=True, help="Strategy ID from registry")
    parser.add_argument("--symbol", required=True, help="NSE symbol (e.g. RELIANCE)")
    parser.add_argument("--unit", default="minutes", help="Candle unit: minutes/hours/days/weeks/months")
    parser.add_argument("--interval", type=int, default=5, help="Candle interval")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100_000, help="Starting capital")
    parser.add_argument("--qty", type=int, default=10, help="Quantity per trade")
    parser.add_argument("--no-cache", action="store_true", help="Skip cache")
    args = parser.parse_args()

    if token_status() == "missing":
        print("ERROR: UPSTOX_ACCESS_TOKEN not set. See README.md for setup.")
        sys.exit(1)

    # Resolve symbol
    resolver = InstrumentResolver()
    resolver.load()
    ikey = resolver.resolve(args.symbol)
    if not ikey:
        print(f"ERROR: Symbol '{args.symbol}' not found in NSE master.")
        sys.exit(1)

    from_d = date.fromisoformat(args.from_date)
    to_d = date.fromisoformat(args.to_date)

    print(f"Fetching {args.symbol} ({ikey}) {args.unit}/{args.interval} from {from_d} to {to_d} ...")
    df = fetch_historical(ikey, args.symbol, args.unit, args.interval, from_d, to_d, no_cache=args.no_cache)

    if df.empty:
        print("No data returned.")
        sys.exit(1)

    print(f"Got {len(df)} candles. Running backtest with '{args.strategy}' ...")
    result = run_backtest(df, args.strategy, capital=args.capital, qty=args.qty, symbol=args.symbol)

    # Print metrics
    print("\n── Metrics ──")
    for k, v in result["metrics"].items():
        print(f"  {k}: {v}")

    # Print trades
    print(f"\n── Trades ({len(result['trades'])}) ──")
    for t in result["trades"]:
        print(f"  {t['entry_time']} -> {t['exit_time']}  "
              f"entry={t['entry_price']:.2f}  exit={t['exit_price']:.2f}  "
              f"pnl={t['net_pnl']:.2f}  reason={t['reason']}")


if __name__ == "__main__":
    main()
