"""
test_instruments.py — Offline instrument resolver tests.
Uses a small fake master file.
Covers: resolved symbol, unknown symbol, download failure.
"""
import sys
import json
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

from instruments import InstrumentResolver, build_symbol_map


# ── Fake master data ─────────────────────────────────────────────────────
FAKE_MASTER = [
    {
        "instrument_key": "NSE_EQ|INE002A01018",
        "trading_symbol": "RELIANCE",
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "name": "Reliance Industries",
    },
    {
        "instrument_key": "NSE_EQ|INE009A01021",
        "trading_symbol": "INFY",
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "name": "Infosys",
    },
    {
        "instrument_key": "NSE_EQ|INE467B01029",
        "trading_symbol": "TCS",
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "name": "TCS",
    },
    {
        # Not an equity — should be excluded
        "instrument_key": "NSE_FO|INE002A01018",
        "trading_symbol": "RELIANCE",
        "segment": "NSE_FO",
        "exchange": "NSE",
        "name": "Reliance F&O",
    },
]


def test_build_symbol_map():
    """Test that only NSE_EQ instruments are included."""
    smap = build_symbol_map(FAKE_MASTER)
    assert "RELIANCE" in smap
    assert "INFY" in smap
    assert "TCS" in smap
    # F&O should not override
    assert smap["RELIANCE"] == "NSE_EQ|INE002A01018"
    print(f"  Symbol map: {smap}")
    print("✅ test_build_symbol_map PASSED")


def test_resolve_known_symbol():
    """Test resolving a known symbol."""
    resolver = InstrumentResolver(master_loader=lambda: FAKE_MASTER)
    resolver.load()

    ikey = resolver.resolve("RELIANCE")
    assert ikey == "NSE_EQ|INE002A01018"

    ikey = resolver.resolve("infy")  # case-insensitive
    assert ikey == "NSE_EQ|INE009A01021"

    print("✅ test_resolve_known_symbol PASSED")


def test_resolve_unknown_symbol():
    """Test resolving an unknown symbol returns None."""
    resolver = InstrumentResolver(master_loader=lambda: FAKE_MASTER)
    resolver.load()

    ikey = resolver.resolve("NOSUCHSYMBOL")
    assert ikey is None
    print("✅ test_resolve_unknown_symbol PASSED")


def test_resolve_many():
    """Test batch resolution."""
    resolver = InstrumentResolver(master_loader=lambda: FAKE_MASTER)
    resolver.load()

    result = resolver.resolve_many(["RELIANCE", "UNKNOWN", "TCS"])
    assert result["RELIANCE"] == "NSE_EQ|INE002A01018"
    assert result["UNKNOWN"] is None
    assert result["TCS"] == "NSE_EQ|INE467B01029"
    print(f"  Batch result: {result}")
    print("✅ test_resolve_many PASSED")


def test_download_failure():
    """Test graceful handling when master download fails."""

    def failing_loader():
        raise ConnectionError("Simulated download failure")

    resolver = InstrumentResolver(master_loader=failing_loader)
    resolver.load()

    assert not resolver.is_loaded
    assert resolver.error is not None
    assert "Failed to load" in resolver.error
    print(f"  Error: {resolver.error}")

    # Should still return None for any symbol without manual fallback
    ikey = resolver.resolve("RELIANCE")
    assert ikey is None

    # But if user sets a manual fallback key, it resolves even when download failed!
    resolver.set_manual_key("RELIANCE", "NSE_EQ|INE002A01018")
    ikey_manual = resolver.resolve("RELIANCE")
    assert ikey_manual == "NSE_EQ|INE002A01018"
    print("✅ test_download_failure and manual fallback PASSED")


def test_encode_key():
    """Test URL encoding of instrument key."""
    resolver = InstrumentResolver(master_loader=lambda: FAKE_MASTER)
    encoded = resolver.encode_key("NSE_EQ|INE002A01018")
    assert "%7C" in encoded
    assert "|" not in encoded
    print(f"  Encoded: {encoded}")
    print("✅ test_encode_key PASSED")


if __name__ == "__main__":
    print("=" * 60)
    print("INSTRUMENT RESOLVER TESTS (offline, fake master)")
    print("=" * 60)

    test_build_symbol_map()
    test_resolve_known_symbol()
    test_resolve_unknown_symbol()
    test_resolve_many()
    test_download_failure()
    test_encode_key()

    print("\n" + "=" * 60)
    print("ALL INSTRUMENT TESTS PASSED ✅")
    print("=" * 60)
