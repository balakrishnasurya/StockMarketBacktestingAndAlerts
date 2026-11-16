"""
test_alerts_chartink_ui.py — UI wiring tests for the Alerts page.

Runs the real app.py through ``streamlit.testing.v1.AppTest`` on the Alerts page.
``scanner.is_market_open`` is forced False so the 5-minute auto-refresh fragment
never starts a real Upstox scan mid-test, and the Chartink sync /
``write_watchlist_atomic`` are stubbed so nothing is written to state/.

Covers:
  * the 🔄 Chartink button exists next to ➕ Add, replaces the list and shows the diff
  * ↩️ Undo restores the previous list
  * Auto-refresh defaults to ON with a 5-minute interval
"""
import sys
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import chartink_sync

CANNED = {
    "ok": True,
    "reason": None,
    "fetched": 3,
    "previous": ["RELIANCE", "INFY"],
    "symbols": ["YESBANK", "VOLTAS", "INFY"],
    "dropped": ["NOTREAL"],
    "truncated": [],
    "validation_note": None,
    "written": True,
    "dry_run": False,
    "watchlist_path": "state/watchlist.json",
    "checked_at": "2026-11-16T21:30:00+05:30",
}


@contextmanager
def market_closed():
    """Force the market-closed path so auto-refresh cannot fire a real scan."""
    import scanner

    original = scanner.is_market_open
    scanner.is_market_open = lambda *args, **kwargs: False
    try:
        yield
    finally:
        scanner.is_market_open = original


def test_chartink_button_replaces_and_undoes():
    from streamlit.testing.v1 import AppTest

    calls = {"sync": 0, "write": []}

    def fake_sync(**kwargs):
        calls["sync"] += 1
        return dict(CANNED)

    def fake_write(symbols, path=None):
        calls["write"].append(list(symbols))

    original_sync = chartink_sync.sync
    original_write = chartink_sync.write_watchlist_atomic
    chartink_sync.sync = fake_sync
    chartink_sync.write_watchlist_atomic = fake_write
    try:
        with market_closed():
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
            at.session_state["nav_radio"] = "🔔 Alerts"
            at.run()
            assert not at.exception, list(at.exception)

            button = at.button(key="chartink_sync_btn")
            assert button.label == "🔄 Chartink"

            button.click().run()
            assert not at.exception, list(at.exception)
            assert calls["sync"] == 1, f"sync should run once, ran {calls['sync']}"

            banners = [s.value for s in at.success]
            assert any("3" in b and "UB Intra Scan" in b for b in banners), banners
            captions = [c.value for c in at.caption]
            assert any("2** new" in c for c in captions), captions
            assert any("No longer in today's scan" in c for c in captions), captions

            at.button(key="chartink_undo").click().run()
            assert not at.exception, list(at.exception)
            assert calls["write"] == [["RELIANCE", "INFY"]], calls["write"]
    finally:
        chartink_sync.sync = original_sync
        chartink_sync.write_watchlist_atomic = original_write


def test_alerts_auto_refresh_defaults():
    from streamlit.testing.v1 import AppTest

    with market_closed():
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
        at.session_state["nav_radio"] = "🔔 Alerts"
        at.run()
        assert not at.exception, list(at.exception)

        toggle = at.toggle(key="alerts_auto_refresh")
        assert toggle.value is True, "Auto-refresh should default to ON"

        interval = at.number_input(key="alerts_refresh_minutes")
        assert int(interval.value) == 5, f"default interval should be 5, got {interval.value}"


def test_ui_module_imports():
    """The app must import chartink_sync/ChartinkError for the panel to work."""
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "import chartink_sync" in source
    assert "from chartink import ChartinkError" in source
    assert "_render_chartink_panel()" in source


if __name__ == "__main__":
    failures = 0
    for test in (
        test_chartink_button_replaces_and_undoes,
        test_alerts_auto_refresh_defaults,
        test_ui_module_imports,
    ):
        try:
            test()
            print(f"✅ {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"❌ {test.__name__}: {type(exc).__name__}: {exc}")
    raise SystemExit(1 if failures else 0)
