"""
Chartink screener client — headless, no browser, no login.

The screener at https://chartink.com/screener/ub-intra-scan is driven by one XHR::

    POST https://chartink.com/screener/process
    content-type: application/json
    x-requested-with: XMLHttpRequest
    x-xsrf-token: <decoded XSRF-TOKEN cookie>

    {"scan_clause": ..., "debug_clause": ..., "column_clause": ...}

Chartink (Laravel) uses double-submit CSRF protection, but *running* a scan
needs no account: an anonymous session is enough. We therefore reproduce the
browser handshake exactly — GET the screener page to mint a session, read the
``XSRF-TOKEN`` cookie, and echo it back in the ``X-XSRF-TOKEN`` header.

Response shape::

    {"draw": 1, "recordsTotal": 14,
     "data": [{"sr": 1, "nsecode": "MUTHOOTFIN", "name": "...",
               "scan-column-default-close": 2777, ...}]}

…which is precisely the symbol list the page's Copy button serialises, plus the
close / %change / volume columns for free.

Note: clauses are captured from the saved screener. If the screener's
conditions are edited on chartink.com, re-capture them (DevTools → Network →
``process`` → Payload) and update the constants below.
"""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import requests

# ── Endpoints ────────────────────────────────────────────────────────────
BASE_URL = "https://chartink.com"
SCREENER_URL = f"{BASE_URL}/screener/ub-intra-scan"
PROCESS_URL = f"{BASE_URL}/screener/process"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30

# ── Screener definition (ub-intra-scan) ──────────────────────────────────
# Captured verbatim from the browser request. Keep the spacing — Chartink's
# clause parser is fine with it, and this stays diff-able against DevTools.
SCAN_CLAUSE = (
    "( {33489} (  [0] 5 minute adx( 14 ) >  30 and  [0] 5 minute adx( 14 ) >  30 "
    "and  [0] 5 minute adx di positive( 14 ) >=  26 and  "
    "[0] 5 minute adx di negative( 14 ) <=  17 ) )"
)

DEBUG_CLAUSE = (
    "groupcount( 1 where  [0] 5 minute adx( 14 ) >  30),"
    "groupcount( 1 where  [0] 5 minute adx( 14 ) >  30),"
    "groupcount( 1 where  [0] 5 minute adx di positive( 14 ) >=  26),"
    "groupcount( 1 where  [0] 5 minute adx di negative( 14 ) <=  17)"
)

COLUMN_CLAUSE = (
    " Daily Close as 'scan-column-default-close',  Daily \"close - 1 candle ago "
    "close / 1 candle ago close * 100\" as 'scan-column-default-percent-change', "
    "filternumber( daily close >  1 day ago close,1) as "
    "'default-percent-change-conditional-filters-color',  Daily Volume as "
    "'scan-column-default-volume'"
)


class ChartinkError(RuntimeError):
    """Chartink returned something we cannot use (CSRF, HTML, bad payload)."""


@dataclass(frozen=True)
class ScreenerRow:
    """One stock from the screener result table."""

    symbol: str
    name: str = ""
    close: Optional[float] = None
    percent_change: Optional[float] = None
    volume: Optional[int] = None

    @classmethod
    def from_payload(cls, row: dict[str, Any]) -> "ScreenerRow":
        return cls(
            symbol=str(row.get("nsecode") or "").strip().upper(),
            name=str(row.get("name") or "").strip(),
            close=_as_number(row.get("scan-column-default-close")),
            percent_change=_as_number(row.get("scan-column-default-percent-change")),
            volume=_as_int(row.get("scan-column-default-volume")),
        )


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _as_int(value: Any) -> Optional[int]:
    num = _as_number(value)
    return int(num) if num is not None else None


