"""
Download, cache, and query the Upstox NSE instrument master.
Builds a symbol -> instrument_key map for NSE equities only.
"""
import gzip
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from config import DATA_DIR, INSTRUMENT_MASTER_URL, IST
from api_tracker import tracker


MASTER_CACHE = DATA_DIR / "nse_instruments.json"
MASTER_MAX_AGE = timedelta(days=1)


def _download_master() -> list[dict]:
    """Download the NSE instrument master from Upstox public assets."""
    t0 = time.time()
    try:
        resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        latency = (time.time() - t0) * 1000
        resp.raise_for_status()
        raw = gzip.decompress(resp.content).decode("utf-8")
        instruments = json.loads(raw)
        MASTER_CACHE.write_text(json.dumps(instruments, indent=2), encoding="utf-8")
        tracker.record_call(
            call_type="Instrument Master",
            symbol="NSE_EQ",
            instrument_key="MASTER",
            endpoint="/market-quote/instruments/exchange/NSE.json.gz",
            unit_interval="Daily",
            range_str="Active Contracts",
            status_code=resp.status_code,
            latency_ms=latency,
            candles_count=len(instruments),
            status="SUCCESS",
            details=f"Downloaded and cached {len(instruments):,} instruments",
        )
        return instruments
    except Exception as e:
        latency = (time.time() - t0) * 1000
        tracker.record_call(
            call_type="Instrument Master",
            symbol="NSE_EQ",
            instrument_key="MASTER",
            endpoint="/market-quote/instruments/exchange/NSE.json.gz",
            unit_interval="Daily",
            range_str="Active Contracts",
            status_code=500,
            latency_ms=latency,
            candles_count=0,
            status="FAILED",
            details=str(e),
        )
        raise


def _load_master(force_refresh: bool = False) -> list[dict]:
    """Load instruments, refreshing cache if stale or missing."""
    if not force_refresh and MASTER_CACHE.exists():
        age = datetime.now() - datetime.fromtimestamp(MASTER_CACHE.stat().st_mtime)
        if age < MASTER_MAX_AGE:
            data = json.loads(MASTER_CACHE.read_text(encoding="utf-8"))
            tracker.record_call(
                call_type="Cache Hit",
                symbol="NSE_EQ",
                instrument_key="MASTER",
                endpoint="data/nse_instruments.json",
                unit_interval="Daily",
                range_str="Active Contracts",
                status_code=200,
                latency_ms=1.0,
                candles_count=len(data),
                status="CACHED",
                details=f"Loaded {len(data):,} instruments from daily cache",
            )
            return data
    return _download_master()


def build_symbol_map(instruments: list[dict]) -> dict[str, str]:
    """
    Build trading_symbol -> instrument_key map for NSE equities.
    The master file uses fields: instrument_key, trading_symbol, segment, exchange.
    NSE equities have segment == 'NSE_EQ'.
    """
    smap: dict[str, str] = {}
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg == "NSE_EQ":
            sym = inst.get("trading_symbol", "")
            ikey = inst.get("instrument_key", "")
            if sym and ikey:
                smap[sym] = ikey
    return smap


class InstrumentResolver:
    """Resolve plain symbols like RELIANCE to instrument keys."""

    def __init__(self, force_refresh: bool = False,
                 master_loader=None):
        """
        Args:
            force_refresh: Force re-download of master.
            master_loader: Callable returning list[dict] — for testing.
        """
        self._loader = master_loader or (lambda: _load_master(force_refresh))
        self._map: dict[str, str] = {}
        self._manual_map: dict[str, str] = {}
        self._error: Optional[str] = None
        self._loaded = False

    def load(self) -> None:
        """Load the instrument master and build the map."""
        try:
            instruments = self._loader()
            self._map = build_symbol_map(instruments)
            self._loaded = True
            self._error = None
        except Exception as e:
            self._error = f"Failed to load instrument master: {e}"
            self._loaded = False

    def set_manual_key(self, symbol: str, instrument_key: str) -> None:
        """Set a fallback instrument key for a symbol (e.g. if master download fails)."""
        if symbol and instrument_key:
            self._manual_map[symbol.strip().upper()] = instrument_key.strip()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def error(self) -> Optional[str]:
        return self._error

    def resolve(self, symbol: str) -> Optional[str]:
        """Return instrument_key for a symbol, or None."""
        sym = symbol.strip().upper()
        if sym in self._manual_map:
            return self._manual_map[sym]
        if not self._loaded:
            self.load()
        return self._manual_map.get(sym) or self._map.get(sym)

    def resolve_many(self, symbols: list[str]) -> dict[str, Optional[str]]:
        """Resolve a list of symbols. Returns {symbol: instrument_key_or_None}."""
        if not self._loaded:
            self.load()
        result = {}
        for s in symbols:
            sym = s.strip().upper()
            result[sym] = self._manual_map.get(sym) or self._map.get(sym)
        return result

    def encode_key(self, instrument_key: str) -> str:
        """URL-encode an instrument key (| -> %7C)."""
        return quote(instrument_key, safe="")

    @property
    def available_symbols(self) -> list[str]:
        """Return all available NSE equity symbols."""
        if not self._loaded:
            self.load()
        return sorted(self._map.keys())
