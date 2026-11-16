"""
Fetch historical candle data from Upstox API v3.
Handles chunking, caching, and retries.
"""
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests

from config import (
    DATA_DIR,
    HISTORICAL_URL,
    INTRADAY_URL,
    IST,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
    UPSTOX_ACCESS_TOKEN,
    get_chunk_days,
)
from api_tracker import tracker


def _headers() -> dict:
    """Build request headers. Token value is never logged."""
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def _cache_path(symbol: str, unit: str, interval: int) -> Path:
    return DATA_DIR / f"{symbol}_{unit}{interval}.csv"


def _load_cache(symbol: str, unit: str, interval: int,
                from_date: date, to_date: date) -> Optional[pd.DataFrame]:
    """Load cache only if it fully covers the requested range."""
    path = _cache_path(symbol, unit, interval)
    if not path.exists():
        return None
    t0 = time.time()
    try:
        df = pd.read_csv(path, parse_dates=["timestamp"])
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
        cache_min = df["timestamp"].min().date()
        cache_max = df["timestamp"].max().date()
        if cache_min <= from_date and cache_max >= to_date:
            mask = (df["timestamp"].dt.date >= from_date) & (df["timestamp"].dt.date <= to_date)
            res = df.loc[mask].reset_index(drop=True)
            latency = (time.time() - t0) * 1000
            tracker.record_call(
                call_type="Cache Hit",
                symbol=symbol,
                instrument_key="",
                endpoint=f"data/{symbol}_{unit}{interval}.csv",
                unit_interval=f"{unit}/{interval}",
                range_str=f"{from_date} to {to_date}",
                status_code=200,
                latency_ms=latency,
                candles_count=len(res),
                status="CACHED",
                details="Loaded from local CSV cache",
                df=res,
            )
            return res
    except Exception:
        pass
    return None


