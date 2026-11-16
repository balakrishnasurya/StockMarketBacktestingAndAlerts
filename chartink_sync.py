"""
Sync the Chartink "UB Intra Scan" screener into this app's watchlist.

Replaces ``state/watchlist.json`` wholesale, so yesterday's symbols disappear in
the same atomic write — no "clear then add" step, and a crash mid-run cannot
leave a half-written list.

Usage::

    python chartink_sync.py --dry-run     # show what would be written
    python chartink_sync.py               # replace the watchlist
    python chartink_sync.py --top 10      # only the 10 strongest movers
    python chartink_sync.py --max 20      # hard cap at MAX_WATCHLIST

Safety rails:
  * 0 rows from Chartink  -> abort, leave the existing watchlist untouched
  * symbols not in Upstox -> dropped (and reported) unless --no-validate
  * market holidays       -> caller's job; this script only moves data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from chartink import ChartinkClient, ChartinkError, ScreenerRow
from config import (
    CHARTINK_SYNC_LOG_FILE,
    IST,
    MAX_WATCHLIST,
    WATCHLIST_FILE,
)

# Task Scheduler redirects stdout to a file, where Windows defaults to cp1252 and
# the status glyphs below would raise UnicodeEncodeError. Force UTF-8.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

MAX_LOG_ENTRIES = 60


# ── Watchlist I/O ────────────────────────────────────────────────────────
def load_watchlist(path: Path = WATCHLIST_FILE) -> list[str]:
    """Read the current watchlist (missing/corrupt file -> empty list)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(s).strip().upper() for s in data if str(s).strip()] if isinstance(data, list) else []


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to ``path`` atomically so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_watchlist_atomic(symbols: Sequence[str], path: Path = WATCHLIST_FILE) -> None:
    """Replace the on-disk watchlist in one atomic step."""
    _write_json_atomic(path, [str(s).strip().upper() for s in symbols if str(s).strip()])


# ── Selection ────────────────────────────────────────────────────────────
def select_symbols(
    rows: Sequence[ScreenerRow],
    top: Optional[int] = None,
    max_symbols: Optional[int] = None,
) -> tuple[list[str], list[str]]:
    """Return ``(kept, truncated)`` symbols from screener rows.

    ``top`` keeps the strongest % movers first; ``max_symbols`` is a hard cap.
    Duplicates are removed while preserving Chartink's ordering.
    """
    chosen = list(rows)
    if top is not None and top > 0:
        chosen = sorted(
            chosen,
            key=lambda r: (r.percent_change is None, -(r.percent_change or 0.0)),
        )[:top]

    symbols: list[str] = []
    for row in chosen:
        sym = (row.symbol or "").strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)

    truncated: list[str] = []
    if max_symbols is not None and max_symbols > 0 and len(symbols) > max_symbols:
        truncated = symbols[max_symbols:]
        symbols = symbols[:max_symbols]
    return symbols, truncated


def validate_symbols(
    symbols: Sequence[str],
    resolver: Optional[Any] = None,
) -> tuple[list[str], list[str], Optional[str]]:
    """Drop symbols Upstox' instrument master does not know.

    Returns ``(kept, dropped, note)``. ``note`` is set when validation could not
    run (e.g. the master download failed) — in that case everything is kept.
    """
    if resolver is None:
        from instruments import InstrumentResolver

        resolver = InstrumentResolver()

    resolved = resolver.resolve_many(list(symbols))
    if not getattr(resolver, "is_loaded", True):
        err = getattr(resolver, "error", None) or "unknown error"
        return list(symbols), [], f"instrument master unavailable ({err}); validation skipped"

    kept = [sym for sym in symbols if resolved.get(sym)]
    dropped = [sym for sym in symbols if not resolved.get(sym)]
    return kept, dropped, None


# ── Sync log ─────────────────────────────────────────────────────────────
def record_sync(entry: dict[str, Any], path: Path = CHARTINK_SYNC_LOG_FILE) -> None:
    """Append one line of history to ``state/chartink_sync_log.json``."""
    path = Path(path)
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                history = existing
        except (OSError, json.JSONDecodeError):
            history = []
    history.append(entry)
    _write_json_atomic(path, history[-MAX_LOG_ENTRIES:])


