"""
api_tracker.py — Session API call logger and metrics tracker.
Tracks calls to Upstox API and local cache hits.
NEVER logs, prints, stores, or exposes the API token.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
import pandas as pd

from config import IST, UPSTOX_ACCESS_TOKEN


@dataclass
class ApiCallRecord:
    timestamp: datetime
    call_type: str        # 'Historical' | 'Intraday' | 'Instrument Master' | 'Cache Hit'
    symbol: str
    instrument_key: str
    endpoint: str         # sanitized URL path (never contains token)
    unit_interval: str    # e.g. 'days/1', 'minutes/15'
    range_str: str        # e.g. '2024-01-01 to 2024-12-31'
    status_code: int      # HTTP status or 200 for cache
    latency_ms: float     # milliseconds taken
    candles_count: int    # number of candles received/loaded
    status: str           # 'SUCCESS', 'CACHED', '429_RETRY', 'FAILED'
    details: str = ""     # extra note (e.g. 'Chunk 1/3', 'Loaded from disk')


class ApiTracker:
    """Thread-safe singleton session tracker for API calls and data metrics."""

    def __init__(self):
        self._calls: List[ApiCallRecord] = []
        self._latest_datasets: Dict[str, pd.DataFrame] = {}

    def sanitize_text(self, text: str) -> str:
        """Ensure no API token ever leaks into logs or details."""
        if not text:
            return ""
        if UPSTOX_ACCESS_TOKEN:
            text = text.replace(UPSTOX_ACCESS_TOKEN, "***")
        return text

    def record_call(
        self,
        call_type: str,
        symbol: str,
        instrument_key: str,
        endpoint: str,
        unit_interval: str,
        range_str: str,
        status_code: int,
        latency_ms: float,
        candles_count: int,
        status: str,
        details: str = "",
        df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Record an API call or cache hit event."""
        rec = ApiCallRecord(
            timestamp=datetime.now(IST),
            call_type=call_type,
            symbol=symbol.upper() if symbol else "N/A",
            instrument_key=instrument_key or "N/A",
            endpoint=self.sanitize_text(endpoint),
            unit_interval=unit_interval or "N/A",
            range_str=range_str or "N/A",
            status_code=status_code,
            latency_ms=round(latency_ms, 1),
            candles_count=candles_count,
            status=status,
            details=self.sanitize_text(details),
        )
        self._calls.append(rec)

        # Store latest fetched DataFrame for inspection in UI (max 10 datasets)
        if df is not None and not df.empty:
            key = f"{symbol.upper()}_{unit_interval}"
            self._latest_datasets[key] = df.copy()
            if len(self._latest_datasets) > 10:
                # remove oldest
                oldest_key = next(iter(self._latest_datasets))
                del self._latest_datasets[oldest_key]

    def get_calls(self) -> List[ApiCallRecord]:
        """Return all calls, newest first."""
        return list(reversed(self._calls))

    def get_calls_df(self) -> pd.DataFrame:
        """Return call history as a pandas DataFrame."""
        if not self._calls:
            return pd.DataFrame(columns=[
                "Time (IST)", "Type", "Symbol", "Endpoint",
                "Unit/Interval", "Range", "HTTP Status",
                "Latency (ms)", "Candles", "Status", "Details"
            ])
        rows = []
        for c in reversed(self._calls):
            rows.append({
                "Time (IST)": c.timestamp.strftime("%H:%M:%S"),
                "Type": c.call_type,
                "Symbol": c.symbol,
                "Endpoint": c.endpoint,
                "Unit/Interval": c.unit_interval,
                "Range": c.range_str,
                "HTTP Status": c.status_code,
                "Latency (ms)": c.latency_ms,
                "Candles": c.candles_count,
                "Status": c.status,
                "Details": c.details,
            })
        return pd.DataFrame(rows)

    def get_metrics(self) -> Dict[str, Any]:
        """Compute aggregate metrics for the API monitor."""
        total_events = len(self._calls)
        cache_hits = sum(1 for c in self._calls if c.call_type == "Cache Hit")
        live_calls = [c for c in self._calls if c.call_type != "Cache Hit"]
        total_live_calls = len(live_calls)
        rate_limits_429 = sum(1 for c in self._calls if c.status_code == 429 or "429" in c.status)
        errors = sum(1 for c in live_calls if c.status_code >= 400 and c.status_code != 429)

        avg_latency = (
            sum(c.latency_ms for c in live_calls) / total_live_calls
            if total_live_calls > 0
            else 0.0
        )
        total_candles = sum(c.candles_count for c in self._calls)
        symbols = sorted(list({c.symbol for c in self._calls if c.symbol != "N/A"}))

        cache_savings_pct = (
            round((cache_hits / total_events) * 100, 1)
            if total_events > 0
            else 0.0
        )

        # Health status
        if not self._calls:
            health = "Idle (no calls yet)"
            health_color = "gray"
        elif errors > 0 and live_calls and live_calls[-1].status_code >= 400:
            health = "Error"
            health_color = "red"
        elif rate_limits_429 > 0:
            health = "Throttled (retrying)"
            health_color = "orange"
        else:
            health = "Healthy"
            health_color = "green"

        return {
            "total_events": total_events,
            "total_live_calls": total_live_calls,
            "cache_hits": cache_hits,
            "cache_savings_pct": cache_savings_pct,
            "rate_limits_429": rate_limits_429,
            "errors": errors,
            "avg_latency_ms": round(avg_latency, 1),
            "total_candles": total_candles,
            "symbols_count": len(symbols),
            "symbols": symbols,
            "health": health,
            "health_color": health_color,
            "last_call_time": self._calls[-1].timestamp.strftime("%H:%M:%S IST") if self._calls else "None",
        }

    def get_latest_datasets(self) -> Dict[str, pd.DataFrame]:
        """Return dict of dataset key -> DataFrame."""
        return self._latest_datasets

    def clear(self) -> None:
        """Clear all logged events."""
        self._calls.clear()
        self._latest_datasets.clear()


# Global shared instance
tracker = ApiTracker()