def _save_cache(df: pd.DataFrame, symbol: str, unit: str, interval: int) -> None:
    """Append to cache file, dedup on timestamp, save."""
    path = _cache_path(symbol, unit, interval)
    if path.exists():
        try:
            existing = pd.read_csv(path, parse_dates=["timestamp"])
            existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True).dt.tz_convert(IST)
            df = pd.concat([existing, df]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        except Exception:
            pass
    df.to_csv(path, index=False)


def _fetch_chunk(instrument_key: str, symbol: str, unit: str, interval: int,
                 from_date: date, to_date: date) -> list[list]:
    """Fetch one chunk of candles from the historical endpoint."""
    encoded_key = quote(instrument_key, safe="")
    url = HISTORICAL_URL.format(
        instrument_key=encoded_key,
        unit=unit,
        interval=interval,
        to_date=to_date.isoformat(),
        from_date=from_date.isoformat(),
    )
    sanitized_endpoint = f"/v3/historical-candle/{encoded_key}/{unit}/{interval}/{to_date}/{from_date}"

    for attempt in range(2):
        t0 = time.time()
        try:
            resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            latency = (time.time() - t0) * 1000

            if resp.status_code == 429:
                tracker.record_call(
                    call_type="Historical",
                    symbol=symbol,
                    instrument_key=instrument_key,
                    endpoint=sanitized_endpoint,
                    unit_interval=f"{unit}/{interval}",
                    range_str=f"{from_date} to {to_date}",
                    status_code=429,
                    latency_ms=latency,
                    candles_count=0,
                    status="429_RETRY",
                    details=f"Rate limit hit on attempt {attempt+1}, retrying after {RETRY_DELAY}s",
                )
                time.sleep(RETRY_DELAY)
                continue

            resp.raise_for_status()
            data = resp.json()
            candles = data.get("data", {}).get("candles", [])

            tracker.record_call(
                call_type="Historical",
                symbol=symbol,
                instrument_key=instrument_key,
                endpoint=sanitized_endpoint,
                unit_interval=f"{unit}/{interval}",
                range_str=f"{from_date} to {to_date}",
                status_code=resp.status_code,
                latency_ms=latency,
                candles_count=len(candles),
                status="SUCCESS",
                details=f"Chunk received ({len(candles)} candles)",
            )
            return candles

        except requests.exceptions.RequestException as e:
            latency = (time.time() - t0) * 1000
            err_code = getattr(getattr(e, "response", None), "status_code", 500)
            tracker.record_call(
                call_type="Historical",
                symbol=symbol,
                instrument_key=instrument_key,
                endpoint=sanitized_endpoint,
                unit_interval=f"{unit}/{interval}",
                range_str=f"{from_date} to {to_date}",
                status_code=err_code,
                latency_ms=latency,
                candles_count=0,
                status="FAILED",
                details=str(e),
            )
            if attempt == 0 and err_code == 429:
                time.sleep(RETRY_DELAY)
                continue
            raise
    return []


def fetch_historical(instrument_key: str, symbol: str,
                     unit: str, interval: int,
                     from_date: date, to_date: date,
                     no_cache: bool = False) -> pd.DataFrame:
    """
    Fetch historical candles with automatic chunking.
    Returns DataFrame with columns: timestamp, open, high, low, close, volume
    sorted oldest first, timestamps timezone-aware IST.
    """
    if not no_cache:
        cached = _load_cache(symbol, unit, interval, from_date, to_date)
        if cached is not None:
            return cached

    chunk_days = get_chunk_days(unit, interval)
    all_candles: list[list] = []
    current_from = from_date

    while current_from <= to_date:
        current_to = min(current_from + timedelta(days=chunk_days - 1), to_date)
        try:
            candles = _fetch_chunk(instrument_key, symbol, unit, interval, current_from, current_to)
            all_candles.extend(candles)
        except Exception:
            pass  # Skip failed chunks, continue with the rest
        current_from = current_to + timedelta(days=1)
        time.sleep(REQUEST_DELAY)

    if not all_candles:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df = df.drop(columns=["oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(IST)
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)

    if not no_cache:
        _save_cache(df, symbol, unit, interval)

    # Filter to requested range
    mask = (df["timestamp"].dt.date >= from_date) & (df["timestamp"].dt.date <= to_date)
    result_df = df.loc[mask].reset_index(drop=True)

    # Save to tracker dataset cache for UI inspection
    tracker.record_call(
        call_type="Dataset Assembled",
        symbol=symbol,
        instrument_key=instrument_key,
        endpoint=f"Assembled ({len(result_df)} candles)",
        unit_interval=f"{unit}/{interval}",
        range_str=f"{from_date} to {to_date}",
        status_code=200,
        latency_ms=0.0,
        candles_count=len(result_df),
        status="READY",
        details=f"Combined {len(result_df)} rows for {symbol}",
        df=result_df,
    )

    return result_df


def fetch_intraday(instrument_key: str, symbol: str,
                   unit: str = "minutes", interval: int = 1) -> pd.DataFrame:
    """
    Fetch today's intraday candles.
    Returns DataFrame with same columns as fetch_historical.
    """
    encoded_key = quote(instrument_key, safe="")
    url = INTRADAY_URL.format(
        instrument_key=encoded_key,
        unit=unit,
        interval=interval,
    )
    sanitized_endpoint = f"/v3/historical-candle/intraday/{encoded_key}/{unit}/{interval}"

    for attempt in range(2):
        t0 = time.time()
        try:
            resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            latency = (time.time() - t0) * 1000

            if resp.status_code == 429:
                tracker.record_call(
                    call_type="Intraday",
                    symbol=symbol,
                    instrument_key=instrument_key,
                    endpoint=sanitized_endpoint,
                    unit_interval=f"{unit}/{interval}",
                    range_str="Today (Intraday)",
                    status_code=429,
                    latency_ms=latency,
                    candles_count=0,
                    status="429_RETRY",
                    details=f"Rate limit hit, retrying after {RETRY_DELAY}s",
                )
                time.sleep(RETRY_DELAY)
                continue

            resp.raise_for_status()
            data = resp.json()
            candles = data.get("data", {}).get("candles", [])

            df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df = df.drop(columns=["oi"])
                df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(IST)
                df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)

            tracker.record_call(
                call_type="Intraday",
                symbol=symbol,
                instrument_key=instrument_key,
                endpoint=sanitized_endpoint,
                unit_interval=f"{unit}/{interval}",
                range_str="Today (Intraday)",
                status_code=resp.status_code,
                latency_ms=latency,
                candles_count=len(df),
                status="SUCCESS",
                details=f"Received {len(df)} intraday candles",
                df=df,
            )
            return df

        except requests.exceptions.RequestException as e:
            latency = (time.time() - t0) * 1000
            err_code = getattr(getattr(e, "response", None), "status_code", 500)
            tracker.record_call(
                call_type="Intraday",
                symbol=symbol,
                instrument_key=instrument_key,
                endpoint=sanitized_endpoint,
                unit_interval=f"{unit}/{interval}",
                range_str="Today (Intraday)",
                status_code=err_code,
                latency_ms=latency,
                candles_count=0,
                status="FAILED",
                details=str(e),
            )
            if attempt == 0 and err_code == 429:
                time.sleep(RETRY_DELAY)
                continue
            raise

    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
