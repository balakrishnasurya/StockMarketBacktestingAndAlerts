"""
test_chartink.py — Offline tests for the Chartink screener client + watchlist sync.

No network: every test injects a fake HTTP session, fake screener client and
fake instrument resolver. Covers payload parsing, the CSRF handshake, HTML /
419 failures, atomic replacement, the empty-result guard, validation drops and
--top / --max selection.
"""
import json
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

import chartink_sync
from chartink import (
    ChartinkClient,
    ChartinkError,
    ScreenerRow,
    build_request_payload,
    parse_payload,
)


# ── Fakes ────────────────────────────────────────────────────────────────
FAKE_PAYLOAD = {
    "draw": 1,
    "recordsTotal": 2,
    "data": [
        {
            "sr": 1,
            "nsecode": "muthootfin",
            "name": "Muthoot Finance Limited",
            "scan-column-default-close": 2777,
            "scan-column-default-percent-change": -2.22,
            "scan-column-default-volume": 679654,
        },
        {
            "sr": 2,
            "nsecode": "YESBANK",
            "name": "Yes Bank Limited",
            "scan-column-default-close": 18.02,
            "scan-column-default-percent-change": 3.44,
            "scan-column-default-volume": 188122355,
        },
    ],
}


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """Mimics the two calls ChartinkClient makes, without any network."""

    def __init__(self, post_status=200, post_text=None, xsrf="eyJpdiI6IkFCQyJ9%3D", page_status=200):
        if post_text is None:
            post_text = json.dumps(FAKE_PAYLOAD)
        self.post_status = post_status
        self.post_text = post_text
        self.page_status = page_status
        self.cookies = {"XSRF-TOKEN": xsrf} if xsrf else {}
        self.last_post = None

    def get(self, url, **kwargs):
        return FakeResponse(self.page_status, "<html>screener</html>")

    def post(self, url, **kwargs):
        self.last_post = kwargs
        return FakeResponse(self.post_status, self.post_text)


class FakeClient:
    def __init__(self, rows=None, exc=None):
        self.rows = rows if rows is not None else parse_payload(FAKE_PAYLOAD)
        self.exc = exc

    def fetch_rows(self):
        if self.exc:
            raise self.exc
        return self.rows


class FakeResolver:
    is_loaded = True
    error = None

    def __init__(self, known):
        self.known = set(known)

    def resolve_many(self, symbols):
        return {s: (f"NSE_EQ|{s}" if s in self.known else None) for s in symbols}


def row(symbol, pct=None):
    return ScreenerRow.from_payload(
        {"nsecode": symbol, "scan-column-default-percent-change": pct}
    )


def _tmpdir():
    return Path(tempfile.mkdtemp(prefix="chartink-test-"))


# ── Parsing ──────────────────────────────────────────────────────────────
def test_parses_symbols_and_columns():
    rows = parse_payload(FAKE_PAYLOAD)
    assert len(rows) == 2, rows
    assert rows[0].symbol == "MUTHOOTFIN", "symbols should be upper-cased"
    assert rows[0].close == 2777.0
    assert abs(rows[1].percent_change - 3.44) < 1e-9
    assert rows[1].volume == 188122355
    assert len(chartink_sync.select_symbols(rows)) == 2


def test_parse_rejects_payload_without_data():
    try:
        parse_payload({"message": "nope"})
    except ChartinkError as exc:
        assert "data" in str(exc)
    else:
        raise AssertionError("expected ChartinkError for payload without 'data'")


def test_request_payload_shape():
    payload = build_request_payload()
    assert set(payload) == {"scan_clause", "debug_clause", "column_clause"}
    assert "adx" in payload["scan_clause"].lower()


# ── CSRF handshake / failure modes ───────────────────────────────────────
def test_client_sends_decoded_csrf_token():
    session = FakeSession(xsrf="eyJpdiI6IkFCQyJ9%3D")
    rows = ChartinkClient(session=session).fetch_rows()
    assert [r.symbol for r in rows] == ["MUTHOOTFIN", "YESBANK"]
    headers = session.last_post["headers"]
    assert headers["x-xsrf-token"] == "eyJpdiI6IkFCQyJ9=", "cookie must be URL-decoded"
    assert headers["x-requested-with"] == "XMLHttpRequest"
    assert set(session.last_post["json"]) == {"scan_clause", "debug_clause", "column_clause"}


def test_client_raises_on_csrf_419():
    session = FakeSession(post_status=419, post_text='{"message":"CSRF token mismatch."}')
    try:
        ChartinkClient(session=session).fetch_rows()
    except ChartinkError as exc:
        assert "419" in str(exc) or "CSRF" in str(exc)
    else:
        raise AssertionError("expected ChartinkError on 419")