def parse_payload(payload: dict[str, Any]) -> list[ScreenerRow]:
    """Turn a raw ``/screener/process`` JSON payload into rows."""
    if not isinstance(payload, dict):
        raise ChartinkError(f"Unexpected Chartink payload type: {type(payload).__name__}")
    data = payload.get("data")
    if data is None:
        raise ChartinkError(
            "Chartink payload has no 'data' key — the screener may have been "
            f"deleted or renamed. Keys: {sorted(payload)[:8]}"
        )
    rows = [ScreenerRow.from_payload(item) for item in data]
    return [row for row in rows if row.symbol]


def build_request_payload(
    scan_clause: Optional[str] = None,
    debug_clause: Optional[str] = None,
    column_clause: Optional[str] = None,
) -> dict[str, str]:
    """Build the JSON body Chartink expects for a scan run."""
    return {
        "scan_clause": SCAN_CLAUSE if scan_clause is None else scan_clause,
        "debug_clause": DEBUG_CLAUSE if debug_clause is None else debug_clause,
        "column_clause": COLUMN_CLAUSE if column_clause is None else column_clause,
    }


class ChartinkClient:
    """Runs the UB Intra Scan screener and returns its rows.

    Args:
        session: A ``requests.Session``-like object (inject a fake in tests).
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        session: Optional[Any] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    # -- handshake ───────────────────────────────────────────────────────
    def _prime_session(self) -> None:
        """GET the screener page so Chartink issues a session + XSRF cookie."""
        resp = self._session.get(
            SCREENER_URL,
            timeout=self._timeout,
            headers={"user-agent": USER_AGENT},
        )
        if resp.status_code >= 400:
            raise ChartinkError(
                f"Could not open screener page ({resp.status_code}). "
                "Chartink may be down or blocking this IP."
            )

    def _csrf_token(self) -> str:
        raw = self._session.cookies.get("XSRF-TOKEN")
        if not raw:
            raise ChartinkError(
                "No XSRF-TOKEN cookie issued by Chartink — cannot run the scan."
            )
        return urllib.parse.unquote(raw)

    # -- scan ────────────────────────────────────────────────────────────
    def fetch_rows(self) -> list[ScreenerRow]:
        """Run the scan and return every row Chartink sends back."""
        self._prime_session()
        token = self._csrf_token()

        resp = self._session.post(
            PROCESS_URL,
            json=build_request_payload(),
            timeout=self._timeout,
            headers={
                "user-agent": USER_AGENT,
                "accept": "*/*",
                "content-type": "application/json",
                "x-requested-with": "XMLHttpRequest",
                "x-xsrf-token": token,
                "referer": SCREENER_URL,
                "origin": BASE_URL,
            },
        )

        if resp.status_code == 419:
            raise ChartinkError(
                "Chartink rejected the CSRF token (419). The session handshake "
                "failed — retry, and if it persists the endpoint has changed."
            )
        if resp.status_code >= 400:
            raise ChartinkError(
                f"Chartink scan failed with HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        body = (resp.text or "").lstrip()
        if not body.startswith("{"):
            # Chartink serves the JSON with a text/html content type, so the
            # body is the only reliable signal. HTML here means a login wall,
            # an error page, or a bot check.
            raise ChartinkError(
                "Chartink returned HTML instead of scan JSON "
                "(login wall, rate limit or block page?). "
                f"First 160 chars: {body[:160]!r}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ChartinkError(f"Chartink scan JSON was unparseable: {exc}") from exc

        return parse_payload(payload)

    def fetch_symbols(self) -> list[str]:
        """Run the scan and return just the NSE symbols, in Chartink order."""
        return [row.symbol for row in self.fetch_rows()]


if __name__ == "__main__":  # quick manual check: python chartink.py
    client = ChartinkClient()
    rows = client.fetch_rows()
    print(f"{len(rows)} stocks matched the UB Intra Scan screener:")
    for row in rows:
        pct = "?" if row.percent_change is None else f"{row.percent_change:+.2f}%"
        print(f"  {row.symbol:<14} {pct:>8}  close={row.close}")