# ── Orchestration ────────────────────────────────────────────────────────
def sync(
    *,
    watchlist_path: Path = WATCHLIST_FILE,
    log_path: Optional[Path] = CHARTINK_SYNC_LOG_FILE,
    top: Optional[int] = None,
    max_symbols: Optional[int] = None,
    validate: bool = True,
    dry_run: bool = False,
    client: Optional[Any] = None,
    resolver: Optional[Any] = None,
) -> dict[str, Any]:
    """Run the screener and replace the watchlist. Returns a summary dict."""
    client = client if client is not None else ChartinkClient()
    rows: list[ScreenerRow] = client.fetch_rows()

    summary: dict[str, Any] = {
        "ok": False,
        "reason": None,
        "fetched": len(rows),
        "previous": load_watchlist(watchlist_path),
        "symbols": [],
        "dropped": [],
        "truncated": [],
        "validation_note": None,
        "written": False,
        "dry_run": dry_run,
        "watchlist_path": str(watchlist_path),
        "checked_at": datetime.now(IST).isoformat(timespec="seconds"),
    }

    if not rows:
        summary["reason"] = (
            "Chartink returned 0 rows — watchlist left untouched. "
            "Check the screener on chartink.com."
        )
        return summary

    symbols, truncated = select_symbols(rows, top=top, max_symbols=max_symbols)
    summary["truncated"] = truncated
    if not symbols:
        summary["reason"] = "No usable symbols after selection — watchlist left untouched."
        return summary

    dropped: list[str] = []
    if validate:
        symbols, dropped, note = validate_symbols(symbols, resolver=resolver)
        summary["dropped"] = dropped
        summary["validation_note"] = note
        if not symbols:
            summary["reason"] = (
                "Every screener symbol was rejected by the instrument master — "
                "watchlist left untouched."
            )
            return summary

    summary["symbols"] = symbols
    if len(symbols) > MAX_WATCHLIST:
        summary["reason"] = (
            f"{len(symbols)} symbols is above MAX_WATCHLIST ({MAX_WATCHLIST}); "
            "the app will scan all of them but the Add button in the UI will be disabled. "
            "Use --max/--top or raise the config value."
        )

    if dry_run:
        summary["ok"] = True
        return summary

    write_watchlist_atomic(symbols, watchlist_path)
    summary["ok"] = True
    summary["written"] = True

    if log_path is not None:
        record_sync(
            {
                "synced_at": summary["checked_at"],
                "count": len(symbols),
                "symbols": symbols,
                "dropped": dropped,
                "fetched": len(rows),
            },
            log_path,
        )
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────
def _print_summary(summary: dict[str, Any], quiet: bool = False) -> None:
    if summary["reason"]:
        print(f"⚠️  {summary['reason']}")
    if not summary["symbols"]:
        print(f"❌ Nothing written. Fetched: {summary['fetched']}")
        return
    symbols = summary["symbols"]
    print(
        f"{'DRY RUN — would write' if summary['dry_run'] else 'Replaced watchlist with'} "
        f"{len(symbols)} symbols:"
    )
    print("   " + ", ".join(symbols))
    if summary["dropped"]:
        print(f"   dropped (not in Upstox master): {', '.join(summary['dropped'])}")
    if summary["truncated"]:
        print(f"   truncated: {', '.join(summary['truncated'])}")
    if summary["validation_note"]:
        print(f"   note: {summary['validation_note']}")

    previous = summary["previous"]
    if previous and not quiet:
        added = [s for s in symbols if s not in previous]
        removed = [s for s in previous if s not in symbols]
        kept = [s for s in symbols if s in previous]
        print(f"\n   carried over: {len(kept)}   new: {len(added)}   dropped from yesterday: {len(removed)}")
        if added:
            print(f"   + {', '.join(added)}")
        if removed:
            print(f"   - {', '.join(removed)}")

    if not summary["dry_run"]:
        print("\n   Next: reload the Alerts page (Ctrl+R) so Streamlit picks up the new list.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replace the watchlist with today's Chartink UB Intra Scan results.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show the result without writing.")
    parser.add_argument("--top", type=int, default=None, help="Keep only the N strongest % movers.")
    parser.add_argument("--max", dest="max_symbols", type=int, default=None, help="Hard cap on symbol count.")
    parser.add_argument("--no-validate", dest="validate", action="store_false", help="Skip Upstox symbol validation.")
    parser.add_argument("--watchlist", type=Path, default=WATCHLIST_FILE, help="Watchlist JSON path (testing).")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Only print the one-line result.")
    args = parser.parse_args(argv)

    try:
        summary = sync(
            watchlist_path=args.watchlist,
            top=args.top,
            max_symbols=args.max_symbols,
            validate=args.validate,
            dry_run=args.dry_run,
        )
    except ChartinkError as exc:
        print(f"❌ Chartink error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(summary, indent=2))
    elif args.quiet:
        print(f"{len(summary['symbols'])} symbols -> {summary['watchlist_path']}" if summary["ok"] else f"FAILED: {summary['reason']}")
    else:
        _print_summary(summary)

    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