def test_client_raises_on_html_instead_of_json():
    session = FakeSession(post_text="<!DOCTYPE html><html>login</html>")
    try:
        ChartinkClient(session=session).fetch_rows()
    except ChartinkError as exc:
        assert "HTML" in str(exc)
    else:
        raise AssertionError("expected ChartinkError when Chartink serves HTML")


def test_client_raises_without_xsrf_cookie():
    session = FakeSession(xsrf="")
    try:
        ChartinkClient(session=session).fetch_rows()
    except ChartinkError as exc:
        assert "XSRF" in str(exc)
    else:
        raise AssertionError("expected ChartinkError when no XSRF cookie is issued")


# ── Watchlist replacement ────────────────────────────────────────────────
def test_sync_replaces_previous_watchlist():
    folder = _tmpdir()
    path = folder / "watchlist.json"
    path.write_text(json.dumps(["RELIANCE", "INFY", "TCS"]), encoding="utf-8")

    summary = chartink_sync.sync(
        watchlist_path=path,
        log_path=None,
        client=FakeClient([row("YESBANK"), row("VOLTAS")]),
        resolver=FakeResolver({"YESBANK", "VOLTAS"}),
    )

    assert summary["ok"] and summary["written"], summary
    assert json.loads(path.read_text(encoding="utf-8")) == ["YESBANK", "VOLTAS"]
    assert summary["previous"] == ["RELIANCE", "INFY", "TCS"]
    leftover = [p.name for p in folder.iterdir() if p.suffix == ".tmp"]
    assert not leftover, f"atomic write left temp files behind: {leftover}"


def test_sync_aborts_on_empty_result():
    folder = _tmpdir()
    path = folder / "watchlist.json"
    path.write_text(json.dumps(["RELIANCE"]), encoding="utf-8")

    summary = chartink_sync.sync(
        watchlist_path=path,
        log_path=None,
        client=FakeClient([]),
        resolver=FakeResolver({"RELIANCE"}),
    )

    assert not summary["ok"], summary
    assert "0 rows" in summary["reason"]
    assert json.loads(path.read_text(encoding="utf-8")) == ["RELIANCE"], "must not wipe the list"


def test_sync_drops_unknown_symbols():
    folder = _tmpdir()
    path = folder / "watchlist.json"

    summary = chartink_sync.sync(
        watchlist_path=path,
        log_path=None,
        client=FakeClient([row("YESBANK"), row("NOTREAL")]),
        resolver=FakeResolver({"YESBANK"}),
    )

    assert summary["dropped"] == ["NOTREAL"], summary
    assert json.loads(path.read_text(encoding="utf-8")) == ["YESBANK"]


def test_sync_keeps_everything_when_validation_unavailable():
    folder = _tmpdir()
    path = folder / "watchlist.json"

    class BrokenResolver:
        is_loaded = False
        error = "download failed"

        def resolve_many(self, symbols):
            return {s: None for s in symbols}

    summary = chartink_sync.sync(
        watchlist_path=path,
        log_path=None,
        client=FakeClient([row("YESBANK")]),
        resolver=BrokenResolver(),
    )

    assert summary["ok"], summary
    assert summary["validation_note"] and "skipped" in summary["validation_note"]
    assert json.loads(path.read_text(encoding="utf-8")) == ["YESBANK"]


def test_top_and_max_selection():
    rows = [row("A", 5.0), row("B", -2.0), row("C", 1.0), row("D", None)]
    kept, truncated = chartink_sync.select_symbols(rows, top=2)
    assert kept == ["A", "C"], kept
    assert truncated == []

    kept, truncated = chartink_sync.select_symbols(rows, max_symbols=1)
    assert kept == ["A"] and truncated == ["B", "C", "D"]

    kept, _ = chartink_sync.select_symbols([row("A"), row("A"), row("B")])
    assert kept == ["A", "B"], "duplicates must collapse"


def test_dry_run_does_not_write():
    folder = _tmpdir()
    path = folder / "watchlist.json"

    summary = chartink_sync.sync(
        watchlist_path=path,
        log_path=None,
        dry_run=True,
        client=FakeClient([row("YESBANK")]),
        resolver=FakeResolver({"YESBANK"}),
    )

    assert summary["ok"] and not summary["written"]
    assert not path.exists(), "dry run must not create the file"


def test_record_sync_trims_history():
    folder = _tmpdir()
    log = folder / "log.json"
    for i in range(chartink_sync.MAX_LOG_ENTRIES + 5):
        chartink_sync.record_sync({"i": i}, log)
    history = json.loads(log.read_text(encoding="utf-8"))
    assert len(history) == chartink_sync.MAX_LOG_ENTRIES
    assert history[-1]["i"] == chartink_sync.MAX_LOG_ENTRIES + 4


def test_corrupt_watchlist_reads_as_empty():
    folder = _tmpdir()
    path = folder / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    assert chartink_sync.load_watchlist(path) == []


# ── Runner ───────────────────────────────────────────────────────────────
def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"✅ {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - test runner
            failures += 1
            print(f"❌ {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
