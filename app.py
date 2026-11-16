"""
Streamlit App — Strategy Backtester, Alert Scanner & API Monitor
================================================================
Start with:  streamlit run app.py

Alert only. NEVER places, modifies, or cancels any order.
"""
import importlib
import inspect
import json
import re
import sys
import textwrap
import time
from datetime import date, datetime, timedelta
from html import escape as html_escape
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# Ensure project root is on sys.path so imports work
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    CHARTINK_SYNC_LOG_FILE,
    COST_PER_SIDE,
    DATA_DIR,
    DEFAULT_CAPITAL,
    DEFAULT_NOTIONAL,
    DEFAULT_QTY,
    IST,
    JOURNAL_FILE,
    MAX_WATCHLIST,
    RESULTS_DIR,
    STATE_DIR,
    UPSTOX_ACCESS_TOKEN,
    WATCHLIST_FILE,
    token_status,
)
from instruments import InstrumentResolver
from data_loader import fetch_historical, fetch_intraday
from backtest.engine import run_backtest
from scanner import is_market_open, scan_watchlist_multi
from strategies import list_strategies, get_display_name
from api_tracker import tracker
from chart_tools import render_drawing_chart
from chartink import ChartinkError
import chartink_sync
from journal import (
    entry_id as journal_entry_id,
    evaluate_entry,
    load_journal,
    record_multi_scan_alerts,
    save_journal,
    summary_stats,
    sync_journal_multi,
)

# Every registered strategy is a 5-minute intraday bullish engine.
INTRADAY_5M_STRATEGIES = frozenset({
    "three_candle_bullish",
    "vwap_bullish",
    "h1_pyramid",
    "h2_confirm_extend",
    "h3_aplus_confluence",
    "h4_trend_runner",
    "h5_adaptive_master",
})

# ── Page config ──────────────────────────────────────────────────────────
st.set_page_config(
    page_title="NSE Strategy Alerts & Backtest",
    page_icon="📈",
    layout="wide",
)

# ── Appearance / theme engine ────────────────────────────────────────────
UI_PREFS_FILE = STATE_DIR / "ui_prefs.json"

THEME_PRESETS = {
    "Midnight Emerald": {"bg": "#070b14", "accent": "#10b981", "glow": "#38bdf8", "text": "#f1f5f9"},
    "Ocean Sapphire": {"bg": "#050d1c", "accent": "#38bdf8", "glow": "#22d3ee", "text": "#eef6ff"},
    "Royal Violet": {"bg": "#0a0714", "accent": "#a78bfa", "glow": "#f472b6", "text": "#f6f2ff"},
    "Sunset Amber": {"bg": "#0c0803", "accent": "#f59e0b", "glow": "#fb923c", "text": "#fff7ed"},
    "Crimson Rose": {"bg": "#10050a", "accent": "#fb7185", "glow": "#f472b6", "text": "#fff1f2"},
    "Graphite Mono": {"bg": "#0a0a0c", "accent": "#94a3b8", "glow": "#64748b", "text": "#f4f4f5"},
}

# Every editable field lives in session state under the matching `theme_*` key.
# "Custom" is the scratch palette used while the user drags the colour pickers.
CUSTOM_THEME = "Custom"

_THEME_DEFAULTS = {
    "preset": "Midnight Emerald",
    "bg": "#070b14",
    "surface": "",
    "accent": "#10b981",
    "glow": "#38bdf8",
    "text": "#f1f5f9",
    "ambient": True,
    "compact": False,
    "hover_sidebar": True,
    "saved_themes": {},
}


def _parse_hex(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (15, 23, 42)
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (15, 23, 42)


def _is_hex_color(value) -> bool:
    text = str(value or "").strip().lstrip("#")
    return len(text) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in text)


def _clean_hex(value, fallback: str) -> str:
    """Return a normalized #rrggbb string, or the fallback when invalid."""
    if _is_hex_color(value):
        text = str(value).strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        return f"#{text.lower()}"
    return fallback


def _surface_for(bg: str) -> str:
    """Default panel colour derived from the page background."""
    return _mix_hex(_clean_hex(bg, "#070b14"), "#ffffff", 0.055)


def _hex_to_rgb(value: str) -> str:
    r, g, b = _parse_hex(value)
    return f"{r}, {g}, {b}"


def _mix_hex(base: str, other: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, float(ratio)))
    r1, g1, b1 = _parse_hex(base)
    r2, g2, b2 = _parse_hex(other)
    return "#{:02x}{:02x}{:02x}".format(
        round(r1 + (r2 - r1) * ratio),
        round(g1 + (g2 - g1) * ratio),
        round(b1 + (b2 - b1) * ratio),
    )


def _relative_luminance(value: str) -> float:
    channels = []
    for channel in _parse_hex(value):
        c = channel / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 – 21.0)."""
    l1 = _relative_luminance(foreground)
    l2 = _relative_luminance(background)
    high, low = max(l1, l2), min(l1, l2)
    return (high + 0.05) / (low + 0.05)


def _sanitize_theme_spec(spec) -> dict:
    """Validate one saved palette, filling in sensible derived values."""
    if not isinstance(spec, dict):
        return {}
    bg = _clean_hex(spec.get("bg"), _THEME_DEFAULTS["bg"])
    accent = _clean_hex(spec.get("accent"), _THEME_DEFAULTS["accent"])
    clean = {
        "bg": bg,
        "surface": _clean_hex(spec.get("surface"), _surface_for(bg)),
        "accent": accent,
        "glow": _clean_hex(spec.get("glow"), accent),
        "text": _clean_hex(spec.get("text"), _THEME_DEFAULTS["text"]),
    }
    return clean


def _theme_names() -> list[str]:
    """Curated presets, then user-saved palettes, then the scratch Custom slot."""
    saved = st.session_state.get("theme_saved_themes") or {}
    names = list(THEME_PRESETS)
    if isinstance(saved, dict):
        names.extend(name for name in saved if name not in THEME_PRESETS)
    names.append(CUSTOM_THEME)
    return names


def _theme_spec_for(name: str) -> dict:
    """Look up a palette by name from the curated presets or saved themes."""
    if name in THEME_PRESETS:
        return _sanitize_theme_spec(THEME_PRESETS[name])
    saved = st.session_state.get("theme_saved_themes") or {}
    if isinstance(saved, dict) and name in saved:
        return _sanitize_theme_spec(saved[name])
    return {}


def load_ui_prefs() -> dict:
    """Load appearance preferences saved in state/ui_prefs.json."""
    prefs = dict(_THEME_DEFAULTS)
    prefs["saved_themes"] = {}
    try:
        if UI_PREFS_FILE.exists():
            # utf-8-sig strips an optional BOM left by external editors
            data = json.loads(UI_PREFS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for key in _THEME_DEFAULTS:
                    if key in data:
                        prefs[key] = data[key]
                saved = data.get("saved_themes")
                if isinstance(saved, dict):
                    cleaned = {}
                    for name, spec in saved.items():
                        clean_name = str(name or "").strip()
                        clean_spec = _sanitize_theme_spec(spec)
                        if clean_name and clean_spec and clean_name not in THEME_PRESETS:
                            cleaned[clean_name] = clean_spec
                    prefs["saved_themes"] = cleaned
    except Exception:
        pass

    prefs["bg"] = _clean_hex(prefs.get("bg"), _THEME_DEFAULTS["bg"])
    prefs["surface"] = (
        _clean_hex(prefs.get("surface"), "") or _surface_for(prefs["bg"])
    )
    prefs["accent"] = _clean_hex(prefs.get("accent"), _THEME_DEFAULTS["accent"])
    prefs["glow"] = _clean_hex(prefs.get("glow"), prefs["accent"])
    prefs["text"] = _clean_hex(prefs.get("text"), _THEME_DEFAULTS["text"])
    prefs["ambient"] = bool(prefs.get("ambient", True))
    prefs["compact"] = bool(prefs.get("compact", False))
    prefs["hover_sidebar"] = bool(prefs.get("hover_sidebar", True))

    valid_names = list(THEME_PRESETS) + list(prefs["saved_themes"]) + [CUSTOM_THEME]
    if prefs.get("preset") not in valid_names:
        prefs["preset"] = _THEME_DEFAULTS["preset"]
    return prefs


def save_ui_prefs(prefs: dict) -> None:
    """Persist appearance preferences, skipping identical writes."""
    try:
        current = None
        if UI_PREFS_FILE.exists():
            try:
                current = json.loads(
                    UI_PREFS_FILE.read_text(encoding="utf-8-sig")
                )
            except Exception:
                current = None
        if current == prefs:
            return
        UI_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        UI_PREFS_FILE.write_text(
            json.dumps(prefs, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _persist_theme() -> None:
    """Save the current editable palette as the default appearance."""
    save_ui_prefs(
        {
            "preset": st.session_state.get("theme_preset", _THEME_DEFAULTS["preset"]),
            "bg": _clean_hex(st.session_state.get("theme_bg"), _THEME_DEFAULTS["bg"]),
            "surface": _clean_hex(
                st.session_state.get("theme_surface"),
                _surface_for(st.session_state.get("theme_bg", _THEME_DEFAULTS["bg"])),
            ),
            "accent": _clean_hex(
                st.session_state.get("theme_accent"), _THEME_DEFAULTS["accent"]
            ),
            "glow": _clean_hex(
                st.session_state.get("theme_glow"), _THEME_DEFAULTS["glow"]
            ),
            "text": _clean_hex(
                st.session_state.get("theme_text"), _THEME_DEFAULTS["text"]
            ),
            "ambient": bool(st.session_state.get("theme_ambient", True)),
            "compact": bool(st.session_state.get("theme_compact", False)),
            "hover_sidebar": bool(st.session_state.get("theme_hover_sidebar", True)),
            "saved_themes": st.session_state.get("theme_saved_themes", {}),
        }
    )


def _sync_theme_colors() -> None:
    """When a preset is chosen, load its colours into the colour pickers."""
    name = st.session_state.get("theme_preset", _THEME_DEFAULTS["preset"])
    spec = _theme_spec_for(name)
    if not spec:
        return
    st.session_state["theme_bg"] = spec["bg"]
    st.session_state["theme_surface"] = spec["surface"]
    st.session_state["theme_accent"] = spec["accent"]
    st.session_state["theme_glow"] = spec["glow"]
    st.session_state["theme_text"] = spec["text"]


def _mark_theme_custom() -> None:
    """Tweaking a colour promotes the selection to the Custom palette."""
    st.session_state["theme_preset"] = CUSTOM_THEME


def _reset_theme() -> None:
    """Restore the default palette (runs as a widget callback)."""
    bg = THEME_PRESETS["Midnight Emerald"]["bg"]
    st.session_state["theme_preset"] = "Midnight Emerald"
    st.session_state["theme_bg"] = bg
    st.session_state["theme_surface"] = _surface_for(bg)
    st.session_state["theme_accent"] = THEME_PRESETS["Midnight Emerald"]["accent"]
    st.session_state["theme_glow"] = THEME_PRESETS["Midnight Emerald"]["glow"]
    st.session_state["theme_text"] = THEME_PRESETS["Midnight Emerald"]["text"]
    st.session_state["theme_ambient"] = True
    st.session_state["theme_compact"] = False
    st.session_state["theme_hover_sidebar"] = True
    _persist_theme()


def _apply_theme_name(name: str) -> None:
    """Load a curated or saved palette into the editors (widget callback)."""
    st.session_state["theme_preset"] = name
    _sync_theme_colors()


def _capture_theme_spec() -> dict:
    """Snapshot the live colour pickers as a portable palette."""
    bg = _clean_hex(st.session_state.get("theme_bg"), _THEME_DEFAULTS["bg"])
    return {
        "bg": bg,
        "surface": _clean_hex(
            st.session_state.get("theme_surface"), _surface_for(bg)
        ),
        "accent": _clean_hex(
            st.session_state.get("theme_accent"), _THEME_DEFAULTS["accent"]
        ),
        "glow": _clean_hex(
            st.session_state.get("theme_glow"), _THEME_DEFAULTS["glow"]
        ),
        "text": _clean_hex(
            st.session_state.get("theme_text"), _THEME_DEFAULTS["text"]
        ),
    }


def _save_current_theme() -> None:
    """Store the live palette under the name typed in the studio."""
    name = str(st.session_state.get("theme_save_name", "")).strip()
    if not name:
        st.session_state["_theme_notice"] = ("error", "Give the theme a name first.")
        return
    if name in THEME_PRESETS or name == CUSTOM_THEME:
        st.session_state["_theme_notice"] = (
            "error",
            f"“{name}” is a reserved palette name. Pick another name.",
        )
        return
    saved = dict(st.session_state.get("theme_saved_themes") or {})
    saved[name] = _capture_theme_spec()
    st.session_state["theme_saved_themes"] = saved
    st.session_state["theme_preset"] = name
    st.session_state["theme_save_name"] = ""
    st.session_state["_theme_notice"] = ("success", f"Saved “{name}”.")
    _persist_theme()


def _delete_saved_theme(name: str) -> None:
    """Remove a user-saved palette."""
    saved = dict(st.session_state.get("theme_saved_themes") or {})
    saved.pop(name, None)
    st.session_state["theme_saved_themes"] = saved
    if st.session_state.get("theme_preset") == name:
        st.session_state["theme_preset"] = CUSTOM_THEME
    st.session_state["_theme_notice"] = ("info", f"Deleted “{name}”.")
    _persist_theme()


def _active_theme() -> dict:
    """Resolve the live editable palette into every derived CSS colour."""
    preset_name = st.session_state.get("theme_preset", _THEME_DEFAULTS["preset"])
    if preset_name not in _theme_names():
        preset_name = CUSTOM_THEME

    bg = _clean_hex(st.session_state.get("theme_bg"), _THEME_DEFAULTS["bg"])
    surface = _clean_hex(st.session_state.get("theme_surface"), _surface_for(bg))
    accent = _clean_hex(
        st.session_state.get("theme_accent"), _THEME_DEFAULTS["accent"]
    )
    glow = _clean_hex(st.session_state.get("theme_glow"), accent)
    text = _clean_hex(st.session_state.get("theme_text"), _THEME_DEFAULTS["text"])

    return {
        "preset": preset_name,
        "bg": bg,
        "accent": accent,
        "glow": glow,
        "surface": surface,
        "surface2": _mix_hex(bg, "#ffffff", 0.028),
        "accent2": _mix_hex(accent, "#ffffff", 0.22),
        "accent3": _mix_hex(accent, "#ffffff", 0.46),
        "text": text,
        "text_strong": text,
        "text_soft": _mix_hex(text, surface, 0.16),
        "muted": _mix_hex(text, surface, 0.38),
        "muted2": _mix_hex(text, surface, 0.66),
        "indigo": "#6366f1",
        "on_accent": "#0b1220" if _relative_luminance(accent) >= 0.30 else "#f8fafc",
        "ambient": bool(st.session_state.get("theme_ambient", True)),
        "compact": bool(st.session_state.get("theme_compact", False)),
        "hover_sidebar": bool(st.session_state.get("theme_hover_sidebar", True)),
    }


def _theme_css() -> str:
    """Generate the runtime theme layer (CSS variables + shell overrides)."""
    t = _active_theme()
    accent_rgb = _hex_to_rgb(t["accent"])
    glow_rgb = _hex_to_rgb(t["glow"])
    bg_rgb = _hex_to_rgb(t["bg"])
    surface_rgb = _hex_to_rgb(t["surface"])
    surface2_rgb = _hex_to_rgb(t["surface2"])
    muted_rgb = _hex_to_rgb(t["muted"])
    indigo_rgb = _hex_to_rgb(t["indigo"])

    ambient_css = ""
    if t["ambient"]:
        ambient_css = f"""
@keyframes ambientDrift {{
    0%   {{ background-position: 0% 0%, 100% 0%, 50% 100%, 0px 0px; }}
    50%  {{ background-position: 6% 3%, 94% 4%, 46% 96%, 14px 14px; }}
    100% {{ background-position: 0% 0%, 100% 0%, 50% 100%, 0px 0px; }}
}}
.stApp {{
    animation: ambientDrift 34s ease-in-out infinite;
}}
@media (prefers-reduced-motion: reduce) {{
    .stApp {{ animation: none !important; }}
}}
"""

    compact_css = ""
    if t["compact"]:
        compact_css = """
section[data-testid="stSidebar"] div[data-testid="stRadio"] label {
    padding: 6px 10px !important;
    min-height: 34px !important;
}
section[data-testid="stSidebar"] div[data-testid="stRadio"] label p {
    font-size: 0.8rem !important;
}
section[data-testid="stSidebar"] .sidebar-brand {
    padding: 8px 10px 10px !important;
}
section[data-testid="stSidebar"] .telemetry-row {
    padding: 5px 9px !important;
}
section[data-testid="stSidebar"] .side-label {
    margin: 8px 4px 4px !important;
}
"""

    rail_css = ""
    if t["hover_sidebar"]:
        rail_css = f"""
/* Collapsed sidebar becomes a slim rail that peeks open on hover.
   The native collapse / expand buttons are left untouched. */
@media screen and (min-width: 901px) and (hover: hover) {{
section[data-testid="stSidebar"][data-collapsed="true"] {{
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    bottom: 0 !important;
    height: 100vh !important;
    min-width: 280px !important;
    max-width: 480px !important;
    margin: 0 !important;
    transform: translateX(calc(-100% + 16px)) !important;
    clip-path: inset(64px 0 0 0) !important;
    transition: transform 240ms cubic-bezier(0.22, 0.7, 0.3, 1),
                clip-path 240ms ease, box-shadow 240ms ease !important;
    z-index: 1000001 !important;
    border-radius: 0 14px 14px 0 !important;
    border-right: 1px solid rgba({accent_rgb}, 0.45) !important;
    box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.32), 14px 0 34px rgba(0, 0, 0, 0.34) !important;
    overflow: hidden !important;
}}
section[data-testid="stSidebar"][data-collapsed="true"]:hover,
section[data-testid="stSidebar"][data-collapsed="true"]:focus-within {{
    transform: translateX(0) !important;
    clip-path: inset(0 0 0 0) !important;
    box-shadow: 26px 0 70px rgba(0, 0, 0, 0.55) !important;
}}
section[data-testid="stSidebar"][data-collapsed="true"]::after {{
    content: "❯";
    position: absolute;
    right: 0;
    top: 50%;
    transform: translateY(-50%);
    width: 16px;
    height: 88px;
    display: grid;
    place-items: center;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 9px;
    font-weight: 700;
    color: {t["accent2"]};
    background: linear-gradient(180deg, rgba({accent_rgb}, 0.42), rgba({accent_rgb}, 0.12));
    border-left: 1px solid rgba({accent_rgb}, 0.55);
    box-shadow: 0 0 20px rgba({accent_rgb}, 0.38);
    pointer-events: none;
    opacity: 1;
    transition: opacity 160ms ease;
}}
section[data-testid="stSidebar"][data-collapsed="true"]:hover::after,
section[data-testid="stSidebar"][data-collapsed="true"]:focus-within::after {{
    opacity: 0;
}}
}}
@media screen and (prefers-reduced-motion: reduce) {{
section[data-testid="stSidebar"][data-collapsed="true"] {{
    transition: none !important;
}}
}}
"""

    return f"""
<style>
:root {{
    --bg: {t["bg"]};
    --bg-rgb: {bg_rgb};
    --surface: {t["surface"]};
    --surface-rgb: {surface_rgb};
    --surface-2: {t["surface2"]};
    --surface-2-rgb: {surface2_rgb};
    --accent: {t["accent"]};
    --accent-rgb: {accent_rgb};
    --accent-2: {t["accent2"]};
    --accent-3: {t["accent3"]};
    --on-accent: {t["on_accent"]};
    --glow: {t["glow"]};
    --glow-rgb: {glow_rgb};
    --text: {t["text"]};
    --text-strong: {t["text_strong"]};
    --text-soft: {t["text_soft"]};
    --muted: {t["muted"]};
    --muted2: {t["muted2"]};
    --muted-rgb: {muted_rgb};
    --indigo-rgb: {indigo_rgb};
}}

.stApp {{
    background-color: {t["bg"]} !important;
    background-image:
        radial-gradient(1200px 560px at 6% -8%, rgba({accent_rgb}, 0.12), transparent 60%),
        radial-gradient(1050px 500px at 96% -4%, rgba({glow_rgb}, 0.09), transparent 58%),
        radial-gradient(900px 480px at 50% 105%, rgba({indigo_rgb}, 0.06), transparent 60%),
        radial-gradient(rgba({muted_rgb}, 0.055) 1px, transparent 1px) !important;
    background-size: auto, auto, auto, 28px 28px !important;
    background-attachment: fixed !important;
}}

header[data-testid="stHeader"] {{
    background: rgba({bg_rgb}, 0.65) !important;
}}

section[data-testid="stSidebar"] {{
    background: linear-gradient(180deg, {t["surface2"]} 0%, {t["bg"]} 100%) !important;
    border-right: 1px solid rgba({accent_rgb}, 0.16) !important;
}}

/* Native controls follow the chosen accent */
.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"],
.stFormSubmitButton > button[kind="primary"],
.stFormSubmitButton > button[data-testid="stBaseButton-primary"] {{
    background: linear-gradient(135deg, {t["accent"]} 0%, {t["accent2"]} 100%) !important;
    color: {t["on_accent"]} !important;
    border: 1px solid rgba({accent_rgb}, 0.55) !important;
    box-shadow: 0 6px 22px rgba({accent_rgb}, 0.24) !important;
}}
.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover,
.stFormSubmitButton > button[kind="primary"]:hover,
.stFormSubmitButton > button[data-testid="stBaseButton-primary"]:hover {{
    background: linear-gradient(135deg, {t["accent2"]} 0%, {t["accent"]} 100%) !important;
    color: {t["on_accent"]} !important;
    box-shadow: 0 10px 30px rgba({accent_rgb}, 0.34) !important;
}}
.stButton > button:focus-visible,
.stFormSubmitButton > button:focus-visible,
.stDownloadButton > button:focus-visible {{
    outline: 2px solid rgba({accent_rgb}, 0.7) !important;
    outline-offset: 2px !important;
}}
.stTabs [data-baseweb="tab-highlight"] {{
    background-color: {t["accent"]} !important;
}}
.stTabs [aria-selected="true"] {{
    color: {t["accent2"]} !important;
}}
div[data-baseweb="slider"] div[role="slider"] {{
    background-color: {t["accent"]} !important;
}}

/* Sidebar nav / brand accents */
section[data-testid="stSidebar"] div[data-testid="stRadio"] label:hover {{
    background: rgba({accent_rgb}, 0.08) !important;
    border-color: rgba({accent_rgb}, 0.24) !important;
}}
section[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input:checked) {{
    background: linear-gradient(90deg, rgba({accent_rgb}, 0.20), rgba({accent_rgb}, 0.03)) !important;
    border-color: rgba({accent_rgb}, 0.45) !important;
    box-shadow: inset 3px 0 0 {t["accent"]}, 0 6px 18px rgba(0, 0, 0, 0.22) !important;
}}
section[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input:checked) p {{
    color: {t["text"]} !important;
}}
.sidebar-brand {{
    background: linear-gradient(135deg, rgba({accent_rgb}, 0.12), rgba({surface_rgb}, 0.35)) !important;
    border-color: rgba({accent_rgb}, 0.20) !important;
}}
.sidebar-brand::after {{
    background: radial-gradient(120px 60px at 100% 0%, rgba({accent_rgb}, 0.24), transparent 70%) !important;
}}
.brand-logo {{
    background: linear-gradient(135deg, rgba({accent_rgb}, 0.24), rgba({glow_rgb}, 0.14)) !important;
    border-color: rgba({accent_rgb}, 0.38) !important;
}}
.sidebar-brand .brand-title {{
    background: linear-gradient(135deg, {t["accent"]} 0%, {t["glow"]} 100%) !important;
    -webkit-background-clip: text !important;
    background-clip: text !important;
    -webkit-text-fill-color: transparent !important;
}}
.brand-live {{
    background: rgba({accent_rgb}, 0.12) !important;
    border-color: rgba({accent_rgb}, 0.30) !important;
    color: {t["accent2"]} !important;
}}
.side-label::after {{
    background: linear-gradient(90deg, rgba({accent_rgb}, 0.30), transparent) !important;
}}
{ambient_css}
{compact_css}
{rail_css}
</style>
"""


_saved_ui_prefs = load_ui_prefs()
if "theme_preset" not in st.session_state:
    st.session_state["theme_preset"] = _saved_ui_prefs["preset"]
if "theme_bg" not in st.session_state:
    st.session_state["theme_bg"] = _saved_ui_prefs["bg"]
if "theme_surface" not in st.session_state:
    st.session_state["theme_surface"] = _saved_ui_prefs["surface"]
if "theme_accent" not in st.session_state:
    st.session_state["theme_accent"] = _saved_ui_prefs["accent"]
if "theme_glow" not in st.session_state:
    st.session_state["theme_glow"] = _saved_ui_prefs["glow"]
if "theme_text" not in st.session_state:
    st.session_state["theme_text"] = _saved_ui_prefs["text"]
if "theme_ambient" not in st.session_state:
    st.session_state["theme_ambient"] = bool(_saved_ui_prefs["ambient"])
if "theme_compact" not in st.session_state:
    st.session_state["theme_compact"] = bool(_saved_ui_prefs["compact"])
if "theme_hover_sidebar" not in st.session_state:
    st.session_state["theme_hover_sidebar"] = bool(_saved_ui_prefs["hover_sidebar"])
if "theme_saved_themes" not in st.session_state:
    st.session_state["theme_saved_themes"] = dict(_saved_ui_prefs["saved_themes"])


# ── Premium CSS injection ─────────────────────────────────────────────────
st.markdown("""
<style>
/* ═══════════════════════════════════════════════════
   GOOGLE FONTS
═══════════════════════════════════════════════════ */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap');

/* ═══════════════════════════════════════════════════
   GLOBAL BASE & ATMOSPHERIC GRID
═══════════════════════════════════════════════════ */
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

::selection {
    background: rgba(var(--accent-rgb), 0.30);
    color: #ffffff;
}

.stApp {
    background-color: var(--bg);
    background-image:
        radial-gradient(1200px 560px at 6% -8%, rgba(var(--accent-rgb), 0.10), transparent 60%),
        radial-gradient(1050px 500px at 96% -4%, rgba(var(--glow-rgb), 0.08), transparent 58%),
        radial-gradient(900px 480px at 50% 105%, rgba(var(--indigo-rgb), 0.05), transparent 60%),
        radial-gradient(rgba(var(--muted-rgb), 0.055) 1px, transparent 1px);
    background-size: auto, auto, auto, 28px 28px;
    background-attachment: fixed;
    min-height: 100vh;
}

/* Cleaner app chrome */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
[data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
header[data-testid="stHeader"] {
    background: rgba(var(--bg-rgb), 0.65) !important;
    backdrop-filter: blur(10px);
}

/* ═══════════════════════════════════════════════════
   SIDEBAR
═══════════════════════════════════════════════════ */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, var(--surface-2) 0%, var(--bg) 100%) !important;
    border-right: 1px solid rgba(var(--muted-rgb), 0.14) !important;
}

section[data-testid="stSidebar"] .stMarkdown h1,
section[data-testid="stSidebar"] .stMarkdown h2,
section[data-testid="stSidebar"] .stMarkdown h3 {
    color: var(--text) !important;
}

section[data-testid="stSidebar"] .stSelectbox label,
section[data-testid="stSidebar"] .stNumberInput label,
section[data-testid="stSidebar"] .stRadio label,
section[data-testid="stSidebar"] p {
    color: var(--text-soft) !important;
}

section[data-testid="stSidebar"] div[data-baseweb="select"] > div,
section[data-testid="stSidebar"] div[data-testid="stNumberInputContainer"] {
    background-color: rgba(var(--surface-rgb), 0.72) !important;
    border: 1px solid rgba(var(--accent-rgb), 0.24) !important;
    border-radius: 10px !important;
    transition: border-color 0.18s ease, box-shadow 0.18s ease !important;
}

section[data-testid="stSidebar"] div[data-baseweb="select"] > div:hover,
section[data-testid="stSidebar"] div[data-testid="stNumberInputContainer"]:hover {
    border-color: rgba(var(--accent-rgb), 0.48) !important;
}

section[data-testid="stSidebar"] div[data-baseweb="select"] > div:focus-within,
section[data-testid="stSidebar"] div[data-testid="stNumberInputContainer"]:focus-within {
    border-color: rgba(var(--accent-rgb), 0.72) !important;
    box-shadow: 0 0 0 2px rgba(var(--accent-rgb), 0.16) !important;
}

/* ═══════════════════════════════════════════════════
   MAIN CONTENT AREA
═══════════════════════════════════════════════════ */
.main .block-container {
    padding-top: 1.15rem;
    padding-left: 2rem;
    padding-right: 2rem;
    max-width: 1480px;
}

/* ═══════════════════════════════════════════════════
   TYPOGRAPHY
═══════════════════════════════════════════════════ */
h1, h2, h3, h4, h5, h6 {
    font-family: 'Space Grotesk', sans-serif !important;
    color: var(--text) !important;
    letter-spacing: -0.01em;
}

p, label, span, div {
    color: var(--text-soft);
}

/* ═══════════════════════════════════════════════════
   TOP COMMAND RIBBON
═══════════════════════════════════════════════════ */
.top-command-ribbon {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    background: linear-gradient(90deg, rgba(var(--surface-rgb), 0.88) 0%, rgba(var(--surface-2-rgb), 0.88) 100%);
    border: 1px solid rgba(var(--muted-rgb), 0.16);
    border-radius: 12px;
    padding: 9px 16px;
    margin-bottom: 16px;
    backdrop-filter: blur(14px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
}

.ribbon-group {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
}

.ribbon-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 11px;
    border-radius: 999px;
    font-size: 0.73rem;
    font-weight: 600;
    font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
    background: rgba(30, 41, 59, 0.65);
    border: 1px solid rgba(var(--muted-rgb), 0.16);
    color: var(--text-soft);
    white-space: nowrap;
}

.ribbon-pill.emerald {
    background: rgba(var(--accent-rgb), 0.12);
    border-color: rgba(var(--accent-rgb), 0.35);
    color: var(--accent-2);
}

.ribbon-pill.amber {
    background: rgba(245, 158, 11, 0.12);
    border-color: rgba(245, 158, 11, 0.35);
    color: #fbbf24;
}

.ribbon-pill.rose {
    background: rgba(244, 63, 94, 0.12);
    border-color: rgba(244, 63, 94, 0.35);
    color: #fb7185;
}

.ribbon-pill.cyan {
    background: rgba(var(--glow-rgb), 0.12);
    border-color: rgba(var(--glow-rgb), 0.32);
    color: var(--glow);
}

/* ═══════════════════════════════════════════════════
   CUSTOM METRIC CARDS
═══════════════════════════════════════════════════ */
.metric-card {
    background: linear-gradient(165deg, rgba(var(--surface-rgb), 0.92) 0%, rgba(10, 16, 30, 0.88) 100%);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-radius: 14px;
    padding: 16px 18px;
    border: 1px solid rgba(var(--muted-rgb), 0.14);
    transition: transform 0.18s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    margin-bottom: 10px;
    position: relative;
    overflow: hidden;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.22);
}

.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    border-radius: 14px 14px 0 0;
}

.metric-card::after {
    content: '';
    position: absolute;
    top: -32px; right: -32px;
    width: 96px; height: 96px;
    border-radius: 50%;
    pointer-events: none;
    opacity: 0.55;
}

.metric-card.green::before { background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
.metric-card.gold::before  { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
.metric-card.blue::before  { background: linear-gradient(90deg, #3b82f6, var(--glow)); }
.metric-card.red::before   { background: linear-gradient(90deg, #f43f5e, #fb7185); }
.metric-card.purple::before{ background: linear-gradient(90deg, #8b5cf6, #c084fc); }
.metric-card.cyan::before  { background: linear-gradient(90deg, #06b6d4, var(--glow)); }

.metric-card.green::after  { background: radial-gradient(circle, rgba(var(--accent-rgb),0.16), transparent 70%); }
.metric-card.gold::after   { background: radial-gradient(circle, rgba(245,158,11,0.15), transparent 70%); }
.metric-card.blue::after   { background: radial-gradient(circle, rgba(59,130,246,0.16), transparent 70%); }
.metric-card.red::after    { background: radial-gradient(circle, rgba(244,63,94,0.16), transparent 70%); }
.metric-card.purple::after { background: radial-gradient(circle, rgba(139,92,246,0.16), transparent 70%); }
.metric-card.cyan::after   { background: radial-gradient(circle, rgba(6,182,212,0.16), transparent 70%); }

.metric-card:hover {
    transform: translateY(-2px);
    border-color: rgba(var(--accent-rgb), 0.35);
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.34), 0 0 20px rgba(var(--accent-rgb), 0.08);
}

.metric-card .metric-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
}

.metric-card .metric-label {
    font-size: 0.71rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0;
}

.metric-card .metric-icon {
    font-size: 1.05rem;
    line-height: 1;
    opacity: 0.92;
}

.metric-card .metric-value {
    font-family: 'Space Grotesk', 'JetBrains Mono', sans-serif;
    font-variant-numeric: tabular-nums;
    font-size: 1.65rem;
    font-weight: 700;
    color: var(--text-strong);
    line-height: 1.15;
}

.metric-card .metric-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.73rem;
    font-weight: 600;
    margin-top: 6px;
    color: var(--accent-2);
}

.metric-card .metric-delta.negative {
    color: #fb7185;
}

/* ═══════════════════════════════════════════════════
   SECTION HEADERS (Gradient Banner)
═══════════════════════════════════════════════════ */
.section-header {
    background: linear-gradient(135deg, rgba(var(--surface-rgb), 0.92) 0%, rgba(var(--accent-rgb), 0.09) 55%, rgba(var(--glow-rgb), 0.08) 100%);
    border: 1px solid rgba(var(--accent-rgb), 0.22);
    border-radius: 16px;
    padding: 20px 26px;
    margin-bottom: 20px;
    position: relative;
    overflow: hidden;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.24);
}

.section-header::after {
    content: '';
    position: absolute;
    top: -60%; right: -10%;
    width: 320px; height: 320px;
    background: radial-gradient(circle, rgba(var(--accent-rgb),0.10) 0%, transparent 70%);
    border-radius: 50%;
    pointer-events: none;
}

.section-header h1 {
    font-family: 'Space Grotesk', sans-serif !important;
    font-size: 1.72rem !important;
    font-weight: 700 !important;
    background: linear-gradient(135deg, var(--text-strong) 0%, var(--accent-3) 55%, var(--glow) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin: 0 !important;
    padding: 0 !important;
}

.section-header .sub {
    font-size: 0.85rem;
    color: var(--muted);
    margin-top: 5px;
}

/* ═══════════════════════════════════════════════════
   COMMAND CENTER & WATCHLIST SNAPSHOT CARDS
═══════════════════════════════════════════════════ */
.wl-snap-card {
    background: linear-gradient(165deg, rgba(var(--surface-rgb), 0.90) 0%, rgba(var(--surface-2-rgb), 0.88) 100%);
    border: 1px solid rgba(var(--muted-rgb), 0.14);
    border-radius: 13px;
    padding: 13px 15px;
    margin-bottom: 12px;
    transition: transform 0.16s ease, border-color 0.18s ease, box-shadow 0.18s ease;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.20);
}

.wl-snap-card:hover {
    transform: translateY(-2px);
    border-color: rgba(var(--accent-rgb), 0.38);
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.30);
}

.wl-snap-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
}

.wl-snap-sym {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 0.95rem;
    color: var(--text-strong);
    letter-spacing: 0.02em;
}

.wl-snap-chg {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 6px;
}

.wl-snap-chg.up {
    background: rgba(var(--accent-rgb), 0.14);
    color: var(--accent-2);
    border: 1px solid rgba(var(--accent-rgb), 0.30);
}

.wl-snap-chg.down {
    background: rgba(244, 63, 94, 0.14);
    color: #fb7185;
    border: 1px solid rgba(244, 63, 94, 0.30);
}

.wl-snap-chg.flat {
    background: rgba(var(--muted-rgb), 0.12);
    color: var(--muted);
    border: 1px solid rgba(var(--muted-rgb), 0.22);
}

.wl-snap-mid {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 8px;
    margin: 6px 0 8px;
}

.wl-snap-price {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.18rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1.1;
}

.wl-snap-sub {
    font-size: 0.69rem;
    color: var(--muted2);
    margin-top: 3px;
}

.wl-range-track {
    width: 100%;
    height: 4px;
    background: rgba(var(--muted-rgb), 0.16);
    border-radius: 999px;
    overflow: hidden;
    position: relative;
}

.wl-range-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--glow), var(--accent));
}

.wl-range-labels {
    display: flex;
    justify-content: space-between;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.66rem;
    color: var(--muted2);
    margin-top: 4px;
}

.cc-panel {
    background: rgba(var(--surface-rgb), 0.72);
    border: 1px solid rgba(var(--muted-rgb), 0.14);
    border-radius: 15px;
    padding: 18px 20px;
    margin-bottom: 16px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.20);
}

.sig-feed-card {
    background: rgba(var(--surface-rgb), 0.85);
    border: 1px solid rgba(var(--muted-rgb), 0.14);
    border-left: 3px solid var(--accent);
    border-radius: 11px;
    padding: 11px 14px;
    margin-bottom: 9px;
    transition: border-color 0.15s ease, background 0.15s ease;
}

.sig-feed-card.loss {
    border-left-color: #f43f5e;
}

.sig-feed-card.open {
    border-left-color: #f59e0b;
}

.sig-feed-card:hover {
    background: rgba(30, 41, 59, 0.72);
}

/* ═══════════════════════════════════════════════════
   GLASS PANELS
═══════════════════════════════════════════════════ */
.glass-panel {
    background: rgba(var(--surface-rgb), 0.72);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(var(--muted-rgb), 0.13);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 20px;
}

/* ═══════════════════════════════════════════════════
   INFO / WARNING / ERROR BOXES
═══════════════════════════════════════════════════ */
div[data-testid="stAlert"] {
    border-radius: 10px !important;
    border: none !important;
}

div[data-testid="stAlert"][kind="info"] {
    background: rgba(79, 172, 254, 0.08) !important;
    border-left: 3px solid var(--glow) !important;
}

div[data-testid="stAlert"][kind="success"] {
    background: rgba(var(--accent-rgb), 0.08) !important;
    border-left: 3px solid var(--accent) !important;
}

div[data-testid="stAlert"][kind="warning"] {
    background: rgba(255, 215, 0, 0.08) !important;
    border-left: 3px solid #fbbf24 !important;
}

div[data-testid="stAlert"][kind="error"] {
    background: rgba(255, 75, 75, 0.1) !important;
    border-left: 3px solid #f43f5e !important;
}

/* ═══════════════════════════════════════════════════
   BUTTONS
═══════════════════════════════════════════════════ */
.stButton > button,
.stFormSubmitButton > button,
.stDownloadButton > button {
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    border-radius: 10px !important;
    min-height: 2.55rem !important;
    letter-spacing: 0.01em !important;
    transition: transform 0.15s ease, box-shadow 0.2s ease,
                background 0.2s ease, border-color 0.2s ease !important;
}

/* Button labels inherit the button colour. Without this the global
   p/span/div colour rule makes primary button text unreadable. */
.stButton button p,
.stButton button span,
.stButton button div,
.stFormSubmitButton button p,
.stFormSubmitButton button span,
.stFormSubmitButton button div,
.stDownloadButton button p,
.stDownloadButton button span,
.stDownloadButton button div {
    color: inherit !important;
    font-weight: inherit !important;
    letter-spacing: inherit !important;
}

.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"],
.stFormSubmitButton > button[kind="primary"],
.stFormSubmitButton > button[data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%) !important;
    color: var(--on-accent) !important;
    font-weight: 700 !important;
    font-family: 'Space Grotesk', sans-serif !important;
    border: 1px solid rgba(var(--accent-rgb), 0.55) !important;
    padding: 0.6rem 1.6rem !important;
    letter-spacing: 0.02em !important;
    text-shadow: none !important;
    box-shadow: 0 6px 22px rgba(var(--accent-rgb), 0.22) !important;
}

.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover,
.stFormSubmitButton > button[kind="primary"]:hover,
.stFormSubmitButton > button[data-testid="stBaseButton-primary"]:hover {
    background: linear-gradient(135deg, var(--accent-2) 0%, var(--accent) 100%) !important;
    color: var(--on-accent) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 10px 30px rgba(var(--accent-rgb), 0.35) !important;
}

.stButton > button[kind="primary"]:active,
.stButton > button[data-testid="stBaseButton-primary"]:active {
    transform: translateY(0) !important;
    box-shadow: 0 4px 14px rgba(var(--accent-rgb), 0.28) !important;
}

.stButton > button:focus-visible,
.stFormSubmitButton > button:focus-visible,
.stDownloadButton > button:focus-visible {
    outline: 2px solid rgba(var(--accent-rgb), 0.7) !important;
    outline-offset: 2px !important;
}

.stButton > button:not([kind="primary"]):not([data-testid="stBaseButton-primary"]) {
    background: rgba(255, 255, 255, 0.045) !important;
    color: var(--text-soft) !important;
    border: 1px solid rgba(255, 255, 255, 0.14) !important;
    padding: 0.55rem 1.1rem !important;
}

.stButton > button:not([kind="primary"]):not([data-testid="stBaseButton-primary"]):hover {
    background: rgba(var(--accent-rgb), 0.09) !important;
    border-color: rgba(var(--accent-rgb), 0.38) !important;
    color: var(--text-strong) !important;
    transform: translateY(-1px) !important;
}

.stButton > button:disabled,
.stFormSubmitButton > button:disabled {
    background: rgba(255, 255, 255, 0.035) !important;
    color: #475569 !important;
    border-color: rgba(255, 255, 255, 0.07) !important;
    box-shadow: none !important;
    transform: none !important;
    cursor: not-allowed !important;
}

.stButton > button:disabled p,
.stFormSubmitButton > button:disabled p {
    color: #475569 !important;
}

/* ═══════════════════════════════════════════════════
   FORM / INPUTS  (colors handled by [theme] config)
═══════════════════════════════════════════════════ */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stDateInput > div > div > input {
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}

.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
    border-color: rgba(var(--accent-rgb), 0.55) !important;
    box-shadow: 0 0 0 2px rgba(var(--accent-rgb), 0.12) !important;
    outline: none !important;
}

/* Selectbox wrapper border */
.stSelectbox > div > div > div {
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
    transition: border-color 0.2s ease !important;
}

.stSelectbox > div > div > div:focus-within {
    border-color: rgba(var(--accent-rgb), 0.55) !important;
}

/* Label styling */
.stTextInput label,
.stNumberInput label,
.stSelectbox label,
.stDateInput label,
.stCheckbox span {
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    letter-spacing: 0.03em !important;
    opacity: 0.8;
}

/* ═══════════════════════════════════════════════════
   DATAFRAMES / TABLES
═══════════════════════════════════════════════════ */
div[data-testid="stDataFrame"] {
    border-radius: 12px !important;
    overflow: hidden !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
}

/* ═══════════════════════════════════════════════════
   EXPANDERS
═══════════════════════════════════════════════════ */
div[data-testid="stExpander"] {
    background: rgba(var(--surface-rgb), 0.5) !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    border-radius: 12px !important;
}

div[data-testid="stExpander"] summary {
    color: var(--muted) !important;
    font-weight: 500 !important;
}

/* ═══════════════════════════════════════════════════
   DIVIDER
═══════════════════════════════════════════════════ */
hr {
    border: none !important;
    height: 1px !important;
    background: linear-gradient(90deg, transparent, rgba(var(--accent-rgb),0.2), transparent) !important;
    margin: 1.5rem 0 !important;
}

/* ═══════════════════════════════════════════════════
   MARKET STATUS PULSE ANIMATION
═══════════════════════════════════════════════════ */
@keyframes pulse-green {
    0%   { box-shadow: 0 0 0 0 rgba(var(--accent-rgb), 0.6); }
    70%  { box-shadow: 0 0 0 10px rgba(var(--accent-rgb), 0); }
    100% { box-shadow: 0 0 0 0 rgba(var(--accent-rgb), 0); }
}

@keyframes pulse-red {
    0%   { box-shadow: 0 0 0 0 rgba(255, 75, 75, 0.6); }
    70%  { box-shadow: 0 0 0 10px rgba(255, 75, 75, 0); }
    100% { box-shadow: 0 0 0 0 rgba(255, 75, 75, 0); }
}

.market-status-open, .market-status-closed {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 10px 20px;
    border-radius: 50px;
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    font-size: 0.9rem;
    margin: 8px 0;
}

.market-status-open {
    background: rgba(var(--accent-rgb), 0.1);
    border: 1px solid rgba(var(--accent-rgb), 0.3);
    color: var(--accent);
}

.market-status-closed {
    background: rgba(255, 75, 75, 0.1);
    border: 1px solid rgba(255, 75, 75, 0.3);
    color: #f43f5e;
}

.pulse-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}

.pulse-dot.green {
    background: var(--accent);
    animation: pulse-green 2s infinite;
}

.pulse-dot.red {
    background: #f43f5e;
    animation: pulse-red 2s infinite;
}

/* ═══════════════════════════════════════════════════
   WATCHLIST BADGE
═══════════════════════════════════════════════════ */
.wl-badge {
    display: inline-block;
    background: rgba(var(--accent-rgb), 0.15);
    border: 1px solid rgba(var(--accent-rgb), 0.3);
    border-radius: 20px;
    padding: 3px 12px;
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--accent);
    margin: 2px 4px;
    font-family: 'Space Grotesk', sans-serif;
    letter-spacing: 0.05em;
}

.wl-badge.unresolved {
    background: rgba(255, 75, 75, 0.1);
    border-color: rgba(255, 75, 75, 0.3);
    color: #f43f5e;
}

/* ═══════════════════════════════════════════════════
   SIGNAL BADGE
═══════════════════════════════════════════════════ */
.signal-buy {
    background: rgba(var(--accent-rgb), 0.15);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: 700;
    font-size: 0.75rem;
    font-family: 'Space Grotesk', sans-serif;
}

.signal-sell {
    background: rgba(255, 75, 75, 0.15);
    border: 1px solid #f43f5e;
    color: #f43f5e;
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: 700;
    font-size: 0.75rem;
    font-family: 'Space Grotesk', sans-serif;
}

/* ═══════════════════════════════════════════════════
   SIDEBAR BRAND
═══════════════════════════════════════════════════ */
.sidebar-brand {
    text-align: center;
    padding: 10px 0 20px;
}

.sidebar-brand .brand-icon {
    font-size: 2.5rem;
    display: block;
    margin-bottom: 8px;
}

.sidebar-brand .brand-title {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 1.1rem;
    font-weight: 700;
    background: linear-gradient(135deg, var(--accent) 0%, var(--glow) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

.sidebar-brand .brand-sub {
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 2px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
}

.token-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
    font-family: 'Space Grotesk', sans-serif;
    width: 100%;
    justify-content: center;
    margin: 4px 0;
}

.token-badge.ok {
    background: rgba(var(--accent-rgb), 0.1);
    border: 1px solid rgba(var(--accent-rgb), 0.3);
    color: var(--accent);
}

.token-badge.missing {
    background: rgba(255, 75, 75, 0.1);
    border: 1px solid rgba(255, 75, 75, 0.3);
    color: #f43f5e;
}

.telemetry-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    background: rgba(255,255,255,0.03);
    border-radius: 8px;
    margin: 4px 0;
    border: 1px solid rgba(255,255,255,0.05);
}

.telemetry-row .tl-label {
    font-size: 0.72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

.telemetry-row .tl-value {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--muted);
}

/* ═══════════════════════════════════════════════════
   NAV RADIO (sidebar)
═══════════════════════════════════════════════════ */
div[data-testid="stRadio"] > div {
    gap: 3px !important;
}

div[data-testid="stRadio"] label {
    padding: 9px 14px !important;
    border-radius: 10px !important;
    border: 1px solid transparent !important;
    transition: background 0.18s ease, border-color 0.18s ease,
                box-shadow 0.18s ease !important;
    cursor: pointer !important;
}

div[data-testid="stRadio"] label p {
    font-size: 0.9rem !important;
    font-weight: 500 !important;
}

div[data-testid="stRadio"] label:hover {
    background: rgba(var(--accent-rgb), 0.07) !important;
    border-color: rgba(var(--accent-rgb), 0.22) !important;
}

section[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input:checked) {
    background: linear-gradient(90deg, rgba(var(--accent-rgb), 0.16), rgba(var(--accent-rgb), 0.04)) !important;
    border-color: rgba(var(--accent-rgb), 0.4) !important;
    box-shadow: inset 3px 0 0 var(--accent), 0 4px 16px rgba(var(--accent-rgb), 0.08) !important;
}

section[data-testid="stSidebar"] div[data-testid="stRadio"] label:has(input:checked) p {
    color: var(--text-strong) !important;
    font-weight: 600 !important;
}

/* ═══════════════════════════════════════════════════
   CAPTION / FOOTER TEXT
═══════════════════════════════════════════════════ */
.stCaption, caption {
    color: var(--muted) !important;
    font-size: 0.78rem !important;
    line-height: 1.45 !important;
}

/* ═══════════════════════════════════════════════════
   TOGGLE
═══════════════════════════════════════════════════ */
.stToggle span {
    color: var(--muted) !important;
}

/* ═══════════════════════════════════════════════════
   SPINNER
═══════════════════════════════════════════════════ */
div[data-testid="stSpinner"] p {
    color: var(--accent) !important;
}

/* ═══════════════════════════════════════════════════
   DOWNLOAD BUTTON
═══════════════════════════════════════════════════ */
.stDownloadButton > button {
    background: rgba(79, 172, 254, 0.1) !important;
    border: 1px solid rgba(79, 172, 254, 0.3) !important;
    color: var(--glow) !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    transition: all 0.2s ease !important;
}

.stDownloadButton > button:hover {
    background: rgba(79, 172, 254, 0.2) !important;
    border-color: rgba(79, 172, 254, 0.55) !important;
    color: #7dd3fc !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(79, 172, 254, 0.22) !important;
}

/* ═══════════════════════════════════════════════════
   FORM SUBMIT BUTTON
═══════════════════════════════════════════════════ */
div[data-testid="stForm"] {
    background: rgba(var(--surface-rgb), 0.5);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 16px;
    padding: 20px;
}

/* ═══════════════════════════════════════════════════
   SCROLLBAR
═══════════════════════════════════════════════════ */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: rgba(255,255,255,0.02); }
::-webkit-scrollbar-thumb { background: rgba(var(--accent-rgb), 0.2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(var(--accent-rgb), 0.4); }

/* ═══════════════════════════════════════════════════
   STATE MACHINE & STRATEGY FLOW
═══════════════════════════════════════════════════ */
.state-flow-card {
    background: rgba(var(--surface-rgb), 0.7);
    border: 1px solid rgba(var(--accent-rgb), 0.2);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
    transition: all 0.2s ease;
    height: 100%;
}
.state-flow-card:hover {
    border-color: rgba(var(--accent-rgb), 0.5);
    background: rgba(var(--surface-rgb), 0.9);
}
.state-flow-header {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
    font-family: 'Space Grotesk', sans-serif;
    color: var(--text);
    font-size: 0.95rem;
    margin-bottom: 8px;
}
.state-badge {
    padding: 3px 8px;
    border-radius: 14px;
    font-size: 0.70rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    display: inline-block;
}
.state-badge.armed { background: rgba(255, 170, 0, 0.2); color: #f59e0b; border: 1px solid #f59e0b; }
.state-badge.impulse { background: rgba(var(--accent-rgb), 0.2); color: var(--accent); border: 1px solid var(--accent); }
.state-badge.pullback { background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid #c084fc; }
.state-badge.intrade { background: rgba(var(--glow-rgb), 0.2); color: var(--glow); border: 1px solid var(--glow); }

/* ═══════════════════════════════════════════════════
   SECTION HEADER ACCENT
═══════════════════════════════════════════════════ */
.section-header::before {
    content: '';
    position: absolute;
    top: 0;
    left: 28px;
    width: 64px;
    height: 3px;
    border-radius: 0 0 4px 4px;
    background: linear-gradient(90deg, var(--accent), var(--glow));
}

/* ═══════════════════════════════════════════════════
   TABS
═══════════════════════════════════════════════════ */
div[data-testid="stTabs"] button[data-baseweb="tab"] {
    font-family: 'Space Grotesk', sans-serif !important;
    font-weight: 600 !important;
    font-size: 0.92rem !important;
    color: var(--muted) !important;
    padding: 10px 18px !important;
    transition: color 0.18s ease, background 0.18s ease !important;
    border-radius: 8px 8px 0 0 !important;
}

div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
    color: #e2e8f0 !important;
    background: rgba(var(--accent-rgb), 0.05) !important;
}

div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
    color: var(--accent) !important;
}

div[data-testid="stTabs"] div[data-baseweb="tab-highlight"] {
    background-color: var(--accent) !important;
    height: 2px !important;
}

div[data-testid="stTabs"] div[data-baseweb="tab-border"] {
    background: rgba(255, 255, 255, 0.07) !important;
}

/* ═══════════════════════════════════════════════════
   INPUTS / PLACEHOLDERS
═══════════════════════════════════════════════════ */
input::placeholder,
textarea::placeholder {
    color: var(--muted2) !important;
    opacity: 1 !important;
}

div[data-baseweb="select"] > div {
    background-color: rgba(255, 255, 255, 0.025) !important;
}

.stNumberInput button {
    color: var(--muted) !important;
}

.stNumberInput button:hover {
    color: var(--accent) !important;
}

/* ═══════════════════════════════════════════════════
   MULTISELECT — readable strategy chips
═══════════════════════════════════════════════════ */
div[data-testid="stMultiSelect"] div[data-baseweb="select"] > div {
    background-color: rgba(255, 255, 255, 0.04) !important;
    border: 1px solid rgba(var(--accent-rgb), 0.22) !important;
    border-radius: 10px !important;
    transition: border-color 0.18s ease, box-shadow 0.18s ease !important;
}

div[data-testid="stMultiSelect"] div[data-baseweb="select"] > div:hover {
    border-color: rgba(var(--accent-rgb), 0.42) !important;
}

div[data-testid="stMultiSelect"] div[data-baseweb="select"] > div:focus-within {
    border-color: rgba(var(--accent-rgb), 0.65) !important;
    box-shadow: 0 0 0 2px rgba(var(--accent-rgb), 0.14) !important;
}

/* Chips: dark glass fill + mint text (the default neon fill made the
   labels unreadable and clipped them to a few characters). */
div[data-testid="stMultiSelect"] [data-tag] {
    background: rgba(var(--accent-rgb), 0.13) !important;
    border: 1px solid rgba(var(--accent-rgb), 0.38) !important;
    border-radius: 8px !important;
    color: var(--text-soft) !important;
    height: auto !important;
    min-height: 1.65rem;
    max-width: 100%;
    padding: 2px 3px 2px 9px !important;
    margin: 3px 4px 3px 0 !important;
    box-shadow: none !important;
    transition: background 0.15s ease, border-color 0.15s ease !important;
}

div[data-testid="stMultiSelect"] [data-tag] span {
    color: var(--text-soft) !important;
    font-weight: 600 !important;
    font-size: 0.78rem !important;
    line-height: 1.35 !important;
    letter-spacing: 0.01em;
}

div[data-testid="stMultiSelect"] [data-tag]:hover {
    background: rgba(var(--accent-rgb), 0.22) !important;
    border-color: rgba(var(--accent-rgb), 0.62) !important;
}

div[data-testid="stMultiSelect"] [data-tag] button {
    color: var(--accent-3) !important;
    opacity: 0.85;
    border-radius: 6px !important;
}

div[data-testid="stMultiSelect"] [data-tag] button:hover {
    color: #ffffff !important;
    opacity: 1;
    background: rgba(var(--accent-rgb), 0.2) !important;
}

/* Dropdown menus follow the editable accent (native menus use the fixed
   .streamlit/config.toml primary colour otherwise). */
div[data-baseweb="popover"] ul[role="listbox"] li[role="option"]:hover,
div[data-baseweb="popover"] ul[role="listbox"] li[role="option"][aria-selected="true"],
div[data-baseweb="popover"] li[role="option"]:hover,
div[data-baseweb="popover"] li[role="option"][aria-selected="true"] {
    background-color: rgba(var(--accent-rgb), 0.16) !important;
}

div[data-baseweb="popover"] ul[role="listbox"] li[role="option"]:hover *,
div[data-baseweb="popover"] ul[role="listbox"] li[role="option"][aria-selected="true"] *,
div[data-baseweb="popover"] li[role="option"]:hover *,
div[data-baseweb="popover"] li[role="option"][aria-selected="true"] * {
    color: var(--text) !important;
}

div[data-testid="stMultiSelect"] [data-testid="stMultiSelectTagsContainer"] {
    padding-top: 4px !important;
    padding-bottom: 4px !important;
    row-gap: 2px;
    /* Show every tracked strategy at once instead of a 4-row scroller */
    max-height: none !important;
    overflow-y: visible !important;
}

div[data-testid="stMultiSelect"] input {
    color: var(--text) !important;
    font-size: 0.82rem !important;
}

/* Dropdown menu */
div[data-testid="stMultiSelectDropdown"] {
    background: var(--surface) !important;
    border: 1px solid rgba(var(--accent-rgb), 0.25) !important;
    border-radius: 10px !important;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.45) !important;
}

div[data-testid="stMultiSelectDropdown"] li,
div[data-testid="stMultiSelectDropdown"] [role="option"] {
    color: var(--text-soft) !important;
    font-size: 0.82rem !important;
}

/* ═══════════════════════════════════════════════════
   CHARTS
═══════════════════════════════════════════════════ */
div[data-testid="stVegaLiteChart"],
div[data-testid="stArrowVegaLiteChart"] {
    background: rgba(var(--surface-rgb), 0.55);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
    padding: 8px 10px;
}

/* ═══════════════════════════════════════════════════
   EXPANDER POLISH
═══════════════════════════════════════════════════ */
div[data-testid="stExpander"] {
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.14);
    transition: border-color 0.18s ease !important;
}

div[data-testid="stExpander"]:hover {
    border-color: rgba(var(--accent-rgb), 0.22) !important;
}

div[data-testid="stExpander"] summary:hover {
    color: var(--text) !important;
}

/* ═══════════════════════════════════════════════════
   DATAFRAME POLISH
═══════════════════════════════════════════════════ */
div[data-testid="stDataFrame"] {
    background: rgba(var(--surface-rgb), 0.55) !important;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.18) !important;
}

/* ═══════════════════════════════════════════════════
   SIGNAL / OUTCOME TABLES (gradient row tiers)
═══════════════════════════════════════════════════ */
.sig-table-wrap {
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 14px;
    overflow: hidden;
    background: rgba(var(--surface-2-rgb), 0.78);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.26);
    margin: 6px 0 10px;
}

.sig-table-scroll {
    max-height: 560px;
    overflow: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(16, 185, 129, 0.35) rgba(15, 23, 42, 0.6);
}

.sig-table-scroll::-webkit-scrollbar {
    width: 10px;
    height: 10px;
}

.sig-table-scroll::-webkit-scrollbar-track {
    background: rgba(15, 23, 42, 0.6);
}

.sig-table-scroll::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, rgba(var(--accent-rgb), 0.45), rgba(var(--accent-rgb), 0.22));
    border-radius: 999px;
    border: 2px solid rgba(var(--bg-rgb), 0.9);
}

table.sig-table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-family: 'Inter', sans-serif;
    font-size: 0.79rem;
    white-space: nowrap;
}

table.sig-table thead th {
    position: sticky;
    top: 0;
    z-index: 3;
    background: linear-gradient(180deg, var(--surface) 0%, var(--surface-2) 100%);
    color: #94a3b8;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid rgba(var(--accent-rgb), 0.30);
    white-space: nowrap;
}

table.sig-table thead th.num { text-align: right; }

table.sig-table tbody td {
    padding: 8px 12px;
    color: #dbe4f0;
    border-bottom: 1px solid rgba(148, 163, 184, 0.07);
    vertical-align: middle;
}

table.sig-table tbody td.num {
    text-align: right;
    font-family: 'JetBrains Mono', monospace;
    font-variant-numeric: tabular-nums;
    font-size: 0.78rem;
}

table.sig-table tbody td.wrap {
    white-space: normal;
    min-width: 280px;
    line-height: 1.45;
    color: #cbd5e1;
}

table.sig-table tbody td.sym {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    color: #f8fafc;
    letter-spacing: 0.02em;
}

table.sig-table tbody tr {
    background-color: rgba(var(--surface-rgb), 0.55);
    transition: filter 0.15s ease;
}

table.sig-table tbody tr:nth-child(even) {
    background-color: rgba(var(--surface-rgb), 0.78);
}

table.sig-table tbody tr:hover { filter: brightness(1.22); }

table.sig-table tbody tr td:first-child {
    border-left: 3px solid transparent;
    padding-left: 12px;
}

/* ── Tier gradients: strongest → weakest ───────────────── */
tr.tier-tp2 { background-image: linear-gradient(90deg, rgba(16,185,129,0.34) 0%, rgba(16,185,129,0.12) 45%, rgba(16,185,129,0.02) 100%); }
tr.tier-tp2 td:first-child { border-left-color: #10b981; }

tr.tier-tp1 { background-image: linear-gradient(90deg, rgba(16,185,129,0.24) 0%, rgba(16,185,129,0.09) 45%, rgba(16,185,129,0.01) 100%); }
tr.tier-tp1 td:first-child { border-left-color: #34d399; }

tr.tier-trail { background-image: linear-gradient(90deg, rgba(16,185,129,0.17) 0%, rgba(16,185,129,0.06) 50%, rgba(16,185,129,0.01) 100%); }
tr.tier-trail td:first-child { border-left-color: #6ee7b7; }

tr.tier-profit { background-image: linear-gradient(90deg, rgba(16,185,129,0.11) 0%, rgba(16,185,129,0.03) 55%, rgba(16,185,129,0.0) 100%); }
tr.tier-profit td:first-child { border-left-color: #a7f3d0; }

tr.tier-flat {
    background-image: linear-gradient(90deg, rgba(148,163,184,0.14) 0%, rgba(148,163,184,0.05) 55%, rgba(148,163,184,0.0) 100%);
}
tr.tier-flat td:first-child { border-left-color: #94a3b8; }

tr.tier-loss { background-image: linear-gradient(90deg, rgba(244,63,94,0.13) 0%, rgba(244,63,94,0.04) 55%, rgba(244,63,94,0.0) 100%); }
tr.tier-loss td:first-child { border-left-color: #fb7185; }

tr.tier-sl { background-image: linear-gradient(90deg, rgba(244,63,94,0.26) 0%, rgba(244,63,94,0.09) 45%, rgba(244,63,94,0.015) 100%); }
tr.tier-sl td:first-child { border-left-color: #f43f5e; }

tr.tier-open { background-image: linear-gradient(90deg, rgba(245,158,11,0.13) 0%, rgba(245,158,11,0.045) 55%, rgba(245,158,11,0.0) 100%); }
tr.tier-open td:first-child { border-left-color: #fbbf24; }

tr.tier-new { background-image: linear-gradient(90deg, rgba(16,185,129,0.22) 0%, rgba(16,185,129,0.07) 50%, rgba(16,185,129,0.0) 100%); }
tr.tier-new td:first-child { border-left-color: #10b981; }

tr.tier-armed { background-image: linear-gradient(90deg, rgba(168,85,247,0.16) 0%, rgba(168,85,247,0.05) 55%, rgba(168,85,247,0.0) 100%); }
tr.tier-armed td:first-child { border-left-color: #c084fc; }

tr.tier-missed { background-image: linear-gradient(90deg, rgba(148,163,184,0.10) 0%, rgba(148,163,184,0.03) 60%, transparent 100%); }
tr.tier-missed td:first-child { border-left-color: #64748b; }

tr.tier-none { background-color: rgba(var(--surface-rgb), 0.55); }

/* ── Inline badges inside signal tables ────────────────── */
.sig-chip {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.67rem;
    font-weight: 700;
    font-family: 'Space Grotesk', sans-serif;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    white-space: nowrap;
}

.sig-chip.buy   { background: rgba(16,185,129,0.18); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.42); }
.sig-chip.sell  { background: rgba(244,63,94,0.18); color: #fda4af; border: 1px solid rgba(244,63,94,0.42); }
.sig-chip.neutral { background: rgba(148,163,184,0.14); color: #cbd5e1; border: 1px solid rgba(148,163,184,0.30); }

.sig-chip.tp2   { background: rgba(16,185,129,0.26); color: #d1fae5; border: 1px solid rgba(16,185,129,0.62); }
.sig-chip.tp1   { background: rgba(16,185,129,0.18); color: #a7f3d0; border: 1px solid rgba(16,185,129,0.45); }
.sig-chip.trail { background: rgba(16,185,129,0.13); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.35); }
.sig-chip.profit{ background: rgba(16,185,129,0.10); color: #a7f3d0; border: 1px solid rgba(16,185,129,0.26); }
.sig-chip.flat  { background: rgba(148,163,184,0.14); color: #cbd5e1; border: 1px solid rgba(148,163,184,0.30); }
.sig-chip.loss  { background: rgba(244,63,94,0.12); color: #fda4af; border: 1px solid rgba(244,63,94,0.30); }
.sig-chip.sl    { background: rgba(244,63,94,0.24); color: #ffe4e6; border: 1px solid rgba(244,63,94,0.58); }
.sig-chip.open  { background: rgba(245,158,11,0.14); color: #fcd34d; border: 1px solid rgba(245,158,11,0.38); }
.sig-chip.new   { background: rgba(16,185,129,0.20); color: #a7f3d0; border: 1px solid rgba(16,185,129,0.50); }
.sig-chip.armed { background: rgba(168,85,247,0.16); color: #d8b4fe; border: 1px solid rgba(168,85,247,0.42); }
.sig-chip.missed{ background: rgba(148,163,184,0.12); color: #94a3b8; border: 1px solid rgba(148,163,184,0.26); }

/* ── Outcome tier legend ───────────────────────────────── */
.sig-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    align-items: center;
    margin: 4px 0 10px;
}

.sig-legend .lg-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 999px;
    background: rgba(15, 23, 42, 0.85);
    border: 1px solid rgba(148, 163, 184, 0.18);
    font-size: 0.68rem;
    font-weight: 600;
    color: #cbd5e1;
    white-space: nowrap;
}

.sig-legend .lg-swatch {
    width: 11px;
    height: 11px;
    border-radius: 3px;
    display: inline-block;
}

/* Tier-tinted Styler fallbacks (solid fills for interactive grids) */
.tint-tp2   { background-color: rgba(16, 185, 129, 0.34) !important; }
.tint-tp1   { background-color: rgba(16, 185, 129, 0.22) !important; }
.tint-trail { background-color: rgba(16, 185, 129, 0.16) !important; }
.tint-profit{ background-color: rgba(16, 185, 129, 0.10) !important; }
.tint-flat  { background-color: rgba(148, 163, 184, 0.12) !important; }
.tint-loss  { background-color: rgba(244, 63, 94, 0.12) !important; }
.tint-sl    { background-color: rgba(244, 63, 94, 0.24) !important; }
.tint-open  { background-color: rgba(245, 158, 11, 0.12) !important; }

/* ═══════════════════════════════════════════════════
   LINKS / STATUS PILLS
═══════════════════════════════════════════════════ */
a {
    color: var(--glow) !important;
    text-decoration: none !important;
}

a:hover {
    color: #7dd3fc !important;
    text-decoration: underline !important;
}

.market-status-open,
.market-status-closed {
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18);
}

/* ═══════════════════════════════════════════════════
   SIDEBAR V2 — ORGANISED NAV + THEME PANEL
═══════════════════════════════════════════════════ */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
section[data-testid="stSidebar"] .block-container {
    padding: 0.55rem 0.8rem 1.4rem !important;
}

section[data-testid="stSidebar"] .stMarkdown p {
    margin-bottom: 0.35rem;
}

section[data-testid="stSidebar"] hr {
    margin: 0.85rem 0 !important;
}

/* Brand card */
.sidebar-brand {
    display: flex;
    align-items: center;
    gap: 9px;
    text-align: left;
    padding: 10px 11px 11px;
    margin: 0 0 10px;
    border: 1px solid rgba(var(--accent-rgb), 0.20);
    border-radius: 14px;
    background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.12), rgba(var(--surface-rgb), 0.35));
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.22);
    position: relative;
    overflow: hidden;
}

.sidebar-brand::after {
    content: "";
    position: absolute;
    inset: 0;
    background: radial-gradient(120px 60px at 100% 0%, rgba(var(--accent-rgb), 0.24), transparent 70%);
    pointer-events: none;
}

.sidebar-brand .brand-logo {
    width: 42px;
    height: 42px;
    flex: 0 0 42px;
    position: relative;
    display: grid;
    place-items: center;
    font-size: 1.35rem;
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.24), rgba(var(--glow-rgb), 0.14));
    border: 1px solid rgba(var(--accent-rgb), 0.38);
    box-shadow: inset 0 0 18px rgba(var(--accent-rgb), 0.16);
}

.sidebar-brand .brand-status {
    position: absolute;
    right: -3px;
    bottom: -3px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--accent);
    border: 2px solid var(--surface-2);
    box-shadow: 0 0 8px rgba(var(--accent-rgb), 0.85);
}

.sidebar-brand .brand-status.off {
    background: #f59e0b;
    box-shadow: 0 0 8px rgba(245, 158, 11, 0.8);
}

.sidebar-brand .brand-copy {
    min-width: 0;
    flex: 1 1 auto;
    text-align: left;
}

.sidebar-brand .brand-title {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.86rem;
    font-weight: 700;
    line-height: 1.15;
    letter-spacing: -0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    background: linear-gradient(135deg, var(--accent) 0%, var(--glow) 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
}

.sidebar-brand .brand-sub {
    font-size: 0.6rem;
    color: var(--muted);
    margin-top: 3px;
    text-transform: uppercase;
    letter-spacing: 0.11em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.sidebar-brand .brand-live {
    position: relative;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 6px;
    border-radius: 999px;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.52rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    background: rgba(var(--accent-rgb), 0.12);
    border: 1px solid rgba(var(--accent-rgb), 0.30);
    color: var(--accent-2);
    white-space: nowrap;
}

.sidebar-brand .brand-live.off {
    background: rgba(245, 158, 11, 0.10);
    border-color: rgba(245, 158, 11, 0.28);
    color: #fcd34d;
}

/* Section label with fading rule */
.side-label {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 12px 4px 6px;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--muted2);
}

.side-label::after {
    content: "";
    flex: 1;
    height: 1px;
    background: linear-gradient(90deg, rgba(var(--accent-rgb), 0.30), transparent);
}

/* Nav radio rendered as cards */
section[data-testid="stSidebar"] div[data-testid="stRadio"] > div[role="radiogroup"] {
    gap: 4px !important;
}

section[data-testid="stSidebar"] div[data-testid="stRadio"] label {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 38px;
    padding: 8px 12px !important;
    border-radius: 11px !important;
    border: 1px solid transparent !important;
    transition: background 0.16s ease, border-color 0.16s ease,
                transform 0.16s ease, box-shadow 0.16s ease !important;
}

/* Collapse the native radio dot; the whole row is the control */
section[data-testid="stSidebar"] div[data-testid="stRadio"] label > div:first-child {
    width: 0 !important;
    min-width: 0 !important;
    margin: 0 !important;
    opacity: 0 !important;
}

section[data-testid="stSidebar"] div[data-testid="stRadio"] label p {
    font-size: 0.86rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.01em !important;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

section[data-testid="stSidebar"] div[data-testid="stRadio"] label:hover {
    transform: translateX(2px);
}

/* Appearance popover trigger */
section[data-testid="stSidebar"] div[data-testid="stPopover"] button {
    justify-content: flex-start !important;
    gap: 8px;
    font-size: 0.82rem !important;
    background: rgba(var(--surface-rgb), 0.55) !important;
    border: 1px dashed rgba(var(--accent-rgb), 0.32) !important;
    color: var(--text-soft) !important;
}

section[data-testid="stSidebar"] div[data-testid="stPopover"] button:hover {
    border-style: solid !important;
    border-color: rgba(var(--accent-rgb), 0.55) !important;
    color: var(--text) !important;
}

/* Theme swatch preview row */
.theme-swatch-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 2px 0 8px;
}

.theme-swatch {
    width: 26px;
    height: 26px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow: inset 0 0 10px rgba(255, 255, 255, 0.08);
}

.theme-swatch.active {
    border-color: transparent;
    box-shadow: 0 0 0 2px var(--accent), inset 0 0 10px rgba(255, 255, 255, 0.10);
}

/* Tighter status + telemetry chips */
.token-badge {
    padding: 5px 10px;
    font-size: 0.72rem;
    min-height: 30px;
}

.telemetry-row {
    padding: 7px 10px;
    margin: 3px 0;
}

/* Sidebar footer */
.side-footer {
    margin-top: 14px;
    padding: 9px 6px 2px;
    border-top: 1px solid rgba(var(--muted-rgb), 0.12);
    text-align: center;
}

.side-footer div {
    font-size: 0.66rem;
    color: var(--muted2);
    line-height: 1.65;
}

.side-footer .sf-theme {
    color: var(--accent-2);
    font-weight: 600;
    letter-spacing: 0.04em;
}

/* ═══════════════════════════════════════════════════
   SIDEBAR V3 — CLEAN NAV, ENGINE CARD, THEME STUDIO
═══════════════════════════════════════════════════ */

/* The highlighted row is the nav control — drop the native radio dot */
section[data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] label > div > div:first-child {
    display: none !important;
}

section[data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] label > div {
    gap: 0 !important;
    padding-left: 0 !important;
}

/* The real section eyebrow above the nav is enough */
section[data-testid="stSidebar"] div[data-testid="stRadio"] > label[data-testid="stWidgetLabel"] {
    display: none !important;
}

/* Compact engine & timeframe card */
section[data-testid="stSidebar"] div[data-testid="stExpander"] {
    background: linear-gradient(180deg, rgba(var(--surface-rgb), 0.62), rgba(var(--surface-2-rgb), 0.38)) !important;
    border: 1px solid rgba(var(--muted-rgb), 0.14) !important;
    border-radius: 14px !important;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.18) !important;
}

section[data-testid="stSidebar"] div[data-testid="stExpander"] summary {
    min-height: 42px !important;
    padding: 0 12px !important;
    align-items: center !important;
}

section[data-testid="stSidebar"] div[data-testid="stExpander"] summary:hover {
    background: rgba(var(--accent-rgb), 0.05) !important;
}

section[data-testid="stSidebar"] div[data-testid="stExpander"] summary p {
    font-size: 0.76rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.03em !important;
    color: var(--text-soft) !important;
}

section[data-testid="stSidebar"] div[data-testid="stExpanderDetails"] {
    padding: 2px 12px 12px !important;
}

.st-key-engine_section div[data-testid="stVerticalBlock"] {
    gap: 0.5rem !important;
}

section[data-testid="stSidebar"] .et-eyebrow {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 6px 2px 0;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--muted2);
}

section[data-testid="stSidebar"] .et-eyebrow::before {
    content: "";
    width: 4px;
    height: 4px;
    flex: 0 0 4px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 8px rgba(var(--accent-rgb), 0.9);
}

.et-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin: 0 0 6px;
}

.et-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--text-soft);
    background: rgba(var(--surface-rgb), 0.65);
    border: 1px solid rgba(var(--muted-rgb), 0.18);
}

.et-chip.hot {
    color: var(--accent-2);
    border-color: rgba(var(--accent-rgb), 0.35);
    background: rgba(var(--accent-rgb), 0.10);
}

.et-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin: 4px 0 0;
    padding-top: 8px;
    border-top: 1px dashed rgba(var(--muted-rgb), 0.16);
    font-size: 0.63rem;
    color: var(--muted2);
}

.et-foot b {
    color: var(--text-soft);
    font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
}

/* Timeframe pills inside the engine card */
section[data-testid="stSidebar"] div[data-testid="stButtonGroup"] {
    gap: 4px !important;
}

section[data-testid="stSidebar"] div[data-testid="stButtonGroup"] button {
    min-height: 28px !important;
    padding: 2px 8px !important;
    border-radius: 9px !important;
    border: 1px solid rgba(var(--muted-rgb), 0.18) !important;
    background: rgba(var(--surface-rgb), 0.55) !important;
    color: var(--text-soft) !important;
    box-shadow: none !important;
}

section[data-testid="stSidebar"] div[data-testid="stButtonGroup"] button p {
    font-size: 0.7rem !important;
    font-weight: 600 !important;
    color: inherit !important;
}

section[data-testid="stSidebar"] div[data-testid="stButtonGroup"] button:hover {
    border-color: rgba(var(--accent-rgb), 0.45) !important;
    background: rgba(var(--accent-rgb), 0.08) !important;
}

section[data-testid="stSidebar"] div[data-testid="stButtonGroup"] button[data-selected="true"],
section[data-testid="stSidebar"] div[data-testid="stButtonGroup"] button[aria-checked="true"] {
    background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.30), rgba(var(--accent-rgb), 0.12)) !important;
    border-color: rgba(var(--accent-rgb), 0.62) !important;
    color: var(--text) !important;
    box-shadow: 0 3px 12px rgba(var(--accent-rgb), 0.18) !important;
}

/* ═══════════════════════════════════════════════════
   THEME STUDIO
═══════════════════════════════════════════════════ */
.ts-hero {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 18px;
    padding: 16px 20px;
    margin: 0 0 16px;
    border-radius: 16px;
    border: 1px solid rgba(var(--accent-rgb), 0.22);
    background:
        radial-gradient(320px 130px at 100% 0%, rgba(var(--accent-rgb), 0.18), transparent 70%),
        linear-gradient(120deg, rgba(var(--surface-rgb), 0.75), rgba(var(--surface-2-rgb), 0.45));
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.22);
}

.ts-hero-title {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 1.06rem;
    font-weight: 700;
    color: var(--text);
}

.ts-hero-sub {
    font-size: 0.74rem;
    color: var(--muted);
    margin-top: 3px;
}

.ts-swatches {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
}

.ts-swatch {
    width: 30px;
    height: 30px;
    border-radius: 9px;
    border: 1px solid rgba(255, 255, 255, 0.16);
    box-shadow: inset 0 0 12px rgba(255, 255, 255, 0.08);
}

.ts-swatch.active {
    box-shadow: 0 0 0 2px var(--accent), inset 0 0 12px rgba(255, 255, 255, 0.10);
}

.ts-heading {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--muted2);
    margin: 0 0 10px;
}

/* Bordered containers are only used by the Theme Studio */
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 16px !important;
    border-color: rgba(var(--muted-rgb), 0.14) !important;
    background: linear-gradient(165deg, rgba(var(--surface-rgb), 0.55), rgba(var(--surface-2-rgb), 0.32)) !important;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.18) !important;
}

.st-key-theme_palette div[data-testid="stVerticalBlock"],
.st-key-theme_behaviour div[data-testid="stVerticalBlock"],
.st-key-theme_saved div[data-testid="stVerticalBlock"] {
    gap: 0.45rem !important;
}

.ts-preview {
    border: 1px solid rgba(var(--muted-rgb), 0.16);
    border-radius: 16px;
    padding: 14px;
    background: linear-gradient(160deg, rgba(var(--surface-rgb), 0.85), rgba(var(--bg), 0.95));
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04), 0 14px 34px rgba(0, 0, 0, 0.25);
}

.ts-preview-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 11px;
    border: 1px solid rgba(var(--accent-rgb), 0.20);
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.12), rgba(var(--surface-rgb), 0.35));
}

.ts-preview-title {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 0.86rem;
    color: var(--text);
}

.ts-preview-sub {
    font-size: 0.58rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-top: 2px;
}

.ts-nav {
    display: flex;
    flex-direction: column;
    gap: 3px;
    margin: 12px 0;
}

.ts-nav span {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 6px 10px;
    border-radius: 9px;
    font-size: 0.73rem;
    color: var(--text-soft);
    border: 1px solid transparent;
}

.ts-nav span.on {
    background: linear-gradient(90deg, rgba(var(--accent-rgb), 0.20), rgba(var(--accent-rgb), 0.03));
    border-color: rgba(var(--accent-rgb), 0.45);
    color: var(--text);
    box-shadow: inset 3px 0 0 var(--accent);
}

.ts-btns {
    display: flex;
    gap: 7px;
    margin: 10px 0 12px;
}

.ts-btn {
    flex: 1;
    text-align: center;
    padding: 7px 10px;
    border-radius: 9px;
    font-size: 0.7rem;
    font-weight: 600;
    border: 1px solid rgba(var(--muted-rgb), 0.22);
    color: var(--text-soft);
    background: rgba(var(--surface-rgb), 0.6);
}

.ts-btn.primary {
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: var(--on-accent);
    border-color: rgba(var(--accent-rgb), 0.6);
    box-shadow: 0 6px 18px rgba(var(--accent-rgb), 0.25);
}

.ts-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 7px;
    margin: 10px 0;
}

.ts-metric {
    border: 1px solid rgba(var(--muted-rgb), 0.16);
    border-radius: 10px;
    padding: 8px 10px;
    background: rgba(var(--surface-rgb), 0.55);
}

.ts-metric span {
    display: block;
    font-size: 0.55rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted2);
}

.ts-metric strong {
    display: block;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 1.02rem;
    color: var(--text);
    margin-top: 3px;
}

.ts-metric em {
    display: block;
    font-style: normal;
    font-size: 0.58rem;
    color: var(--accent-2);
    margin-top: 2px;
}

.ts-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
}

.ts-saved {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}

.ts-saved-name {
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-soft);
}

.ts-saved-dots {
    display: flex;
    gap: 4px;
}

.ts-saved-dots i {
    width: 12px;
    height: 12px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.15);
    display: block;
}

.ts-empty {
    padding: 8px 2px;
    font-size: 0.76rem;
    color: var(--muted2);
}

@media (max-width: 900px) {
    .ts-metrics {
        grid-template-columns: 1fr;
    }
}

/* Responsive sidebar */
@media (max-width: 1200px) {
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
    section[data-testid="stSidebar"] .block-container {
        padding-left: 0.65rem !important;
        padding-right: 0.65rem !important;
    }

    .sidebar-brand .brand-sub {
        letter-spacing: 0.06em;
    }
}

@media (max-width: 900px) {
    .sidebar-brand {
        padding: 9px 10px 10px;
    }

    section[data-testid="stSidebar"] div[data-testid="stRadio"] label p {
        font-size: 0.82rem !important;
    }
}

/* ═══════════════════════════════════════════════════
   RESPONSIVE
═══════════════════════════════════════════════════ */
@media (max-width: 900px) {
    .main .block-container {
        padding-left: 1rem;
        padding-right: 1rem;
    }

    .metric-card .metric-value {
        font-size: 1.4rem;
    }

    .section-header {
        padding: 16px 18px;
    }
}

</style>
""" + _theme_css(), unsafe_allow_html=True)


# ── Alert history persistence ────────────────────────────────────────────
ALERT_HISTORY_FILE = STATE_DIR / "alert_history.json"
ALERT_HISTORY_LIMIT = 500


def load_alert_history() -> list[dict]:
    """Load persisted alert history (newest last)."""
    if ALERT_HISTORY_FILE.exists():
        try:
            data = json.loads(ALERT_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [
                    item
                    for item in data[-ALERT_HISTORY_LIMIT:]
                    if isinstance(item, dict)
                ]
        except Exception:
            pass
    return []


def save_alert_history(history: list[dict]) -> None:
    """Persist alert history, capped to the most recent entries."""
    try:
        ALERT_HISTORY_FILE.write_text(
            json.dumps(
                history[-ALERT_HISTORY_LIMIT:],
                indent=2,
                default=str,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Persistent state initialisation ─────────────────────────────────────
if "alert_history" not in st.session_state:
    st.session_state.alert_history = load_alert_history()
if "resolver" not in st.session_state:
    st.session_state.resolver = InstrumentResolver()
    st.session_state.resolver.load()
if "scan_results" not in st.session_state:
    st.session_state.scan_results = []
if "last_scan_time" not in st.session_state:
    st.session_state.last_scan_time = None
if "selected_alert_symbol" not in st.session_state:
    st.session_state.selected_alert_symbol = None


# ── Helper: watchlist I/O ────────────────────────────────────────────────
def load_watchlist() -> list[str]:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return ["RELIANCE", "INFY", "TCS"]


def save_watchlist(symbols: list[str]) -> None:
    WATCHLIST_FILE.write_text(json.dumps(symbols, indent=2), encoding="utf-8")


# ── Helper: custom metric card ────────────────────────────────────────────
def metric_card(label: str, value: str, delta: str = None, color: str = "green", icon: str = ""):
    """Render a refined financial glassmorphism metric card."""
    delta_html = ""
    if delta:
        s_delta = str(delta)
        is_neg = s_delta.startswith("-")
        delta_class = "negative" if is_neg else ""
        delta_arrow = "▼" if is_neg else "▲"
        delta_html = f'<div class="metric-delta {delta_class}">{delta_arrow} {s_delta}</div>'

    icon_html = f'<span class="metric-icon">{icon}</span>' if icon else ""

    html = (
        f'<div class="metric-card {color}">'
        f'<div class="metric-top">'
        f'<div class="metric-label">{label}</div>'
        f'{icon_html}'
        f'</div>'
        f'<div class="metric-value">{value}</div>'
        f'{delta_html}'
        f'</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


# ── Helper: section header ────────────────────────────────────────────────
def section_header(title: str, subtitle: str = ""):
    sub_html = f'<div class="sub">{subtitle}</div>' if subtitle else ""
    html = f'<div class="section-header"><h1>{title}</h1>{sub_html}</div>'
    st.markdown(html, unsafe_allow_html=True)


# ── Navigation helper & local cache / Altair chart utilities ──────────────
NAV_ITEMS = [
    "🏠 Command Center",
    "💼 Today's P&L",
    "🔔 Alerts",
    "📊 Backtest",
    "📒 Trade Journal",
    "🧠 Strategies",
    "📡 API Calls & Data",
    "🎨 Theme Studio",
]

# Section shown when the app is opened, before the user picks a workspace.
DEFAULT_NAV_ITEM = "🔔 Alerts"


def _navigate_to(target_nav: str) -> None:
    """Queue a sidebar navigation switch for the next rerun."""
    if target_nav in NAV_ITEMS:
        st.session_state["_pending_nav"] = target_nav


def _tf_display(interval, unit) -> str:
    """Compact human label for a candle size, e.g. (5, 'minutes') -> '5m'."""
    suffixes = {
        "minutes": "m",
        "hours": "h",
        "days": "D",
        "weeks": "W",
        "months": "M",
    }
    value = int(interval)
    suffix = suffixes.get(str(unit), str(unit))
    return f"{value}{suffix}"


def _load_local_symbol_df(symbol: str, prefer_intraday: bool = True) -> tuple[pd.DataFrame, str]:
    """
    Read locally cached OHLCV CSV for `symbol` from DATA_DIR without making
    any live network calls. Returns (dataframe, timeframe_label).
    """
    if not DATA_DIR.exists():
        return pd.DataFrame(), ""
    sym = symbol.strip().upper()
    candidates = (
        [(DATA_DIR / f"{sym}_minutes5.csv", "5m"), (DATA_DIR / f"{sym}_days1.csv", "1D")]
        if prefer_intraday
        else [(DATA_DIR / f"{sym}_days1.csv", "1D"), (DATA_DIR / f"{sym}_minutes5.csv", "5m")]
    )
    for path, tf_label in candidates:
        if path.exists():
            try:
                df = pd.read_csv(path)
                if not df.empty and "close" in df.columns:
                    if "timestamp" in df.columns:
                        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dt.tz_convert(IST)
                        df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                    return df, tf_label
            except Exception:
                continue
    for extra in sorted(DATA_DIR.glob(f"{sym}_*.csv")):
        try:
            df = pd.read_csv(extra)
            if not df.empty and "close" in df.columns:
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dt.tz_convert(IST)
                    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                tf_tag = extra.stem.replace(f"{sym}_", "")
                return df, tf_tag
        except Exception:
            continue
    return pd.DataFrame(), ""


def _svg_sparkline(values: list[float], width: int = 112, height: int = 34) -> str:
    """Build a crisp inline SVG area + line sparkline from recent prices."""
    pts = [float(v) for v in values if pd.notna(v)]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) if hi > lo else 1.0
    pad = 3.0
    inner_h = height - 2 * pad
    step_x = width / max(len(pts) - 1, 1)
    coords = []
    for idx, val in enumerate(pts):
        x = round(idx * step_x, 1)
        y = round(pad + inner_h * (1.0 - (val - lo) / span), 1)
        coords.append((x, y))
    is_up = pts[-1] >= pts[0]
    stroke = "#10b981" if is_up else "#f43f5e"
    fill_rgba = "rgba(16,185,129,0.18)" if is_up else "rgba(244,63,94,0.18)"
    poly = " ".join(f"{x},{y}" for x, y in coords)
    area = f"0,{height} {poly} {coords[-1][0]},{height}"
    last_x, last_y = coords[-1]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="overflow:visible;display:block;">'
        f'<polygon points="{area}" fill="{fill_rgba}" />'
        f'<polyline points="{poly}" fill="none" stroke="{stroke}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round" />'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="{stroke}" />'
        f'</svg>'
    )


def _symbol_cache_snapshot(symbol: str) -> dict:
    """Compute last price, session % move, range position, and sparkline from disk cache."""
    df, tf_label = _load_local_symbol_df(symbol, prefer_intraday=True)
    if df.empty:
        return {"symbol": symbol, "cached": False}

    if "timestamp" in df.columns and tf_label == "5m":
        last_day = df["timestamp"].dt.date.max()
        day_df = df[df["timestamp"].dt.date == last_day]
        if len(day_df) >= 3:
            ref_open = float(day_df["open"].iloc[0])
            last_close = float(day_df["close"].iloc[-1])
            hi = float(day_df["high"].max())
            lo = float(day_df["low"].min())
            spark_vals = day_df["close"].tail(36).tolist()
            date_str = str(last_day)
        else:
            tail = df.tail(36)
            ref_open = float(tail["open"].iloc[0])
            last_close = float(tail["close"].iloc[-1])
            hi = float(tail["high"].max())
            lo = float(tail["low"].min())
            spark_vals = tail["close"].tolist()
            date_str = str(df["timestamp"].iloc[-1])[:10]
    else:
        tail = df.tail(30)
        ref_open = float(tail["close"].iloc[-2]) if len(tail) >= 2 else float(tail["open"].iloc[0])
        last_close = float(tail["close"].iloc[-1])
        hi = float(tail["high"].tail(5).max())
        lo = float(tail["low"].tail(5).min())
        spark_vals = tail["close"].tolist()
        date_str = str(df["timestamp"].iloc[-1])[:10] if "timestamp" in df.columns else ""

    chg_pct = ((last_close - ref_open) / ref_open * 100.0) if ref_open else 0.0
    pos_pct = ((last_close - lo) / (hi - lo) * 100.0) if hi > lo else 50.0
    return {
        "symbol": symbol,
        "cached": True,
        "tf": tf_label,
        "close": last_close,
        "chg_pct": chg_pct,
        "high": hi,
        "low": lo,
        "pos_pct": min(max(pos_pct, 4.0), 96.0),
        "spark_svg": _svg_sparkline(spark_vals),
        "date": date_str,
        "rows": len(df),
    }


def _render_watchlist_snapshot_grid(symbols: list[str], max_cards: int = 8) -> int:
    """Render responsive Watchlist Market Pulse cards using local cached candles."""
    if not symbols:
        return 0
    snaps = [_symbol_cache_snapshot(s) for s in symbols[:max_cards]]
    cached_count = sum(1 for s in snaps if s.get("cached"))
    cols_per_row = min(4, max(1, len(snaps)))
    for row_start in range(0, len(snaps), cols_per_row):
        row_items = snaps[row_start : row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, snap in zip(cols, row_items):
            with col:
                if snap.get("cached"):
                    chg = snap["chg_pct"]
                    chg_cls = "up" if chg > 0.02 else ("down" if chg < -0.02 else "flat")
                    sign = "+" if chg > 0 else ""
                    card_html = (
                        f'<div class="wl-snap-card">'
                        f'<div class="wl-snap-top">'
                        f'<span class="wl-snap-sym">{snap["symbol"]}</span>'
                        f'<span class="wl-snap-chg {chg_cls}">{sign}{chg:.2f}%</span>'
                        f'</div>'
                        f'<div class="wl-snap-mid">'
                        f'<div>'
                        f'<div class="wl-snap-price">₹{snap["close"]:,.2f}</div>'
                        f'<div class="wl-snap-sub">{snap["tf"]} cache · {snap["date"]}</div>'
                        f'</div>'
                        f'<div>{snap["spark_svg"]}</div>'
                        f'</div>'
                        f'<div class="wl-range-track">'
                        f'<div class="wl-range-fill" style="width:{snap["pos_pct"]:.0f}%;"></div>'
                        f'</div>'
                        f'<div class="wl-range-labels">'
                        f'<span>L ₹{snap["low"]:,.1f}</span>'
                        f'<span>H ₹{snap["high"]:,.1f}</span>'
                        f'</div>'
                        f'</div>'
                    )
                else:
                    card_html = (
                        f'<div class="wl-snap-card">'
                        f'<div class="wl-snap-top">'
                        f'<span class="wl-snap-sym">{snap["symbol"]}</span>'
                        f'<span class="wl-snap-chg flat">No cache</span>'
                        f'</div>'
                        f'<div class="wl-snap-mid">'
                        f'<div>'
                        f'<div class="wl-snap-price" style="color:#64748b;">—</div>'
                        f'<div class="wl-snap-sub">Run scan or backtest to cache</div>'
                        f'</div>'
                        f'</div>'
                        f'<div class="wl-range-track"><div class="wl-range-fill" style="width:0%;"></div></div>'
                        f'<div class="wl-range-labels"><span>Awaiting candles</span><span>NSE EQ</span></div>'
                        f'</div>'
                    )
                st.markdown(card_html, unsafe_allow_html=True)
    return cached_count


def _render_cached_chart_preview(symbol: str, key_prefix: str = "cc_") -> None:
    """Render an interactive Lightweight Charts candlestick view strictly from local disk cache."""
    df_raw, tf_label = _load_local_symbol_df(symbol, prefer_intraday=True)
    if df_raw.empty or "timestamp" not in df_raw.columns:
        st.info(f"No local cached candles found for **{symbol}** yet. Run a scan or backtest to populate local cache.")
        return

    is_intraday = tf_label == "5m"
    if is_intraday:
        unique_days = sorted(df_raw["timestamp"].dt.date.unique())
        keep_days = unique_days[-2:] if len(unique_days) >= 2 else unique_days
        cdf = df_raw[df_raw["timestamp"].dt.date.isin(keep_days)].copy().reset_index(drop=True)
        cdf["chart_time"] = [
            int(t.timestamp() + (t.utcoffset().total_seconds() if t.utcoffset() else 0))
            for t in cdf["timestamp"]
        ]
    else:
        cdf = df_raw.tail(90).copy().reset_index(drop=True)
        cdf["chart_time"] = cdf["timestamp"].dt.strftime("%Y-%m-%d")

    cdf = cdf.drop_duplicates(subset=["chart_time"]).reset_index(drop=True)
    if cdf.empty:
        return

    candles = [
        {
            "time": t,
            "open": round(float(o), 2),
            "high": round(float(h), 2),
            "low": round(float(l), 2),
            "close": round(float(c), 2),
        }
        for t, o, h, l, c in zip(cdf["chart_time"], cdf["open"], cdf["high"], cdf["low"], cdf["close"])
    ]
    volume_data = []
    if "volume" in cdf.columns and float(cdf["volume"].fillna(0).sum()) > 0:
        volume_data = [
            {
                "time": t,
                "value": float(v),
                "color": "rgba(16,185,129,0.34)" if c >= o else "rgba(244,63,94,0.34)",
            }
            for t, v, o, c in zip(cdf["chart_time"], cdf["volume"].fillna(0), cdf["open"], cdf["close"])
        ]

    # Overlay latest journal levels for this symbol if any exist
    levels = []
    j_entries = [e for e in load_journal() if e.get("symbol") == symbol]
    if j_entries:
        latest_e = sorted(j_entries, key=lambda x: str(x.get("entry_time") or ""))[-1]
        for px, col_hex, title in (
            (latest_e.get("entry_price"), "#38bdf8", "Last Entry"),
            (latest_e.get("stop_loss"), "#f43f5e", "SL"),
            (latest_e.get("tp1"), "#10b981", "TP1"),
        ):
            if px:
                levels.append({"price": float(px), "color": col_hex, "title": title})

    chart_options = {
        "height": 440,
        "layout": {
            "background": {"type": "solid", "color": "#080d1a"},
            "textColor": "#94a3b8",
            "fontSize": 12,
            "fontFamily": "Inter, 'Space Grotesk', sans-serif",
        },
        "grid": {
            "vertLines": {"color": "rgba(148,163,184,0.05)"},
            "horzLines": {"color": "rgba(148,163,184,0.07)"},
        },
        "crosshair": {
            "mode": 0,
            "vertLine": {"color": "rgba(16,185,129,0.5)", "width": 1, "style": 2, "labelBackgroundColor": "#059669"},
            "horzLine": {"color": "rgba(56,189,248,0.5)", "width": 1, "style": 2, "labelBackgroundColor": "#0284c7"},
        },
        "rightPriceScale": {
            "borderColor": "rgba(148,163,184,0.16)",
            "scaleMargins": {"top": 0.10, "bottom": 0.14},
            "entireTextOnly": True,
        },
        "timeScale": {
            "borderColor": "rgba(148,163,184,0.16)",
            "barSpacing": 8,
            "rightOffset": 5,
            "timeVisible": is_intraday,
            "secondsVisible": False,
        },
    }
    candle_options = {
        "upColor": "#10b981",
        "downColor": "#f43f5e",
        "borderVisible": False,
        "wickUpColor": "#10b981",
        "wickDownColor": "#f43f5e",
        "priceFormat": {"type": "price", "precision": 2, "minMove": 0.05},
        "priceLineVisible": True,
        "priceLineColor": "rgba(148,163,184,0.4)",
        "lastValueVisible": True,
    }
    render_drawing_chart(
        symbol=symbol,
        unit="minutes" if is_intraday else "days",
        interval=5 if is_intraday else 1,
        chart_height=440,
        chart_options=chart_options,
        candle_options=candle_options,
        candles=candles,
        volume=volume_data,
        levels=levels,
        markers=[],
    )


def _altair_area_chart(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    y_title: str = "",
    color: str = "#10b981",
    height: int = 265,
    x_type: str = "O",
) -> alt.Chart:
    """Build a sleek dark-themed gradient area + line chart with hover tooltips."""
    base = alt.Chart(df).encode(
        x=alt.X(
            f"{x_col}:{x_type}",
            title=None,
            sort=None,
            axis=alt.Axis(
                labelColor="#94a3b8",
                gridColor="rgba(148,163,184,0.06)",
                domainColor="rgba(148,163,184,0.16)",
                tickColor="rgba(148,163,184,0.16)",
                labelAngle=0 if len(df) <= 12 else -30,
                labelFont="JetBrains Mono, Inter, sans-serif",
                labelFontSize=11,
            ),
        ),
        y=alt.Y(
            f"{y_col}:Q",
            title=y_title or None,
            scale=alt.Scale(zero=False),
            axis=alt.Axis(
                labelColor="#94a3b8",
                titleColor="#94a3b8",
                gridColor="rgba(148,163,184,0.08)",
                domainColor="rgba(148,163,184,0.16)",
                labelFont="JetBrains Mono, Inter, sans-serif",
                labelFontSize=11,
            ),
        ),
        tooltip=[
            alt.Tooltip(f"{x_col}:{x_type}", title=x_col),
            alt.Tooltip(f"{y_col}:Q", title=y_title or y_col, format=",.2f"),
        ],
    )
    area = base.mark_area(
        line={"color": color, "strokeWidth": 2.3},
        color=alt.Gradient(
            gradient="linear",
            stops=[
                alt.GradientStop(color=color, offset=0),
                alt.GradientStop(color="rgba(15, 23, 42, 0.02)", offset=1),
            ],
            x1=1,
            x2=1,
            y1=0,
            y2=1,
        ),
        opacity=0.38,
    )
    pts = base.mark_circle(size=42, color=color, opacity=0.85)
    return (
        (area + pts)
        .properties(height=height, background="transparent")
        .configure_view(strokeWidth=0)
    )


def _altair_pnl_bar_chart(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    y_title: str = "Net P&L (₹)",
    height: int = 265,
) -> alt.Chart:
    """Build a conditional emerald/rose P&L bar chart with zero baseline."""
    bars = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5, cornerRadiusBottomLeft=5, cornerRadiusBottomRight=5)
        .encode(
            x=alt.X(
                f"{x_col}:O",
                title=None,
                sort=None,
                axis=alt.Axis(
                    labelColor="#94a3b8",
                    domainColor="rgba(148,163,184,0.16)",
                    labelAngle=0 if len(df) <= 12 else -30,
                    labelFont="JetBrains Mono, Inter, sans-serif",
                    labelFontSize=11,
                ),
            ),
            y=alt.Y(
                f"{y_col}:Q",
                title=y_title,
                axis=alt.Axis(
                    labelColor="#94a3b8",
                    titleColor="#94a3b8",
                    gridColor="rgba(148,163,184,0.08)",
                    domainColor="rgba(148,163,184,0.16)",
                    labelFont="JetBrains Mono, Inter, sans-serif",
                    labelFontSize=11,
                ),
            ),
            color=alt.condition(
                alt.datum[y_col] >= 0,
                alt.value("#10b981"),
                alt.value("#f43f5e"),
            ),
            tooltip=[
                alt.Tooltip(f"{x_col}:O", title=x_col),
                alt.Tooltip(f"{y_col}:Q", title=y_title, format=",.2f"),
            ],
        )
    )
    zero_rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color="rgba(148,163,184,0.35)", strokeDash=[4, 4]
    ).encode(y="y:Q")
    return (
        (bars + zero_rule)
        .properties(height=height, background="transparent")
        .configure_view(strokeWidth=0)
    )


def _altair_horiz_bar_chart(
    df: pd.DataFrame,
    cat_col: str,
    val_col: str,
    height: int = 230,
) -> alt.Chart:
    """Horizontal ranked P&L bar chart for symbol and strategy breakdowns."""
    bars = (
        alt.Chart(df)
        .mark_bar(cornerRadius=4)
        .encode(
            y=alt.Y(
                f"{cat_col}:N",
                sort="-x",
                title=None,
                axis=alt.Axis(
                    labelColor="#e2e8f0",
                    domainColor="rgba(148,163,184,0.16)",
                    labelFont="Space Grotesk, Inter, sans-serif",
                    labelFontSize=11,
                    labelLimit=180,
                ),
            ),
            x=alt.X(
                f"{val_col}:Q",
                title="Net P&L (₹)",
                axis=alt.Axis(
                    labelColor="#94a3b8",
                    titleColor="#94a3b8",
                    gridColor="rgba(148,163,184,0.08)",
                    labelFont="JetBrains Mono, Inter, sans-serif",
                    labelFontSize=10,
                ),
            ),
            color=alt.condition(
                alt.datum[val_col] >= 0,
                alt.value("#10b981"),
                alt.value("#f43f5e"),
            ),
            tooltip=[
                alt.Tooltip(f"{cat_col}:N", title=cat_col),
                alt.Tooltip(f"{val_col}:Q", title="Net P&L (₹)", format=",.2f"),
            ],
        )
    )
    return bars.properties(height=height, background="transparent").configure_view(strokeWidth=0)


# ── Signal / outcome tables with gradient success tiers ──────────────────
# Every setup is graded into a visual tier so the eye can rank outcomes at a
# glance: strongest target hit → weakest, plus neutral flat, stop-loss and open.
_TIER_CSS = {
    "tp2": ("tier-tp2", "rgba(16, 185, 129, 0.34)"),
    "tp1": ("tier-tp1", "rgba(16, 185, 129, 0.22)"),
    "trail": ("tier-trail", "rgba(16, 185, 129, 0.16)"),
    "profit": ("tier-profit", "rgba(16, 185, 129, 0.10)"),
    "flat": ("tier-flat", "rgba(148, 163, 184, 0.12)"),
    "loss": ("tier-loss", "rgba(244, 63, 94, 0.12)"),
    "sl": ("tier-sl", "rgba(244, 63, 94, 0.24)"),
    "open": ("tier-open", "rgba(245, 158, 11, 0.12)"),
    "new": ("tier-new", "rgba(16, 185, 129, 0.22)"),
    "armed": ("tier-armed", "rgba(168, 85, 247, 0.16)"),
    "missed": ("tier-missed", "rgba(148, 163, 184, 0.10)"),
    "none": ("tier-none", "rgba(15, 23, 42, 0.55)"),
}

_TIER_CHIP = {
    "tp2": "tp2",
    "tp1": "tp1",
    "trail": "trail",
    "profit": "profit",
    "flat": "flat",
    "loss": "loss",
    "sl": "sl",
    "open": "open",
    "new": "new",
    "armed": "armed",
    "missed": "missed",
    "none": "neutral",
}


def _outcome_tier(outcome, settled=True, net_pnl=None, exit_reason="") -> str:
    """Map a journal outcome / label onto a visual success tier."""
    key = str(outcome or "").upper()
    reason = str(exit_reason or "").upper()

    if key in ("TP2_HIT", "TP2"):
        return "tp2"
    if key in ("TP1_TRAIL",) or (key == "TP1_HIT" and reason == "TRAILING_SL"):
        return "trail"
    if key in ("TP1_HIT", "TARGETS"):
        return "tp1"
    if key in ("EOD_PROFIT", "PROFIT", "CLOSED IN PROFIT"):
        return "profit"
    if key in ("SL_HIT", "SL", "STOP_LOSS"):
        return "sl"
    if key in ("EOD_LOSS", "LOSS", "CLOSED IN LOSS"):
        return "loss"
    if key in ("FLAT", "BREAKEVEN", "NEUTRAL"):
        return "flat"
    if key in ("OPEN", "AWAITING RESULT", "AWAITING", "INCOMPLETE LEVELS"):
        return "open"

    # Fall back to realised P&L when the label is not decisive
    if not settled:
        return "open"
    try:
        pnl = float(net_pnl) if net_pnl is not None else None
    except (TypeError, ValueError):
        pnl = None
    if pnl is None:
        return "open"
    if pnl > 0:
        return "profit"
    if pnl < 0:
        return "loss"
    return "flat"


def _scan_status_tier(status: str) -> str:
    """Map a scanner status onto a visual tier."""
    key = str(status or "none")
    return {
        "NEW": "new",
        "ARMED": "armed",
        "already_alerted": "profit",
        "MISSED": "missed",
        "none": "none",
        "insufficient_data": "none",
        "no_candles": "none",
        "error": "loss",
    }.get(key, "none")


def _side_chip(side) -> tuple[str, str]:
    """Chip class + label for a BUY / SELL signal."""
    text = str(side or "—").upper()
    if "BUY" in text or "LONG" in text or "BULL" in text:
        return "buy", text
    if "SELL" in text or "SHORT" in text or "BEAR" in text:
        return "sell", text
    return "neutral", text


def _outcome_chip(label) -> tuple[str, str]:
    """Chip class + label for a journal outcome label."""
    text = str(label or "Awaiting result")
    key = text.upper()
    if "TP2" in key:
        return "tp2", text
    if "TRAIL" in key:
        return "trail", text
    if "TP1" in key:
        return "tp1", text
    if "SL" in key or "STOP" in key:
        return "sl", text
    if "PROFIT" in key:
        return "profit", text
    if "LOSS" in key:
        return "loss", text
    if "FLAT" in key:
        return "flat", text
    return "open", text


def _zone_chip(zone) -> tuple[str, str]:
    """Chip class + label for an end-of-day zone (PROFIT / LOSS / FLAT)."""
    key = str(zone or "").upper()
    if key == "PROFIT":
        return "profit", "PROFIT"
    if key == "LOSS":
        return "loss", "LOSS"
    if key == "FLAT":
        return "flat", "FLAT"
    return "neutral", "—"


def _status_chip(status) -> tuple[str, str]:
    """Chip class + label for a scanner status."""
    tier = _scan_status_tier(status)
    pretty = {
        "new": "NEW",
        "armed": "ARMED",
        "profit": "ALERTED",
        "missed": "MISSED",
        "none": "NO SIGNAL",
        "loss": "ERROR",
    }.get(tier, str(status).upper())
    return tier, pretty


def _tier_legend(*tiers: str) -> None:
    if not tiers:
        tiers = ("tp2", "tp1", "trail", "profit", "flat", "sl", "open")
    labels = {
        "tp2": "TP2 hit",
        "tp1": "TP1 hit",
        "trail": "TP1 trailed",
        "profit": "Closed in profit",
        "flat": "Flat / breakeven",
        "loss": "Closed in loss",
        "sl": "Stop-loss hit",
        "open": "Open / pending",
        "new": "New signal",
        "armed": "Armed",
        "missed": "Missed",
    }
    items = []
    for tier in tiers:
        if tier not in labels:
            continue
        swatch = {
            "tp2": "linear-gradient(135deg,#10b981,#064e3b)",
            "tp1": "linear-gradient(135deg,#34d399,#065f46)",
            "trail": "linear-gradient(135deg,#6ee7b7,#047857)",
            "profit": "linear-gradient(135deg,#a7f3d0,#10b981)",
            "flat": "linear-gradient(135deg,#cbd5e1,#475569)",
            "loss": "linear-gradient(135deg,#fb7185,#9f1239)",
            "sl": "linear-gradient(135deg,#f43f5e,#881337)",
            "open": "linear-gradient(135deg,#fbbf24,#b45309)",
            "new": "linear-gradient(135deg,#34d399,#059669)",
            "armed": "linear-gradient(135deg,#c084fc,#7c3aed)",
            "missed": "linear-gradient(135deg,#94a3b8,#475569)",
        }[tier]
        items.append(
            f'<span class="lg-item">'
            f'<span class="lg-swatch" style="background:{swatch};"></span>{labels[tier]}'
            f'</span>'
        )
    if items:
        st.markdown(f'<div class="sig-legend">{"".join(items)}</div>', unsafe_allow_html=True)


def _fmt_money(val, decimals: int = 0) -> str:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return html_escape(str(val)) if val is not None else "—"
    if pd.isna(num):
        return "—"
    sign = "-" if num < 0 else ""
    return f"{sign}₹{abs(num):,.{decimals}f}"


def _render_signal_table(
    df: pd.DataFrame,
    tiers: list | None = None,
    *,
    price_cols=(),
    pnl_cols=(),
    pct_cols=(),
    plain_pct_cols=(),
    wrap_cols=(),
    chip_cols: dict | None = None,
    symbol_col: str = "Symbol",
    max_height: int | None = 520,
    empty_msg: str = "No rows to display.",
) -> None:
    """
    Render a premium HTML signal ledger with tiered gradient row backgrounds.

    `tiers` is a list of tier keys (one per row) that controls the gradient:
    tp2 → tp1 → trail → profit (emerald ramp), flat (slate), sl/loss (rose),
    open (amber), new/armed/missed (scanner states).
    """
    if df is None or df.empty:
        st.info(empty_msg)
        return

    price_cols = set(price_cols)
    pnl_cols = set(pnl_cols)
    pct_cols = set(pct_cols)
    plain_pct_cols = set(plain_pct_cols)
    wrap_cols = set(wrap_cols)
    chip_cols = chip_cols or {}

    columns = list(df.columns)
    tier_list = list(tiers) if tiers is not None else ["none"] * len(df)

    def _is_num_col(col) -> bool:
        return (
            col in price_cols
            or col in pnl_cols
            or col in pct_cols
            or col in plain_pct_cols
            or pd.api.types.is_numeric_dtype(df[col])
        )

    head_cells = []
    for col in columns:
        head_cells.append(f'<th class="{"num" if _is_num_col(col) else ""}">{html_escape(str(col))}</th>')

    body_rows = []
    for row_pos, (_, row) in enumerate(df.iterrows()):
        tier = tier_list[row_pos] if row_pos < len(tier_list) else "none"
        row_cls = _TIER_CSS.get(tier, _TIER_CSS["none"])[0]
        cells = []
        for col in columns:
            raw = row.get(col)
            cell_cls = "wrap" if col in wrap_cols else ""
            if col in chip_cols:
                chip_fn = chip_cols[col]
                try:
                    chip_cls, chip_label = chip_fn(raw)
                except Exception:
                    chip_cls, chip_label = "neutral", str(raw)
                cell_html = f'<span class="sig-chip {chip_cls}">{html_escape(str(chip_label))}</span>'
            elif col in price_cols:
                cell_cls = "num"
                cell_html = _fmt_money(raw, 2)
            elif col in pnl_cols:
                cell_cls = "num"
                cell_html = _fmt_money(raw, 0)
                try:
                    num = float(raw)
                    if not pd.isna(num):
                        tone = "#6ee7b7" if num > 0 else ("#fda4af" if num < 0 else "#94a3b8")
                        cell_html = f'<span style="color:{tone};">{cell_html}</span>'
                except (TypeError, ValueError):
                    pass
            elif col in pct_cols:
                cell_cls = "num"
                try:
                    num = float(raw)
                    if pd.isna(num):
                        cell_html = "—"
                    else:
                        tone = "#6ee7b7" if num > 0 else ("#fda4af" if num < 0 else "#94a3b8")
                        cell_html = f'<span style="color:{tone};">{num:+.2f}%</span>'
                except (TypeError, ValueError):
                    cell_html = "—"
            elif col in plain_pct_cols:
                cell_cls = "num"
                try:
                    num = float(raw)
                    cell_html = "—" if pd.isna(num) else f"{num:.0f}%"
                except (TypeError, ValueError):
                    cell_html = "—"
            elif col == symbol_col:
                cell_cls = "sym"
                cell_html = html_escape(str(raw)) if raw not in (None, "") else "—"
            else:
                if pd.api.types.is_numeric_dtype(df[col]):
                    cell_cls = "num"
                    try:
                        num = float(raw)
                        if pd.isna(num):
                            cell_html = "—"
                        elif float(num).is_integer():
                            cell_html = f"{num:,.0f}"
                        else:
                            cell_html = f"{num:,.2f}"
                    except (TypeError, ValueError):
                        cell_html = "—"
                else:
                    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                        cell_html = "—"
                    else:
                        cell_html = html_escape(str(raw))
            cells.append(f'<td class="{cell_cls}">{cell_html}</td>')
        body_rows.append(f'<tr class="{row_cls}">{"".join(cells)}</tr>')

    scroll_style = f' style="max-height:{int(max_height)}px;"' if max_height else ""
    st.markdown(
        f'<div class="sig-table-wrap">'
        f'<div class="sig-table-scroll"{scroll_style}>'
        f'<table class="sig-table">'
        f'<thead><tr>{"".join(head_cells)}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody>'
        f'</table></div></div>',
        unsafe_allow_html=True,
    )


def _render_ohlcv_table(
    df: pd.DataFrame,
    *,
    max_height: int | None = 420,
    empty_msg: str = "No candles to display.",
) -> None:
    """Render an OHLCV frame as a direction-tinted ledger (green / red candles)."""
    if df is None or df.empty:
        st.info(empty_msg)
        return

    out = pd.DataFrame()
    if "timestamp" in df.columns:
        out["Time"] = pd.to_datetime(df["timestamp"], errors="coerce").dt.strftime(
            "%d %b %H:%M"
        )
    for src, dst in (
        ("open", "Open"),
        ("high", "High"),
        ("low", "Low"),
        ("close", "Close"),
        ("volume", "Volume"),
    ):
        if src in df.columns:
            out[dst] = df[src].values

    # Preserve any extra columns (e.g. a pre-computed Colour label)
    for col in df.columns:
        if col not in ("timestamp", "open", "high", "low", "close", "volume"):
            out[col] = df[col].values

    tiers = []
    for _, row in df.iterrows():
        try:
            close_v = float(row.get("close"))
            open_v = float(row.get("open"))
        except (TypeError, ValueError):
            tiers.append("none")
            continue
        if pd.isna(close_v) or pd.isna(open_v):
            tiers.append("none")
        elif close_v > open_v:
            tiers.append("profit")
        elif close_v < open_v:
            tiers.append("loss")
        else:
            tiers.append("flat")

    _render_signal_table(
        out,
        tiers,
        price_cols=("Open", "High", "Low", "Close"),
        max_height=max_height,
        empty_msg=empty_msg,
    )


def _style_signal_df(df: pd.DataFrame, tiers: list | None = None):
    """
    Return a pandas Styler with solid tier tints for use with an interactive
    st.dataframe (keeps sorting / row selection available).
    """
    tier_list = list(tiers) if tiers is not None else ["none"] * len(df)
    fills = [
        _TIER_CSS.get(t, _TIER_CSS["none"])[1] if t != "none" else ""
        for t in tier_list
    ]
    n_cols = len(df.columns)

    def _row_css(_row):
        idx = _row.name
        fill = fills[idx] if isinstance(idx, int) and idx < len(fills) else fills[0] if fills else ""
        return [f"background-color: {fill}" if fill else "" for _ in range(n_cols)]

    return df.style.apply(_row_css, axis=1)


# Apply any queued navigation jump before instantiating the sidebar radio
if "_pending_nav" in st.session_state:
    st.session_state["nav_radio"] = st.session_state.pop("_pending_nav")
if "nav_radio" not in st.session_state or st.session_state["nav_radio"] not in NAV_ITEMS:
    st.session_state["nav_radio"] = (
        DEFAULT_NAV_ITEM if DEFAULT_NAV_ITEM in NAV_ITEMS else NAV_ITEMS[0]
    )


# ── Sidebar (Left Menu) ──────────────────────────────────────────────────
with st.sidebar:
    ts = token_status()
    now_ist = datetime.now(IST)
    mkt_live = is_market_open(now_ist)

    # Brand card with live market state
    st.markdown(
        f"""
        <div class="sidebar-brand">
            <span class="brand-logo">
                📈
                <span class="brand-status {'on' if mkt_live else 'off'}"
                      title="{'NSE market open' if mkt_live else 'NSE market closed'}"></span>
            </span>
            <div class="brand-copy">
                <div class="brand-title">NSE Strategy Desk</div>
                <div class="brand-sub">Read-only · Upstox v3</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Upstox token status badge
    if ts == "found":
        st.markdown('<div class="token-badge ok" title="UPSTOX_ACCESS_TOKEN loaded">🔑 Upstox</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="token-badge missing" title="Set UPSTOX_ACCESS_TOKEN in .env">⚠️ Upstox</div>', unsafe_allow_html=True)

    # ── Appearance quick panel ───────────────────────────────────────────
    _now_theme = _active_theme()
    _swatch_html = "".join(
        f'<span class="ts-swatch" style="background:{color};" '
        f'title="{html_escape(label)} · {color}"></span>'
        for label, color in (
            ("Page", _now_theme["bg"]),
            ("Panel", _now_theme["surface"]),
            ("Accent", _now_theme["accent"]),
            ("Glow", _now_theme["glow"]),
            ("Text", _now_theme["text"]),
        )
    )
    with st.popover("🎨 Appearance", use_container_width=True):
        st.markdown(
            f'<div class="ts-hero" style="margin:0 0 10px;padding:12px 14px;">'
            f'<div><div class="ts-hero-title">{html_escape(_now_theme["preset"])}</div>'
            f'<div class="ts-hero-sub">Live palette · editable</div></div>'
            f'<div class="ts-swatches">{_swatch_html}</div>'
            f"</div>",
            unsafe_allow_html=True,
        )
        st.button(
            "🎨 Open Theme Studio →",
            key="open_theme_studio",
            type="primary",
            use_container_width=True,
            on_click=_navigate_to,
            args=("🎨 Theme Studio",),
        )
        st.caption(
            "Edit colours, panel surface, glow, density and saved palettes in the studio."
        )

    # Persist whenever a control changes (no-op when unchanged)
    _persist_theme()

    # ── Navigation ───────────────────────────────────────────────────────
    st.markdown('<p class="side-label">Workspace</p>', unsafe_allow_html=True)
    nav = st.radio(
        "Workspace",
        NAV_ITEMS,
        key="nav_radio",
        label_visibility="collapsed",
    )

    # ── Engine & timeframe ───────────────────────────────────────────────
    strats = list_strategies()
    strat_labels = {sid: name for sid, name in strats}
    strat_ids = [s[0] for s in strats]
    DEFAULT_STRATEGY = "three_candle_bullish"
    UNITS = ["minutes", "hours", "days", "weeks", "months"]
    TIMEFRAME_CHOICES = {
        "5m": ("minutes", 5),
        "15m": ("minutes", 15),
        "1h": ("hours", 1),
        "1D": ("days", 1),
    }

    if st.session_state.get("engine_select") not in strat_ids:
        st.session_state.pop("engine_select", None)
    _valid_tf_choices = list(TIMEFRAME_CHOICES) + ["Custom"]
    if st.session_state.get("tf_choice") not in _valid_tf_choices:
        st.session_state.pop("tf_choice", None)

    with st.expander("⚙️ Engine & Timeframe", expanded=True):
        with st.container(key="engine_section"):
            st.markdown('<p class="et-eyebrow">Engine</p>', unsafe_allow_html=True)
            selected_strategy = st.selectbox(
                "Engine",
                options=strat_ids,
                index=strat_ids.index(DEFAULT_STRATEGY) if DEFAULT_STRATEGY in strat_ids else 0,
                format_func=lambda x: strat_labels[x],
                key="engine_select",
                label_visibility="collapsed",
                help="Strategy used for backtests, charts and journal attribution.",
            )

            st.markdown('<p class="et-eyebrow">Timeframe</p>', unsafe_allow_html=True)
            is_intraday_strategy = selected_strategy in INTRADAY_5M_STRATEGIES
            default_tf = "5m" if is_intraday_strategy else "1D"

            # Apply the strategy's natural timeframe whenever the engine changes
            if st.session_state.get("_tf_strategy") != selected_strategy:
                st.session_state["_tf_strategy"] = selected_strategy
                st.session_state["tf_choice"] = default_tf
                st.session_state["unit_select"] = TIMEFRAME_CHOICES[default_tf][0]
                st.session_state["interval_input"] = TIMEFRAME_CHOICES[default_tf][1]
            if st.session_state.get("tf_choice") not in _valid_tf_choices:
                st.session_state["tf_choice"] = default_tf

            tf_choice = st.pills(
                "Timeframe",
                options=_valid_tf_choices,
                key="tf_choice",
                label_visibility="collapsed",
                help="Candle size for scans, backtests and charts · pick Custom for any unit/interval.",
            )
            if not tf_choice:
                tf_choice = default_tf

            if tf_choice == "Custom":
                u_col, i_col = st.columns([1.25, 1])
                with u_col:
                    unit = st.selectbox(
                        "Unit", UNITS, key="unit_select", label_visibility="collapsed"
                    )
                with i_col:
                    interval = st.number_input(
                        "Interval",
                        min_value=1,
                        max_value=300,
                        key="interval_input",
                        label_visibility="collapsed",
                    )
                st.caption("Custom candle size · 1–300")
            else:
                unit, interval = TIMEFRAME_CHOICES[tf_choice]

            st.markdown('<p class="et-eyebrow">Alert coverage</p>', unsafe_allow_html=True)
            scan_all_engines = st.toggle(
                f"Scan all {len(strat_ids)} engines",
                value=True,
                key="scan_all_engines",
                help=(
                    "On: every registered engine is evaluated on each watchlist scan. "
                    "Off: pick a subset below."
                ),
            )
            if scan_all_engines:
                tracked_strategies = list(strat_ids)
            else:
                if "scanner_engine_ids" in st.session_state:
                    st.session_state["scanner_engine_ids"] = [
                        sid
                        for sid in st.session_state.get("scanner_engine_ids") or []
                        if sid in strat_ids
                    ]
                else:
                    st.session_state["scanner_engine_ids"] = list(strat_ids)
                tracked_strategies = st.multiselect(
                    "Engines",
                    options=strat_ids,
                    format_func=lambda x: strat_labels[x],
                    key="scanner_engine_ids",
                    label_visibility="collapsed",
                    placeholder="Pick engines to scan…",
                )
                if not tracked_strategies:
                    tracked_strategies = [selected_strategy]

            st.markdown(
                f'<div class="et-foot">'
                f'<span>⏱ <b>{_tf_display(interval, unit)}</b></span>'
                f'<span>🧠 <b>{len(tracked_strategies)}</b> engines</span>'
                f"</div>",
                unsafe_allow_html=True,
            )

    # API Telemetry & Instruments mini-panel
    api_m = tracker.get_metrics()
    resolver: InstrumentResolver = st.session_state.resolver
    local_csv_count = len(list(DATA_DIR.glob("*_*.csv"))) if DATA_DIR.exists() else 0

    st.markdown('<p class="side-label">System telemetry</p>', unsafe_allow_html=True)
    inst_count_str = f"{len(resolver.available_symbols):,}" if resolver.is_loaded else "Fallback"
    st.markdown(f"""
    <div class="telemetry-row">
        <span class="tl-label">NSE Master</span>
        <span class="tl-value" style="color:var(--accent-2);">{inst_count_str}</span>
    </div>
    <div class="telemetry-row">
        <span class="tl-label">Disk Cache</span>
        <span class="tl-value">{local_csv_count} files</span>
    </div>
    <div class="telemetry-row">
        <span class="tl-label">Live Calls / Hits</span>
        <span class="tl-value">{api_m['total_live_calls']} / {api_m['cache_hits']}</span>
    </div>
    <div class="telemetry-row">
        <span class="tl-label">Avg Latency</span>
        <span class="tl-value">{api_m['avg_latency_ms']} ms</span>
    </div>
    """, unsafe_allow_html=True)
    if not resolver.is_loaded and resolver.error:
        st.caption(f"⚠️ {resolver.error}")

    st.markdown(
        f"""
        <div class="side-footer">
            <div class="sf-theme">🎨 {html_escape(_active_theme()['preset'])} theme</div>
            <div>🛡️ Read-only · alert-only terminal</div>
            <div>Manual execution · estimates only</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── Global Top Command Ribbon (shown on every view) ──────────────────────
def _render_top_ribbon() -> None:
    now_ist = datetime.now(IST)
    mkt_open = is_market_open(now_ist)
    wl_syms = load_watchlist()
    cache_cnt = len(list(DATA_DIR.glob("*_*.csv"))) if DATA_DIR.exists() else 0

    if mkt_open:
        mkt_pill = (
            f'<span class="ribbon-pill emerald">'
            f'<span class="pulse-dot green" style="width:7px;height:7px;"></span>'
            f'NSE LIVE · {now_ist.strftime("%H:%M IST")}'
            f'</span>'
        )
    else:
        mkt_pill = (
            f'<span class="ribbon-pill amber">'
            f'🌙 NSE CLOSED · {now_ist.strftime("%a %d %b · %H:%M IST")}'
            f'</span>'
        )

    tok_pill = (
        '<span class="ribbon-pill emerald">🔑 Upstox Connected</span>'
        if ts == "found"
        else '<span class="ribbon-pill rose">⚠️ Upstox Token Needed</span>'
    )
    tf_pill = f'<span class="ribbon-pill cyan">⏱️ {int(interval)} {unit} · {len(tracked_strategies)} Engines</span>'
    wl_pill = f'<span class="ribbon-pill">📋 {len(wl_syms)} Watchlist · 💾 {cache_cnt} Cached</span>'
    safe_pill = '<span class="ribbon-pill emerald">🛡️ Alert-Only · Manual Execution</span>'

    st.markdown(
        f'<div class="top-command-ribbon">'
        f'<div class="ribbon-group">{mkt_pill}{tok_pill}{tf_pill}</div>'
        f'<div class="ribbon-group">{wl_pill}{safe_pill}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


_render_top_ribbon()


# ══════════════════════════════════════════════════════════════════════════
# VIEW 0 — COMMAND CENTER
# ══════════════════════════════════════════════════════════════════════════
def render_command_center():
    section_header(
        "🏠 Trading Command Center",
        "Watchlist pulse · Multi-strategy signal feed · Journal equity analytics · Interactive cached chart desk"
    )

    wl = load_watchlist()
    journal_entries = load_journal()
    alert_hist = st.session_state.get("alert_history", [])
    j_stats = summary_stats(journal_entries)

    # ── Quick Action Launcher Bar ─────────────────────────────────────────
    qa1, qa2, qa3, qa4 = st.columns(4)
    with qa1:
        st.button(
            "💼 Today's P&L",
            type="primary",
            use_container_width=True,
            key="cc_go_pnl",
            on_click=_navigate_to,
            args=("💼 Today's P&L",),
        )
    with qa2:
        st.button(
            "🔍 Open Live Scanner",
            use_container_width=True,
            key="cc_go_alerts",
            on_click=_navigate_to,
            args=("🔔 Alerts",),
        )
    with qa3:
        st.button(
            "📊 Run Strategy Backtest",
            use_container_width=True,
            key="cc_go_bt",
            on_click=_navigate_to,
            args=("📊 Backtest",),
        )
    with qa4:
        st.button(
            "📒 Review Trade Journal",
            use_container_width=True,
            key="cc_go_journal",
            on_click=_navigate_to,
            args=("📒 Trade Journal",),
        )

    st.write("")

    # ── Headline KPI Strip ────────────────────────────────────────────────
    cached_wl_count = sum(1 for s in wl if _load_local_symbol_df(s)[1])
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    with k1:
        metric_card(
            "Watchlist",
            f"{len(wl)}/{MAX_WATCHLIST}",
            delta=f"{cached_wl_count} cached locally",
            color="cyan",
            icon="📋",
        )
    with k2:
        metric_card(
            "Tracked Engines",
            str(len(tracked_strategies)),
            delta=f"{int(interval)} {unit}",
            color="blue",
            icon="🧠",
        )
    with k3:
        metric_card(
            "Journal Setups",
            str(j_stats["count"]),
            delta=f"{j_stats['targets']} TP · {j_stats['stops']} SL",
            color="purple",
            icon="📒",
        )
    with k4:
        wr = j_stats["win_rate"]
        metric_card(
            "Journal Win Rate",
            f"{wr:.0f}%" if j_stats["count"] else "—",
            delta=f"TP rate {j_stats['tp_rate']:.0f}%" if j_stats["count"] else None,
            color="green" if wr >= 50 else "gold",
            icon="🎯",
        )
    with k5:
        net_val = j_stats["net"]
        metric_card(
            "Hypothetical P&L",
            f"₹{net_val:,.0f}" if j_stats["count"] else "₹0",
            delta=f"Avg ₹{j_stats['avg']:,.0f}/setup" if j_stats["count"] else None,
            color="green" if net_val >= 0 else "red",
            icon="💰",
        )
    with k6:
        pf = j_stats["profit_factor"]
        pf_str = ("∞" if pf == float("inf") else f"{pf:.2f}") if j_stats["count"] else "—"
        metric_card(
            "Profit Factor",
            pf_str,
            delta=f"{len(alert_hist)} saved alerts",
            color="gold",
            icon="⚖️",
        )

    # ── Watchlist Market Pulse (Local Cache + Sparklines) ─────────────────
    st.markdown(
        '<div style="display:flex;align-items:baseline;justify-content:space-between;margin:18px 0 10px;">'
        '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;font-size:1.12rem;margin:0;">'
        '⚡ Watchlist Market Pulse <span style="font-size:0.78rem;color:#64748b;font-weight:500;">'
        '· Instant snapshot from local disk cache (zero API calls)</span></h3>'
        '</div>',
        unsafe_allow_html=True,
    )
    if wl:
        _render_watchlist_snapshot_grid(wl, max_cards=8)
        if len(wl) > 8:
            st.caption(f"Showing top 8 of {len(wl)} watchlist symbols · Open **🔔 Alerts** to manage all {len(wl)} stocks.")
    else:
        st.info("Your watchlist is currently empty. Add NSE symbols on the **🔔 Alerts** page.")

    # ── Two-Column Analytics & Signal Feed ────────────────────────────────
    col_left, col_right = st.columns([1.45, 1.0])

    with col_left:
        st.markdown(
            '<div class="cc-panel">'
            '<h3 style="font-family:Space Grotesk,sans-serif;font-size:1.05rem;margin:0 0 4px;color:#f1f5f9;">'
            '📈 Cumulative Hypothetical P&amp;L &amp; Strategy Performance</h3>'
            '<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px;">'
            'Derived from persisted Trade Journal setups across tracked 5-minute bullish engines</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if journal_entries:
            sorted_j = sorted(
                journal_entries,
                key=lambda e: (str(e.get("date") or ""), str(e.get("entry_time") or "")),
            )
            running = 0.0
            curve_rows = []
            for idx, item in enumerate(sorted_j, 1):
                pnl = float(item.get("net_pnl") or 0.0)
                running += pnl
                label = f"#{idx} {item.get('symbol', '')}"
                curve_rows.append({
                    "Setup": label,
                    "Cumulative P&L (₹)": round(running, 2),
                    "Setup P&L (₹)": round(pnl, 2),
                })
            curve_df = pd.DataFrame(curve_rows)
            curve_color = "#10b981" if running >= 0 else "#f43f5e"
            st.altair_chart(
                _altair_area_chart(
                    curve_df,
                    "Setup",
                    "Cumulative P&L (₹)",
                    "Cumulative Net P&L (₹)",
                    color=curve_color,
                    height=235,
                ),
                use_container_width=True,
            )

            # Strategy leaderboard bar chart
            strat_summary_rows = []
            for sid in sorted({e.get("strategy") for e in journal_entries if e.get("strategy")}):
                sub_e = [e for e in journal_entries if e.get("strategy") == sid]
                st_s = summary_stats(sub_e)
                strat_summary_rows.append({
                    "Strategy": get_display_name(sid),
                    "Net P&L": round(st_s["net"], 2),
                })
            if strat_summary_rows:
                st.markdown(
                    '<p style="font-size:0.8rem;font-weight:600;color:#94a3b8;margin:8px 0 4px;">'
                    '🏆 Net P&amp;L by Strategy Engine</p>',
                    unsafe_allow_html=True,
                )
                st.altair_chart(
                    _altair_horiz_bar_chart(
                        pd.DataFrame(strat_summary_rows),
                        "Strategy",
                        "Net P&L",
                        height=max(165, 34 * len(strat_summary_rows)),
                    ),
                    use_container_width=True,
                )
        else:
            st.info("No journal setups logged yet. Run a Watchlist Scan or Journal Sync to populate live equity analytics.")

    with col_right:
        st.markdown(
            '<div class="cc-panel">'
            '<h3 style="font-family:Space Grotesk,sans-serif;font-size:1.05rem;margin:0 0 4px;color:#f1f5f9;">'
            '🔔 Recent Setups &amp; Signal Feed</h3>'
            '<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:4px;">'
            'Latest triggered setups with entry, stop-loss, TP1 &amp; settled outcome</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if journal_entries:
            recent_j = sorted(
                journal_entries,
                key=lambda e: (str(e.get("date") or ""), str(e.get("entry_time") or "")),
                reverse=True,
            )[:6]
            for e in recent_j:
                net_pnl = float(e.get("net_pnl") or 0.0)
                settled = bool(e.get("settled"))
                cls = "open" if not settled else ("loss" if net_pnl < 0 else "")
                pnl_color = "#fbbf24" if not settled else ("#34d399" if net_pnl >= 0 else "#fb7185")
                pnl_str = "OPEN" if not settled else f"{'+' if net_pnl >= 0 else ''}₹{net_pnl:,.0f}"
                strat_nm = e.get("strategy_name") or get_display_name(str(e.get("strategy") or ""))
                t_short = str(e.get("entry_time") or "")[:16].replace("T", " ")
                entry_p = float(e.get("entry_price") or 0.0)
                sl_p = float(e.get("stop_loss") or 0.0)
                tp1_p = float(e.get("tp1") or 0.0)
                outcome_lbl = e.get("outcome_label") or "In Progress"
                st.markdown(
                    f'<div class="sig-feed-card {cls}">'
                    f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                    f'<div><span style="font-family:Space Grotesk,sans-serif;font-weight:700;font-size:0.95rem;color:#f8fafc;">'
                    f'{e.get("symbol", "?")}</span> '
                    f'<span style="font-size:0.72rem;color:#34d399;font-weight:600;margin-left:4px;">{e.get("side", "BUY")}</span></div>'
                    f'<span style="font-family:JetBrains Mono,monospace;font-weight:700;font-size:0.84rem;color:{pnl_color};">{pnl_str}</span>'
                    f'</div>'
                    f'<div style="font-size:0.74rem;color:#94a3b8;margin-top:3px;">'
                    f'{strat_nm} &nbsp;·&nbsp; <span style="font-family:JetBrains Mono,monospace;">{t_short}</span></div>'
                    f'<div style="display:flex;justify-content:space-between;font-family:JetBrains Mono,monospace;'
                    f'font-size:0.72rem;color:#cbd5e1;margin-top:6px;padding-top:5px;border-top:1px solid rgba(148,163,184,0.10);">'
                    f'<span>Entry ₹{entry_p:,.2f}</span>'
                    f'<span style="color:#fb7185;">SL ₹{sl_p:,.2f}</span>'
                    f'<span style="color:#34d399;">TP1 ₹{tp1_p:,.2f}</span>'
                    f'<span style="color:#94a3b8;">{outcome_lbl}</span>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        elif alert_hist:
            for a in list(reversed(alert_hist[-5:])):
                st.markdown(
                    f'<div class="sig-feed-card">'
                    f'<div style="font-weight:700;color:#f8fafc;">{a.get("symbol", "?")} · {a.get("side", "")}</div>'
                    f'<div style="font-size:0.76rem;color:#94a3b8;">{a.get("message", "")}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("No setups recorded yet. Click **Open Live Scanner** above to evaluate your watchlist.")

    # ── Interactive Cached Candlestick Desk ───────────────────────────────
    cached_syms = [s for s in wl if _load_local_symbol_df(s)[1]]
    if cached_syms:
        st.markdown("---")
        ch_head1, ch_head2 = st.columns([3, 1])
        with ch_head1:
            st.markdown(
                '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;font-size:1.12rem;margin:0;">'
                '🕯️ Quick Chart Desk <span style="font-size:0.78rem;color:#64748b;font-weight:500;">'
                '· Interactive candlestick &amp; drawing tools on cached data</span></h3>',
                unsafe_allow_html=True,
            )
        with ch_head2:
            cc_sym = st.selectbox(
                "Symbol",
                options=cached_syms,
                key="cc_chart_sym_pick",
                label_visibility="collapsed",
            )
        _render_cached_chart_preview(cc_sym, key_prefix="cc_")


# ══════════════════════════════════════════════════════════════════════════
# VIEW 1 — API CALLS & DATA MONITOR
# ══════════════════════════════════════════════════════════════════════════
def render_api_monitor():
    section_header(
        "📡 API Calls & Data Monitor",
        "Live network calls to Upstox API v3 · Cache efficiency · Latency metrics · OHLCV preview"
    )

    st.info(
        "🛡️ **Read-Only Analytics Mode** — Queries market candles and instrument master only. "
        "Order placement and portfolio endpoints are strictly disabled. "
        "Your token is safely kept in `.env` and never displayed."
    )

    # 1. Key Metrics Cards
    m = tracker.get_metrics()
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        metric_card("Live API Calls", str(m['total_live_calls']), color="blue", icon="🌐")
    with c2:
        metric_card("Cache Hits", str(m['cache_hits']), delta=f"{m['cache_savings_pct']}% saved", color="green", icon="⚡")
    with c3:
        metric_card("Avg Latency", f"{m['avg_latency_ms']} ms", color="cyan", icon="⏱️")
    with c4:
        metric_card("Rate Limits", str(m['rate_limits_429']), color="red" if m['rate_limits_429'] > 0 else "gold", icon="🚦")
    with c5:
        metric_card("Candles Fetched", f"{m['total_candles']:,}", color="purple", icon="🕯️")
    with c6:
        health_color = "green" if m["health"] in ("Excellent", "Good") else "gold"
        metric_card("API Health", m["health"], color=health_color, icon="💚")

    # 1b. Local dataset inventory strip
    cache_files = list(DATA_DIR.glob("*_*.csv")) if DATA_DIR.exists() else []
    total_cache_kb = sum(cf.stat().st_size / 1024 for cf in cache_files)
    i1, i2, i3, i4 = st.columns(4)
    with i1:
        metric_card("Cached Datasets", f"{len(cache_files)}", color="cyan", icon="💾")
    with i2:
        metric_card("Disk Footprint", f"{total_cache_kb:,.0f} KB", color="blue", icon="🗄️")
    with i3:
        mem_ds_count = len(tracker.get_latest_datasets())
        metric_card("In-Memory Sets", f"{mem_ds_count}", color="purple", icon="🧠")
    with i4:
        metric_card("Cache Savings", f"{m['cache_savings_pct']}%", color="green", icon="📉")

    st.write("")

    # 2. Quick API Connection Test
    with st.expander("🧪 Test Upstox API Call", expanded=(m["total_live_calls"] == 0)):
        st.write("Send a live request to Upstox API v3 to test your token, verify connectivity, and inspect the round-trip response.")
        col_t1, col_t2, col_t3, col_t4 = st.columns([2, 2, 2, 2])
        test_sym = col_t1.selectbox("Symbol", ["RELIANCE", "INFY", "TCS"], key="test_sym_select")
        test_u = col_t2.selectbox("Unit", ["days", "minutes"], key="test_u_select")
        test_i = col_t3.number_input("Interval", min_value=1, value=1, key="test_i_select")
        col_t4.write("")
        if col_t4.button("🚀 Send Test Call", type="primary"):
            if ts == "missing":
                st.error("UPSTOX_ACCESS_TOKEN is missing in .env. Please set it first.")
            else:
                ikey = resolver.resolve(test_sym)
                if not ikey:
                    st.error(f"Could not resolve key for {test_sym}")
                else:
                    with st.spinner(f"Calling Upstox API for {test_sym}..."):
                        try:
                            if test_u == "days":
                                end_d = date.today()
                                start_d = end_d - timedelta(days=7)
                                df_res = fetch_historical(ikey, test_sym, test_u, int(test_i), start_d, end_d, no_cache=True)
                            else:
                                df_res = fetch_intraday(ikey, test_sym, test_u, int(test_i))
                            st.success(f"✅ Success! Received {len(df_res)} candles for {test_sym}.")
                            st.rerun()
                        except Exception as e:
                            err = str(e)
                            if UPSTOX_ACCESS_TOKEN:
                                err = err.replace(UPSTOX_ACCESS_TOKEN, "***")
                            st.error(f"API Call Failed: {err}")

    # 3. API Calls History Table
    st.markdown("---")
    st.markdown('<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; font-size: 1.1rem; margin-bottom: 12px;">📋 API Call History</h3>', unsafe_allow_html=True)
    calls_df = tracker.get_calls_df()

    fcol1, fcol2, fcol3 = st.columns([3, 3, 2])
    with fcol1:
        type_options = ["All"] + sorted(list(set(calls_df["Type"].dropna().unique()))) if not calls_df.empty else ["All"]
        filter_type = st.selectbox("Filter by Type", type_options, key="filter_type_select")
    with fcol2:
        sym_options = ["All"] + sorted(list(set(calls_df["Symbol"].dropna().unique()))) if not calls_df.empty else ["All"]
        filter_sym = st.selectbox("Filter by Symbol", sym_options, key="filter_sym_select")
    with fcol3:
        st.write("")
        if st.button("🗑️ Clear Log", key="btn_clear_api_log"):
            tracker.clear()
            st.rerun()

    filtered_df = calls_df.copy()
    if not filtered_df.empty:
        if filter_type != "All":
            filtered_df = filtered_df[filtered_df["Type"] == filter_type]
        if filter_sym != "All":
            filtered_df = filtered_df[filtered_df["Symbol"] == filter_sym]

    if not filtered_df.empty:
        # Light tinting: errors rose, rate limits amber, cache hits emerald
        call_tiers = []
        for _, crow in filtered_df.iterrows():
            status_v = str(crow.get("Status") or "").upper()
            type_v = str(crow.get("Type") or "").upper()
            if "ERR" in status_v or "FAIL" in status_v or " 4" in status_v or status_v.startswith("4") or status_v.startswith("5"):
                call_tiers.append("loss")
            elif "429" in status_v or "RATE" in status_v:
                call_tiers.append("open")
            elif "CACHE" in type_v or "HIT" in status_v or "200" in status_v:
                call_tiers.append("profit")
            else:
                call_tiers.append("none")
        try:
            st.dataframe(
                _style_signal_df(filtered_df, call_tiers),
                use_container_width=True,
                hide_index=True,
            )
        except Exception:
            st.dataframe(filtered_df, use_container_width=True, hide_index=True)
        csv_log = filtered_df.to_csv(index=False)
        st.download_button(
            "📥 Download API Calls Log (CSV)",
            csv_log,
            file_name="upstox_api_calls_log.csv",
            mime="text/csv",
        )

        # Request distribution visual
        try:
            if "Symbol" in filtered_df.columns:
                dist = (
                    filtered_df.groupby("Symbol", dropna=True)
                    .size()
                    .reset_index(name="Calls")
                    .sort_values("Calls", ascending=False)
                    .head(12)
                )
                if not dist.empty:
                    st.markdown(
                        '<p style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                        'margin:14px 0 4px;">📊 Requests per Symbol</p>',
                        unsafe_allow_html=True,
                    )
                    bars = (
                        alt.Chart(dist)
                        .mark_bar(cornerRadius=4, color="#38bdf8")
                        .encode(
                            x=alt.X(
                                "Symbol:N",
                                title=None,
                                sort="-y",
                                axis=alt.Axis(
                                    labelColor="#94a3b8",
                                    labelAngle=-35,
                                    domainColor="rgba(148,163,184,0.16)",
                                    labelFont="JetBrains Mono, Inter, sans-serif",
                                    labelFontSize=10,
                                ),
                            ),
                            y=alt.Y(
                                "Calls:Q",
                                title="Requests",
                                axis=alt.Axis(
                                    labelColor="#94a3b8",
                                    titleColor="#94a3b8",
                                    gridColor="rgba(148,163,184,0.08)",
                                    labelFontSize=10,
                                ),
                            ),
                            tooltip=["Symbol:N", "Calls:Q"],
                        )
                        .properties(height=210, background="transparent")
                        .configure_view(strokeWidth=0)
                    )
                    st.altair_chart(bars, use_container_width=True)
        except Exception:
            pass
    else:
        st.info("No API calls recorded yet. Run a Backtest, execute a Watchlist Scan, or use the Test Call button above.")

    st.divider()

    # 4. Data Fetched Inspector
    st.markdown('<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; font-size: 1.1rem; margin-bottom: 4px;">🔍 Data Fetched & OHLCV Preview</h3>', unsafe_allow_html=True)
    st.caption("Inspect live candles and local cache datasets.")

    mem_datasets = tracker.get_latest_datasets()
    cache_files = list(DATA_DIR.glob("*_*.csv")) if DATA_DIR.exists() else []

    dataset_options = list(mem_datasets.keys())
    for cf in cache_files:
        label = f"Disk Cache: {cf.stem}"
        if label not in dataset_options:
            dataset_options.append(label)

    if dataset_options:
        sel_dataset = st.selectbox("Select Dataset to Inspect", dataset_options)
        df_inspect = None
        if sel_dataset in mem_datasets:
            df_inspect = mem_datasets[sel_dataset]
        else:
            clean_name = sel_dataset.replace("Disk Cache: ", "") + ".csv"
            fpath = DATA_DIR / clean_name
            if fpath.exists():
                try:
                    df_inspect = pd.read_csv(fpath)
                    if "timestamp" in df_inspect.columns:
                        df_inspect["timestamp"] = pd.to_datetime(df_inspect["timestamp"], utc=True).dt.tz_convert(IST)
                except Exception:
                    df_inspect = None

        if df_inspect is not None and not df_inspect.empty:
            d1, d2, d3, d4, d5 = st.columns(5)
            min_ts = df_inspect["timestamp"].min() if "timestamp" in df_inspect.columns else "N/A"
            max_ts = df_inspect["timestamp"].max() if "timestamp" in df_inspect.columns else "N/A"
            latest_close = df_inspect["close"].iloc[-1] if "close" in df_inspect.columns else 0.0
            min_p = df_inspect["low"].min() if "low" in df_inspect.columns else 0.0
            max_p = df_inspect["high"].max() if "high" in df_inspect.columns else 0.0

            with d1:
                metric_card("Total Rows", f"{len(df_inspect):,}", color="blue")
            with d2:
                metric_card("Latest Close", f"₹{latest_close:,.2f}", color="green")
            with d3:
                metric_card("Price Range", f"₹{min_p:.0f}–{max_p:.0f}", color="gold")
            with d4:
                metric_card("From Date", str(min_ts)[:10] if min_ts != "N/A" else "N/A", color="cyan")
            with d5:
                metric_card("To Date", str(max_ts)[:10] if max_ts != "N/A" else "N/A", color="purple")

            # Data Quality Audit Card
            with st.expander("🩺 Data Quality & Integrity Check", expanded=False):
                q1, q2, q3, q4 = st.columns(4)
                null_count = df_inspect.isnull().sum().sum()
                dup_count = df_inspect.duplicated(subset=["timestamp"]).sum() if "timestamp" in df_inspect.columns else 0
                with q1:
                    metric_card("Null / NaN", str(null_count), color="red" if null_count > 0 else "green")
                with q2:
                    metric_card("Duplicate TS", str(dup_count), color="red" if dup_count > 0 else "green")
                with q3:
                    metric_card("Timezone", "IST / Asia/Kolkata", color="blue")
                with q4:
                    is_chrono = df_inspect["timestamp"].is_monotonic_increasing if "timestamp" in df_inspect.columns else False
                    metric_card("Sorted", "✅ Yes" if is_chrono else "⚠️ No", color="green" if is_chrono else "red")

            # Data Table Preview
            view_mode = st.radio("Display Rows", ["First 5 & Last 5 Rows", "Full Table"], horizontal=True, key="view_rows_radio")
            if view_mode == "First 5 & Last 5 Rows" and len(df_inspect) > 10:
                preview_df = pd.concat([df_inspect.head(5), df_inspect.tail(5)])
                _render_ohlcv_table(preview_df, max_height=380)
            else:
                _render_ohlcv_table(df_inspect, max_height=460)

            # Price trend chart
            if "close" in df_inspect.columns and "timestamp" in df_inspect.columns:
                st.markdown(
                    '<p style="font-weight:600; color:#94a3b8; margin-top:16px; margin-bottom:4px;">'
                    '📈 Close Price Trend</p>',
                    unsafe_allow_html=True,
                )
                trend_df = df_inspect[["timestamp", "close"]].dropna().copy()
                if len(trend_df) > 320:
                    trend_df = trend_df.tail(320)
                trend_df["Time"] = trend_df["timestamp"].dt.strftime("%d %b %H:%M")
                last_close = float(trend_df["close"].iloc[-1])
                first_close = float(trend_df["close"].iloc[0])
                trend_color = "#10b981" if last_close >= first_close else "#f43f5e"
                st.altair_chart(
                    _altair_area_chart(
                        trend_df.rename(columns={"close": "Close (₹)"}),
                        "Time",
                        "Close (₹)",
                        "Close Price (₹)",
                        color=trend_color,
                        height=250,
                    ),
                    use_container_width=True,
                )
    else:
        st.info("No data fetched yet. Run a Backtest or click 'Send Test Call' above.")

    st.divider()

    # 5. Local Cache Storage Status
    st.markdown('<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; font-size: 1.1rem; margin-bottom: 4px;">💾 Local Disk Cache</h3>', unsafe_allow_html=True)
    if cache_files:
        cache_records = []
        total_cache_size_kb = 0.0
        for cf in cache_files:
            size_kb = round(cf.stat().st_size / 1024, 1)
            total_cache_size_kb += size_kb
            mod_time = datetime.fromtimestamp(cf.stat().st_mtime, tz=IST).strftime("%Y-%m-%d %H:%M:%S")
            cache_records.append({
                "File": cf.name,
                "Size (KB)": size_kb,
                "Last Modified (IST)": mod_time,
            })
        cache_df = pd.DataFrame(cache_records)

        cv1, cv2 = st.columns([1, 1])
        with cv1:
            st.markdown(
                '<p style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                'margin:4px 0 2px;">📦 Largest Cached Datasets</p>',
                unsafe_allow_html=True,
            )
            top_cache = cache_df.sort_values("Size (KB)", ascending=False).head(10)
            top_cache = top_cache.assign(
                Dataset=top_cache["File"].str.replace(r"\.csv$", "", regex=True)
            )
            st.altair_chart(
                _altair_horiz_bar_chart(
                    top_cache[["Dataset", "Size (KB)"]],
                    "Dataset",
                    "Size (KB)",
                    height=max(180, 26 * len(top_cache)),
                ),
                use_container_width=True,
            )
        with cv2:
            st.markdown(
                '<p style="font-size:0.8rem;font-weight:600;color:#94a3b8;'
                'margin:4px 0 2px;">🗂️ Cache File Index</p>',
                unsafe_allow_html=True,
            )
            cache_sorted = cache_df.sort_values("File").reset_index(drop=True)
            cache_tiers = []
            for _, crow in cache_sorted.iterrows():
                try:
                    kb = float(crow.get("Size (KB)") or 0)
                except (TypeError, ValueError):
                    kb = 0.0
                cache_tiers.append(
                    "profit" if kb >= 500 else ("flat" if kb >= 100 else "none")
                )
            _render_signal_table(
                cache_sorted,
                cache_tiers,
                max_height=max(180, 26 * len(top_cache)),
            )
        st.caption(f"Total: {len(cache_files)} files · {total_cache_size_kb:.1f} KB · Data is cached locally to prevent exceeding Upstox API rate limits.")
    else:
        st.info("No cache files saved on disk yet.")


# ══════════════════════════════════════════════════════════════════════════
# VIEW 2 — BACKTEST
# ══════════════════════════════════════════════════════════════════════════
def _render_backtest_result(payload: dict) -> None:
    """Render a stored backtest result with rich, dark-themed analytics."""
    m = payload["metrics"]
    trades = payload["trades"]
    equity = payload["equity_curve"]
    symbol = payload["symbol"]
    strat_name = get_display_name(payload["strategy"])
    source = payload.get("source", "live")

    st.markdown("---")
    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.markdown(
            f'<h3 style="font-family:Space Grotesk,sans-serif;color:#f8fafc;'
            f'font-size:1.15rem;margin:0;">📊 Backtest Results · '
            f'<span style="color:#34d399;">{symbol}</span></h3>'
            f'<span style="font-size:0.78rem;color:#94a3b8;">{strat_name} · '
            f'{"Local disk cache" if source == "cache" else "Live Upstox candles"} · '
            f'{payload.get("candles", 0):,} candles simulated</span>',
            unsafe_allow_html=True,
        )
    with head_r:
        if st.button("🧹 Clear results", key="bt_clear_results", use_container_width=True):
            st.session_state.pop("bt_result", None)
            st.rerun()

    # ── Metrics ───────────────────────────────────────────────────────────
    mc1, mc2, mc3, mc4 = st.columns(4)
    with mc1:
        metric_card("Total Trades", str(m["trades"]), color="blue", icon="📋")
    with mc2:
        win_color = "green" if m["win_rate_pct"] >= 50 else "red"
        metric_card("Win Rate", f"{m['win_rate_pct']}%", color=win_color, icon="🎯")
    with mc3:
        pnl_color = "green" if m["total_pnl"] >= 0 else "red"
        delta_sign = "+" if m["total_pnl"] >= 0 else ""
        metric_card("Total PnL", f"₹{m['total_pnl']:,.0f}", delta=f"{delta_sign}₹{m['total_pnl']:,.0f} net", color=pnl_color, icon="💰")
    with mc4:
        metric_card("Max Drawdown", f"{m['max_drawdown_pct']}%", color="red", icon="📉")

    mc5, mc6, mc7, mc8 = st.columns(4)
    with mc5:
        ret_color = "green" if m["total_return_pct"] >= 0 else "red"
        metric_card("Total Return", f"{m['total_return_pct']}%", color=ret_color, icon="📈")
    with mc6:
        metric_card("Avg Win", f"₹{m['avg_win']:,.0f}", color="green", icon="✅")
    with mc7:
        metric_card("Avg Loss", f"₹{m['avg_loss']:,.0f}", color="red", icon="❌")
    with mc8:
        gross_w = float(m.get("sum_wins") or 0.0) or float(m["avg_win"]) * max(1, sum(1 for t in trades if float(t.get("net_pnl") or 0) > 0))
        gross_l = abs(float(m.get("sum_losses") or 0.0)) or abs(float(m["avg_loss"])) * max(1, sum(1 for t in trades if float(t.get("net_pnl") or 0) < 0))
        pf = (gross_w / gross_l) if gross_l else float("inf")
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        metric_card("Profit Factor", pf_str, color="purple", icon="⚖️")

    st.caption(m.get("cost_note", ""))
    st.caption(m.get("drawdown_note", ""))

    # ── Equity & drawdown ─────────────────────────────────────────────────
    if len(equity) > 1:
        eq_df = pd.DataFrame({
            "Trade": [f"#{i}" for i in range(len(equity))],
            "Equity (₹)": [round(float(v), 2) for v in equity],
        })
        peak = eq_df["Equity (₹)"].cummax()
        eq_df["Drawdown (%)"] = ((eq_df["Equity (₹)"] - peak) / peak * 100.0).round(2)
        eq_color = "#10b981" if eq_df["Equity (₹)"].iloc[-1] >= eq_df["Equity (₹)"].iloc[0] else "#f43f5e"

        ec1, ec2 = st.columns([1.6, 1])
        with ec1:
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:16px 0 2px;">'
                '📈 Equity Curve</p>',
                unsafe_allow_html=True,
            )
            st.altair_chart(
                _altair_area_chart(eq_df, "Trade", "Equity (₹)", "Equity (₹)", color=eq_color, height=250),
                use_container_width=True,
            )
        with ec2:
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:16px 0 2px;">'
                '📉 Drawdown Profile</p>',
                unsafe_allow_html=True,
            )
            st.altair_chart(
                _altair_area_chart(eq_df, "Trade", "Drawdown (%)", "Drawdown (%)", color="#f43f5e", height=250),
                use_container_width=True,
            )

    # ── Per-trade PnL ─────────────────────────────────────────────────────
    if trades:
        trades_df = pd.DataFrame(trades)

        if "net_pnl" in trades_df.columns:
            pnl_df = pd.DataFrame({
                "Trade": [f"#{i + 1}" for i in range(len(trades_df))],
                "Net P&L": trades_df["net_pnl"].astype(float).round(2),
            })
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:16px 0 2px;">'
                '💵 Per-Trade Net P&L</p>',
                unsafe_allow_html=True,
            )
            st.altair_chart(
                _altair_pnl_bar_chart(pnl_df, "Trade", "Net P&L", "Net P&L (₹)", height=210),
                use_container_width=True,
            )

        # ── Trade log (gradient tier ledger) ──────────────────────────────
        st.markdown(
            '<p style="font-weight:600;color:#94a3b8;margin:16px 0 2px;">'
            '📋 Trade Log</p>',
            unsafe_allow_html=True,
        )
        _tier_legend("tp2", "tp1", "profit", "flat", "loss", "sl")

        log_rows = []
        log_tiers = []
        for idx, tr in trades_df.reset_index(drop=True).iterrows():
            net_v = float(tr.get("net_pnl") or 0.0)
            reason = str(tr.get("reason") or "").upper()
            if "TP2" in reason:
                tier = "tp2"
            elif "TP1" in reason:
                tier = "tp1"
            elif "SL" in reason:
                tier = "sl"
            elif net_v > 0:
                tier = "profit"
            elif net_v < 0:
                tier = "loss"
            else:
                tier = "flat"
            log_tiers.append(tier)
            log_rows.append({
                "#": idx + 1,
                "Entry Time": str(tr.get("entry_time") or "")[:16].replace("T", " "),
                "Dir": str(tr.get("direction") or "BUY").upper(),
                "Entry": tr.get("entry_price"),
                "Exit Time": str(tr.get("exit_time") or "")[:16].replace("T", " "),
                "Exit": tr.get("exit_price"),
                "Qty": tr.get("qty"),
                "Gross": tr.get("gross_pnl"),
                "Costs": tr.get("costs"),
                "Net P&L": round(net_v, 2),
                "Exit Reason": str(tr.get("reason") or "—").replace("_", " ").title(),
            })

        _render_signal_table(
            pd.DataFrame(log_rows),
            log_tiers,
            price_cols=("Entry", "Exit"),
            pnl_cols=("Gross", "Costs", "Net P&L"),
            chip_cols={
                "Dir": lambda v: (
                    "buy" if "BUY" in str(v).upper() or "LONG" in str(v).upper() else "sell",
                    str(v),
                )
            },
            max_height=520,
        )

        if "events" in trades_df.columns and any(trades_df["events"]):
            with st.expander("🔍 Detailed Trade Event Progression", expanded=False):
                for idx, tr in trades_df.iterrows():
                    st.markdown(
                        f"**Trade #{idx + 1} ({tr.get('direction', 'TRADE')} "
                        f"@ ₹{float(tr['entry_price']):.2f}):**"
                    )
                    for ev in str(tr["events"]).split(" -> "):
                        st.caption(f"&nbsp;&nbsp;• {ev}")

        st.download_button(
            "📥 Download Trades CSV",
            trades_df.to_csv(index=False),
            file_name=f"{symbol}_{payload['strategy']}_trades.csv",
            mime="text/csv",
            key="bt_download_csv",
        )


def render_backtest():
    section_header(
        "📊 Strategy Backtest",
        "Simulate your strategy on historical OHLCV data · Instant local-cache replays · Results are estimates only"
    )

    st.warning(
        "⚠️ Results are **estimates only**. Backtests are not predictions of "
        "future performance. Always validate on a long, out-of-sample 5-minute history."
    )

    wl = load_watchlist()
    cached_syms = [s for s in wl if _load_local_symbol_df(s)[1]]
    saved_results = sorted(RESULTS_DIR.glob("*_trades.csv")) if RESULTS_DIR.exists() else []

    # ── Instant local-cache replay ────────────────────────────────────────
    insight_l, insight_r = st.columns([1.5, 1])
    with insight_l:
        st.markdown(
            '<div class="cc-panel" style="margin-bottom:10px;padding:14px 18px;">'
            '<div style="font-size:0.85rem;font-weight:600;color:#f1f5f9;margin-bottom:2px;">'
            '⚡ Instant Replay from Local Cache</div>'
            '<div style="font-size:0.78rem;color:#94a3b8;">'
            'Re-run the selected engine on candles already saved on disk — no network call, no rate limit.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if cached_syms:
            rp1, rp2, rp3 = st.columns([2, 1.3, 1])
            with rp1:
                cache_pick = st.selectbox(
                    "Cached symbol",
                    cached_syms,
                    key="bt_cache_pick",
                    label_visibility="collapsed",
                )
            with rp2:
                cache_capital = st.number_input(
                    "Capital (₹)",
                    value=DEFAULT_CAPITAL,
                    min_value=1000,
                    step=10000,
                    key="bt_cache_capital",
                    label_visibility="collapsed",
                )
            with rp3:
                run_cached = st.button(
                    "▶ Run cached",
                    type="primary",
                    use_container_width=True,
                    key="bt_run_cached",
                )
            if run_cached:
                df_cached, tf_label = _load_local_symbol_df(cache_pick)
                if df_cached.empty:
                    st.error(f"No usable cached candles for {cache_pick}.")
                else:
                    with st.spinner(f"Replaying {get_display_name(selected_strategy)} on {cache_pick}…"):
                        try:
                            kwargs = {}
                            if selected_strategy in INTRADAY_5M_STRATEGIES:
                                kwargs["mode"] = "INTRADAY_5M"
                                kwargs["direction_bias"] = "BULLISH"
                            res = run_backtest(
                                df_cached,
                                selected_strategy,
                                capital=float(cache_capital),
                                qty=int(DEFAULT_QTY),
                                symbol=cache_pick,
                                **kwargs,
                            )
                            st.session_state["bt_result"] = {
                                "metrics": res["metrics"],
                                "trades": res["trades"],
                                "equity_curve": res["equity_curve"],
                                "symbol": cache_pick,
                                "strategy": selected_strategy,
                                "source": "cache",
                                "candles": len(df_cached),
                                "tf": tf_label,
                            }
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Cached replay failed: {exc}")
        else:
            st.info(
                "No watchlist symbol has cached candles yet. Run the **🔔 Alerts** scanner "
                "or a live backtest once to populate `data/`."
            )
    with insight_r:
        st.markdown(
            '<div class="cc-panel" style="margin-bottom:10px;padding:14px 18px;">'
            '<div style="font-size:0.85rem;font-weight:600;color:#f1f5f9;margin-bottom:6px;">'
            '🗃️ Backtest Vault</div>'
            f'<div style="display:flex;justify-content:space-between;font-size:0.78rem;color:#94a3b8;margin-bottom:4px;">'
            f'<span>Cached datasets</span><span style="color:#38bdf8;font-weight:600;">{len(cached_syms)} symbols</span></div>'
            f'<div style="display:flex;justify-content:space-between;font-size:0.78rem;color:#94a3b8;margin-bottom:4px;">'
            f'<span>Saved result files</span><span style="color:#34d399;font-weight:600;">{len(saved_results)}</span></div>'
            f'<div style="display:flex;justify-content:space-between;font-size:0.78rem;color:#94a3b8;">'
            f'<span>Active engine</span><span style="color:#c084fc;font-weight:600;">{get_display_name(selected_strategy)}</span></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if saved_results:
            with st.expander(f"📁 Saved runs ({len(saved_results)})"):
                for rf in saved_results[-12:]:
                    st.caption(f"`{rf.name}`")

    # ── Live backtest form ────────────────────────────────────────────────
    with st.expander("🌐 Run a live backtest (fetches Upstox candles)", expanded=True):
        with st.form("backtest_form"):
            col1, col2 = st.columns(2)
            with col1:
                symbol_input = st.text_input(
                    "Symbol",
                    value=wl[0] if wl else "RELIANCE",
                    help="NSE equity symbol (e.g. RELIANCE, INFY, TCS)",
                    placeholder="e.g. RELIANCE",
                )
                manual_key_input = st.text_input(
                    "Manual Instrument Key (fallback)",
                    value="",
                    placeholder="e.g. NSE_EQ|INE002A01018 (optional)",
                    help="Used if the symbol is not in the instrument master or master download failed",
                )
                from_date = st.date_input("From", value=date.today() - timedelta(days=365))
                capital = st.number_input("Capital (₹)", value=DEFAULT_CAPITAL, min_value=1000, step=10000)
            with col2:
                to_date = st.date_input("To", value=date.today())
                qty = st.number_input("Qty per trade", value=DEFAULT_QTY, min_value=1, step=1)
                is_three_candle = selected_strategy in INTRADAY_5M_STRATEGIES
                if is_three_candle:
                    st.caption(
                        "Contract: 5-minute intraday · bullish only · "
                        "50% at 1:1.5 · flat by 14:55 IST"
                    )
            no_cache = st.checkbox("Skip cache (re-fetch data)")
            submitted = st.form_submit_button("▶️ Run Live Backtest", type="primary")
    if submitted:
        symbol = symbol_input.strip().upper()
        if not symbol:
            st.error("Enter a symbol.")
        elif ts == "missing":
            st.error("Set UPSTOX_ACCESS_TOKEN in .env first.")
        else:
            if manual_key_input.strip():
                resolver.set_manual_key(symbol, manual_key_input.strip())
            ikey = resolver.resolve(symbol)
            if not ikey:
                st.error(
                    f"Symbol '{symbol}' not found in NSE instrument master. "
                    "If the master failed to download or the symbol is missing, "
                    "enter a manual instrument key in the fallback field above."
                )
            else:
                with st.spinner(f"Fetching {symbol} data..."):
                    try:
                        df = fetch_historical(
                            ikey, symbol, unit, int(interval),
                            from_date, to_date, no_cache=no_cache,
                        )
                    except Exception as e:
                        err = str(e)
                        if UPSTOX_ACCESS_TOKEN:
                            err = err.replace(UPSTOX_ACCESS_TOKEN, "***")
                        st.error(f"Data fetch error: {err}")
                        df = pd.DataFrame()

                if df.empty:
                    st.warning("No candle data returned for the selected range.")
                else:
                    st.success(f"✅ Got {len(df)} candles for **{symbol}**.")

                    with st.spinner("Running backtest..."):
                        kwargs = {}
                        if is_three_candle:
                            kwargs["mode"] = "INTRADAY_5M"
                            kwargs["direction_bias"] = "BULLISH"
                        result = run_backtest(
                            df, selected_strategy,
                            capital=capital, qty=qty, symbol=symbol,
                            **kwargs,
                        )

                    st.session_state["bt_result"] = {
                        "metrics": result["metrics"],
                        "trades": result["trades"],
                        "equity_curve": result["equity_curve"],
                        "symbol": symbol,
                        "strategy": selected_strategy,
                        "source": "live",
                        "candles": len(df),
                    }
                    st.rerun()

    if st.session_state.get("bt_result"):
        _render_backtest_result(st.session_state["bt_result"])
    elif not cached_syms:
        st.markdown(
            '<div style="background:rgba(15,23,42,0.72);'
            'border:1px dashed rgba(16,185,129,0.28);border-radius:14px;'
            'padding:26px;text-align:center;margin-top:12px;">'
            '<div style="font-size:1.6rem;margin-bottom:6px;">📊</div>'
            '<div style="font-weight:600;color:#f1f5f9;'
            'font-family:Space Grotesk,sans-serif;">No backtest loaded</div>'
            '<div style="color:#94a3b8;font-size:0.86rem;margin-top:4px;">'
            'Run a cached replay above, or submit the live form to fetch fresh candles.</div></div>',
            unsafe_allow_html=True,
        )


# ══════════════════════════════════════════════════════════════════════════
# VIEW 3 — ALERTS & SETUP CHART
# ══════════════════════════════════════════════════════════════════════════
def _render_setup_chart(
    symbol: str,
    instrument_key: str,
    unit: str,
    interval: int,
    trade_info: dict,
    key_prefix: str = "",
):
    """
    Renders an interactive TradingView-style (Lightweight Charts) candlestick
    chart for the selected stock with:
    - Pan / zoom / draggable price & time axes (fully interactive)
    - Candlestick wicks and bodies (Green / Red) + volume histogram
    - Entry, Stop Loss, Target 1 & Target 2 dashed overlay lines
      (last-value labels shown on the price scale)
    - Signal-candle marker (BUY / SELL / ARMED)
    - On-chart drawing tools: trend line, ray, horizontal line, rectangle,
      long-position and short-position tools (entry → stop → target with
      shaded risk/reward boxes), eraser, undo and clear-all
    - Setup diagnostic cards & state-machine execution note
    """
    if not instrument_key:
        st.warning(f"No instrument key mapped for {symbol}. Map key in fallback field above.")
        return

    with st.spinner(f"Loading {symbol} candles for chart..."):
        try:
            now_d = date.today()
            if unit in ("days", "weeks", "months"):
                from_d = now_d - timedelta(days=90)
                df_raw = fetch_historical(instrument_key, symbol, unit, interval, from_d, now_d)
            else:
                from_d = now_d - timedelta(days=7)
                df_hist = fetch_historical(instrument_key, symbol, unit, interval, from_d, now_d - timedelta(days=1))
                df_intra = fetch_intraday(instrument_key, symbol, unit, interval)
                if not df_hist.empty and not df_intra.empty:
                    df_raw = pd.concat([df_hist, df_intra]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                elif not df_intra.empty:
                    df_raw = df_intra
                else:
                    df_raw = df_hist
        except Exception as e:
            st.error(f"Error loading candles for {symbol}: {e}")
            df_raw = pd.DataFrame()

    if df_raw.empty:
        st.info(f"No recent candle data available to plot for {symbol}.")
        return

    df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"])

    # ── Interactive Chart Controls ───────────────────────────────────────
    c_ctrl1, c_ctrl2 = st.columns([2, 1])
    with c_ctrl1:
        range_options = ["Today Only", "Last 2 Days", "Last 40 Candles", "Last 80 Candles", "All Loaded"]
        default_range_idx = 0 if unit in ("minutes", "hours") else 2
        selected_range = st.selectbox(
            "Candle Range",
            options=range_options,
            index=default_range_idx,
            key=f"{key_prefix}chart_range_{symbol}",
            help="Filter candles to remove off-market hours or focus strictly on today's session",
        )
    with c_ctrl2:
        with st.popover("⚙️ Chart settings", use_container_width=True):
            bar_spacing = st.slider(
                "Candle Spacing (X-axis)",
                min_value=3,
                max_value=30,
                value=8,
                step=1,
                key=f"{key_prefix}bar_spacing_{symbol}",
                help="Initial spacing between candles — you can also zoom with the mouse wheel / pinch",
            )
            chart_height = st.slider(
                "Chart Height",
                min_value=400,
                max_value=900,
                value=560,
                step=20,
                key=f"{key_prefix}chart_height_{symbol}",
                help="Chart height — drag the price scale (right side) to stretch or squash the Y-axis live",
            )
            margin_pct = st.slider(
                "Price Margin (Y-axis)",
                min_value=2,
                max_value=30,
                value=10,
                step=1,
                format="%d%%",
                key=f"{key_prefix}y_pad_{symbol}",
                help="Empty space above and below the auto-fitted price range",
            )

    # Filter candles based on chosen range
    if selected_range == "Today Only":
        latest_date = df_raw["timestamp"].dt.date.max()
        cdf = df_raw[df_raw["timestamp"].dt.date == latest_date].copy().reset_index(drop=True)
        if len(cdf) < 5:
            # Fallback if today's session just opened with very few candles
            cdf = df_raw.tail(40).copy().reset_index(drop=True)
    elif selected_range == "Last 2 Days":
        unique_dates = sorted(df_raw["timestamp"].dt.date.unique())
        last_two_dates = unique_dates[-2:] if len(unique_dates) >= 2 else unique_dates
        cdf = df_raw[df_raw["timestamp"].dt.date.isin(last_two_dates)].copy().reset_index(drop=True)
    elif selected_range == "Last 40 Candles":
        cdf = df_raw.tail(40).copy().reset_index(drop=True)
    elif selected_range == "Last 80 Candles":
        cdf = df_raw.tail(80).copy().reset_index(drop=True)
    else:  # All Loaded
        cdf = df_raw.copy().reset_index(drop=True)

    if cdf.empty:
        st.info(f"No candles available for {symbol} under the '{selected_range}' filter.")
        return

    # ── TradingView Lightweight Charts data prep ─────────────────────────
    # Intraday candles keep real timestamps (shifted so the axis renders IST
    # wall-clock time); daily+ candles use business-day strings.
    is_intraday = unit in ("minutes", "hours")
    cdf = cdf.sort_values("timestamp").reset_index(drop=True)

    if is_intraday:
        cdf["chart_time"] = [
            int(ts.timestamp() + (ts.utcoffset().total_seconds() if ts.utcoffset() else 0))
            for ts in cdf["timestamp"]
        ]
    else:
        cdf["chart_time"] = cdf["timestamp"].dt.strftime("%Y-%m-%d")

    cdf = cdf.drop_duplicates(subset=["chart_time"]).reset_index(drop=True)

    price_format = {"type": "price", "precision": 2, "minMove": 0.05}
    candles = [
        {
            "time": t,
            "open": round(float(o), 2),
            "high": round(float(h), 2),
            "low": round(float(l), 2),
            "close": round(float(c), 2),
        }
        for t, o, h, l, c in zip(
            cdf["chart_time"], cdf["open"], cdf["high"], cdf["low"], cdf["close"]
        )
    ]

    candle_options = {
        "upColor": "#10b981",
        "downColor": "#f43f5e",
        "borderVisible": False,
        "wickUpColor": "#10b981",
        "wickDownColor": "#f43f5e",
        "priceFormat": price_format,
        "priceLineVisible": True,
        "priceLineColor": "rgba(138,155,176,0.45)",
        "lastValueVisible": True,
    }

    # Trade Setup Levels
    entry_px = trade_info.get("entry_price") or trade_info.get("price")
    sl_px = trade_info.get("stop_loss")
    tp1_px = trade_info.get("tp1")
    tp2_px = trade_info.get("tp2")

    # Overlay trade setup levels as full-width dashed lines
    levels = [
        {"price": float(px), "color": color, "title": title}
        for px, color, title in (
            (entry_px, "#38bdf8", "Entry"),
            (sl_px, "#f43f5e", "SL"),
            (tp1_px, "#10b981", "TP1"),
            (tp2_px, "#fbbf24", "TP2"),
        )
        if px
    ]

    # Volume histogram pinned to the bottom (TradingView style)
    volume_data = []
    if "volume" in cdf.columns and float(cdf["volume"].fillna(0).sum()) > 0:
        volume_data = [
            {
                "time": t,
                "value": float(v),
                "color": "rgba(16,185,129,0.35)" if c >= o else "rgba(255,75,75,0.35)",
            }
            for t, v, o, c in zip(
                cdf["chart_time"], cdf["volume"].fillna(0), cdf["open"], cdf["close"]
            )
        ]

    # Marker on the signal candle
    markers = []
    side_str = str(trade_info.get("side") or "").upper()
    if side_str and candles:
        is_bull = ("BEAR" not in side_str) and ("BUY" in side_str or "BULL" in side_str)
        marker_time = candles[-1]["time"]
        ct = trade_info.get("candle_time")
        if ct is not None:
            try:
                ct = pd.to_datetime(ct)
                if is_intraday:
                    marker_time = int(ct.timestamp() + (ct.utcoffset().total_seconds() if ct.utcoffset() else 0))
                else:
                    marker_time = ct.strftime("%Y-%m-%d")
            except Exception:
                pass
        if marker_time in {d["time"] for d in candles}:
            markers = [{
                "time": marker_time,
                "position": "belowBar" if is_bull else "aboveBar",
                "color": "#10b981" if is_bull else "#f43f5e",
                "shape": "arrowUp" if is_bull else "arrowDown",
                "text": "ARMED" if "ARMED" in side_str else ("BUY" if is_bull else "SELL"),
            }]

    margin = min(max(margin_pct / 100.0, 0.05), 0.35)
    chart_options = {
        "height": int(chart_height),
        "layout": {
            "background": {"type": "solid", "color": "#070b14"},
            "textColor": "#94a3b8",
            "fontSize": 12,
            "fontFamily": "Inter, 'Space Grotesk', sans-serif",
        },
        "grid": {
            "vertLines": {"color": "rgba(255,255,255,0.04)"},
            "horzLines": {"color": "rgba(255,255,255,0.06)"},
        },
        "crosshair": {
            "mode": 0,
            "vertLine": {"color": "rgba(16,185,129,0.5)", "width": 1, "style": 2, "labelBackgroundColor": "#047857"},
            "horzLine": {"color": "rgba(56,189,248,0.5)", "width": 1, "style": 2, "labelBackgroundColor": "#0284c7"},
        },
        "rightPriceScale": {
            "borderColor": "rgba(255,255,255,0.15)",
            "scaleMargins": {"top": margin, "bottom": margin},
            "entireTextOnly": True,
        },
        "timeScale": {
            "borderColor": "rgba(255,255,255,0.15)",
            "barSpacing": int(bar_spacing),
            "rightOffset": 6,
            "timeVisible": is_intraday,
            "secondsVisible": False,
        },
        "handleScroll": {
            "mouseWheel": True,
            "pressedMouseMove": True,
            "horzTouchDrag": True,
            "vertTouchDrag": False,
        },
        "handleScale": {
            "axisPressedMouseMove": True,
            "mouseWheel": True,
            "pinch": True,
        },
    }

    render_drawing_chart(
        symbol=symbol,
        unit=unit,
        interval=interval,
        chart_height=int(chart_height),
        chart_options=chart_options,
        candle_options=candle_options,
        candles=candles,
        volume=volume_data,
        levels=levels,
        markers=markers,
    )
    st.caption("🖱️ Drag to pan · Scroll to zoom · **Esc** returns to the cursor.")
    with st.expander("✏️ Drawing tools & chart tips", expanded=False):
        st.caption(
            "**Tools:** Trend · Ray · H-Line · Rect · Long/Short position "
            "(click entry → stop → target) · Erase · Undo · Clear.\n\n"
            "Drawings are saved per symbol and timeframe. Drag the price or time "
            "scale to stretch an axis; double-click an axis to reset it."
        )

    # ── Setup Metric Cards ───────────────────────────────────────────────
    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    with mc1:
        sig_label = trade_info.get("side") or "None"
        sig_color = "green" if "BUY" in str(sig_label) else ("red" if "SELL" in str(sig_label) else "gold")
        metric_card("Signal / State", sig_label, color=sig_color, icon="⚡")
    with mc2:
        metric_card("Entry Level", f"₹{float(entry_px):.2f}" if entry_px else "—", color="blue", icon="🎯")
    with mc3:
        sl_delta = None
        if entry_px and sl_px:
            pts = abs(float(entry_px) - float(sl_px))
            pct = (pts / float(entry_px)) * 100
            sl_delta = f"-₹{pts:.2f} ({pct:.1f}%)"
        metric_card("Stop Loss", f"₹{float(sl_px):.2f}" if sl_px else "—", delta=sl_delta, color="red", icon="🛑")
    with mc4:
        tp_delta = None
        if entry_px and tp1_px:
            pts = abs(float(tp1_px) - float(entry_px))
            pct = (pts / float(entry_px)) * 100
            tp_delta = f"+₹{pts:.2f} ({pct:.1f}%)"
        metric_card("Target 1 (TP1)", f"₹{float(tp1_px):.2f}" if tp1_px else "—", delta=tp_delta, color="green", icon="🏆")
    with mc5:
        rr_val = trade_info.get("rr") or ("1:1.5" if tp1_px else "—")
        metric_card("Risk : Reward", rr_val, color="purple", icon="⚖️")

    # ── Strategy Execution & State Guidance ───────────────────────────────
    msg = trade_info.get("message")
    if msg:
        st.markdown(
            f'<div style="background: rgba(13, 21, 48, 0.7); border:1px solid rgba(16, 185, 129, 0.2); '
            f'border-radius:10px; padding:12px 16px; margin-top:12px;">'
            f'<div style="font-weight:600; color:#10b981; font-size:0.85rem; margin-bottom:4px;">📌 Setup Action Note:</div>'
            f'<div style="color:#f1f5f9; font-size:0.83rem;">{msg}</div>'
            f'</div>',
            unsafe_allow_html=True
        )


# ── Alerts helpers ────────────────────────────────────────────────────────
def _flash(kind: str, msg: str) -> None:
    """Queue a message that survives the next st.rerun()."""
    st.session_state["_alerts_flash"] = (kind, msg)


def _show_flash() -> None:
    flash = st.session_state.pop("_alerts_flash", None)
    if flash:
        kind, msg = flash
        getattr(st, kind, st.info)(msg)


def _flush_pending_toasts() -> None:
    for msg in st.session_state.pop("_pending_alert_toasts", []):
        st.toast(msg, icon="🔔")


# ── Chartink sync (UI) ────────────────────────────────────────────────────
_CHARTINK_STATE_KEY = "chartink_sync_result"


def _chartink_last_sync() -> None:
    """Caption showing when the watchlist was last pulled from Chartink."""
    try:
        history = json.loads(CHARTINK_SYNC_LOG_FILE.read_text(encoding="utf-8"))
        entry = history[-1]
        when = datetime.fromisoformat(entry["synced_at"]).strftime("%d %b · %H:%M")
        count = entry.get("count", len(entry.get("symbols") or []))
        st.caption(f"Last Chartink sync: **{when}** · {count} symbols")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return


def _render_chartink_panel() -> None:
    """Outcome of the last in-UI Chartink sync, with an undo."""
    state = st.session_state.get(_CHARTINK_STATE_KEY)
    if not state:
        _chartink_last_sync()
        return

    if state["status"] == "error":
        st.error(
            "Chartink sync failed — the watchlist was left untouched.\n\n"
            f"`{state['message']}`"
        )
        if st.button("Dismiss", key="chartink_dismiss_err"):
            st.session_state.pop(_CHARTINK_STATE_KEY, None)
            st.rerun()
        return

    if state["status"] == "blocked":
        st.warning(
            "Chartink sync skipped — the watchlist was left untouched. "
            f"{state.get('reason') or ''}"
        )
        if st.button("Dismiss", key="chartink_dismiss_blocked"):
            st.session_state.pop(_CHARTINK_STATE_KEY, None)
            st.rerun()
        return

    symbols = state["symbols"]
    previous = state["previous"]
    added = [sym for sym in symbols if sym not in previous]
    removed = [sym for sym in previous if sym not in symbols]
    carried = len(symbols) - len(added)
    when = state.get("at") or ""
    try:
        when = datetime.fromisoformat(when).strftime("%H:%M")
    except ValueError:
        when = ""

    st.success(
        f"Replaced the watchlist with **{len(symbols)}** stocks "
        "from the UB Intra Scan screener"
        + (f" at {when} IST." if when else ".")
    )
    bits = [
        f"**{len(added)}** new",
        f"**{len(removed)}** removed",
        f"**{carried}** carried over",
    ]
    if state.get("dropped"):
        bits.append(f"dropped: {', '.join(state['dropped'])}")
    st.caption(" · ".join(bits))

    if added:
        chips = "".join(f'<span class="wl-badge">{sym}</span>' for sym in added)
        st.markdown(f'<div style="margin:2px 0 4px;">{chips}</div>', unsafe_allow_html=True)
    if removed:
        st.caption("No longer in today's scan: " + ", ".join(removed))

    st.caption(
        "Press **🔍 Scan Now** below, or leave **Auto-refresh** on, to scan this list."
    )

    col_undo, col_dismiss, _ = st.columns([1, 1, 4])
    with col_undo:
        undo_clicked = st.button("↩️ Undo", key="chartink_undo", use_container_width=True)
    with col_dismiss:
        dismiss_clicked = st.button(
            "Dismiss", key="chartink_dismiss_ok", use_container_width=True
        )

    if undo_clicked:
        chartink_sync.write_watchlist_atomic(previous)
        st.session_state.pop(_CHARTINK_STATE_KEY, None)
        _flash("success", f"Watchlist restored to the previous {len(previous)} symbols.")
        st.rerun()
    if dismiss_clicked:
        st.session_state.pop(_CHARTINK_STATE_KEY, None)
        st.rerun()


@st.cache_data(show_spinner=False)
def _alert_beep_wav() -> bytes:
    """Generate a short two-tone WAV for new-alert notifications."""
    import io
    import math
    import struct
    import wave

    sample_rate = 22050
    tone_seconds = 0.11
    gap_seconds = 0.05
    frames = bytearray()
    for freq in (880.0, 1174.66):
        n = int(sample_rate * tone_seconds)
        fade = max(1, int(sample_rate * 0.012))
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            sample = int(32767 * 0.35 * env * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack("<h", sample)
        frames += b"\x00\x00" * int(sample_rate * gap_seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


def _maybe_play_alert_sound() -> None:
    """Play the beep once when a new alert was found (best effort)."""
    if not st.session_state.pop("_play_alert_sound", False):
        return
    if not st.session_state.get("alerts_sound", True):
        return
    # Keep the audio element in the DOM for autoplay, but hide its player UI.
    st.markdown(
        "<style>div[data-testid='stAudio']{height:0;overflow:hidden;margin:0;padding:0;}</style>",
        unsafe_allow_html=True,
    )
    st.audio(_alert_beep_wav(), format="audio/wav", autoplay=True)


def _friendly_status(status: str) -> str:
    return {
        "NEW": "🟢 NEW",
        "ARMED": "⚡ ARMED",
        "already_alerted": "🔵 Alerted",
        "MISSED": "🕘 Missed",
        "none": "⚪ No signal",
        "insufficient_data": "⚠️ Low data",
        "no_candles": "⚠️ No candles",
        "error": "❌ Error",
    }.get(status, status or "—")


def _status_priority(status: str) -> int:
    return {
        "NEW": 0,
        "ARMED": 1,
        "already_alerted": 2,
        "MISSED": 3,
        "none": 4,
        "insufficient_data": 5,
        "no_candles": 6,
        "error": 7,
    }.get(status, 8)


# ── Trade journal helpers ─────────────────────────────────────────────────
JOURNAL_PRESETS = ["Today", "This week", "Last 7 days", "Last 30 days", "All", "Custom"]
JOURNAL_TARGET_OUTCOMES = ("TP1_HIT", "TP2_HIT", "TP1_TRAIL")


def _journal_notional() -> float:
    return float(st.session_state.get("journal_notional", DEFAULT_NOTIONAL))


def _journal_preset_range(preset: str, custom_start=None, custom_end=None):
    """Return (start_date, end_date); start=None means 'all available'."""
    today = datetime.now(IST).date()
    if preset == "Today":
        return today, today
    if preset == "This week":
        return today - timedelta(days=today.weekday()), today
    if preset == "Last 7 days":
        return today - timedelta(days=6), today
    if preset == "Last 30 days":
        return today - timedelta(days=29), today
    if preset == "Custom":
        start = custom_start or (today - timedelta(days=6))
        end = custom_end or today
        if start > end:
            start, end = end, start
        return start, end
    return None, today


def _journal_weekdays(start, end) -> list[date]:
    if start is None or end is None:
        return []
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _journal_filter(entries, start=None, end=None, strategy=None) -> list[dict]:
    out = []
    for entry in entries:
        day = str(entry.get("date") or "")
        if start and day < start.isoformat():
            continue
        if end and day > end.isoformat():
            continue
        if strategy and strategy != "__all__" and entry.get("strategy") != strategy:
            continue
        out.append(entry)
    return out


def _journal_stats(entries: list[dict]) -> dict:
    """Headline stats for a set of journal entries."""
    return summary_stats(entries)


def _normalize_strategy_ids(strategy_ids) -> list[str]:
    """Keep registered strategies in registry order, defaulting to all of them."""
    known = [sid for sid, _ in list_strategies()]
    picked = [sid for sid in dict.fromkeys(strategy_ids or []) if sid in known]
    return picked or known


def _journal_progress_run_multi(
    dates: list[date],
    resolved: dict,
    strategy_ids: list[str],
    unit: str,
    interval: int,
    notional: float,
    key: str,
    title: str = "🧪 Building multi-strategy log",
):
    """Sync and settle several strategies over one candle fetch per symbol."""
    dates = [d for d in sorted(set(dates)) if d.weekday() < 5]
    strategies = _normalize_strategy_ids(strategy_ids)
    if not dates:
        st.warning("No trading days in the selected period.")
        return None
    if ts == "missing":
        st.error("Set UPSTOX_ACCESS_TOKEN in .env first.")
        return None

    keys = {s: k for s, k in resolved.items() if k}
    if not keys:
        st.warning("No resolved symbols to test.")
        return None

    start_label = dates[0].strftime("%d %b")
    end_label = dates[-1].strftime("%d %b")
    with st.status(
        f"{title} · {start_label} – {end_label} · {len(keys)} symbols × "
        f"{len(strategies)} strategies...",
        expanded=True,
    ) as status_box:
        progress = st.progress(0.0, text="Preparing candle requests...")
        log_container = st.container()

        def _on_progress(idx, total, symbol, message):
            progress.progress(
                min(idx / max(total, 1), 1.0),
                text=f"[{idx}/{total}] {message}",
            )
            with log_container:
                st.caption(message)

        summary = None
        try:
            summary = sync_journal_multi(
                keys,
                dates,
                strategies,
                unit,
                int(interval),
                notional=notional,
                progress=_on_progress,
            )
        except Exception as exc:
            status_box.update(label="❌ Multi-strategy sync failed", state="error", expanded=True)
            st.error(f"Multi-strategy sync failed: {exc}")

        if summary is None:
            return None

        progress.progress(1.0, text="✅ Multi-strategy sync complete.")
        status_box.update(
            label=(
                f"✅ Synced {len(dates)} day(s) × {len(strategies)} strategies · "
                f"{summary['added']} setup(s) added · {summary['settled']} settled"
            ),
            state="complete",
            expanded=False,
        )

    st.session_state["journal_last_sync"] = datetime.now(IST).isoformat()
    _flash(
        "success",
        f"Test window synced: {summary['added']} new setup(s) across "
        f"{len(strategies)} strategies · {summary['settled']} settled.",
    )
    if summary.get("errors"):
        _flash("warning", f"{summary['errors']} symbol(s) had no data or failed to load.")
    return summary


def _render_today_journal(resolved, strategy_ids: list[str], unit: str, interval: int):
    """Compact live session log shown on the Alerts page."""
    today = datetime.now(IST).date()
    all_journal = load_journal()
    entries = _journal_filter(all_journal, today, today)
    session_note = "today"
    if not entries and all_journal:
        # Fall back to the most recent session that actually has setups
        recent_days = sorted(
            {str(e.get("date")) for e in all_journal if e.get("date")},
            reverse=True,
        )[:6]
        for day_str in recent_days:
            try:
                day_d = date.fromisoformat(day_str)
            except ValueError:
                continue
            fallback = _journal_filter(all_journal, day_d, day_d)
            if fallback:
                entries = fallback
                session_note = f"last session · {day_d.strftime('%d %b')}"
                break
    stats = _journal_stats(entries)
    notional = _journal_notional()
    strategies = _normalize_strategy_ids(strategy_ids)

    st.divider()
    head_left, head_right = st.columns([3, 1])
    with head_left:
        st.markdown(
            '<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
            'font-size: 1.1rem; margin: 0;">📒 Session Journal</h3>'
            f'<span style="font-size:0.78rem;color:#94a3b8;">'
            f'{len(strategies)} tracked strateg'
            f'{"y" if len(strategies) == 1 else "ies"} · '
            f'showing {session_note} · every triggered setup is logged with its strategy · '
            'outcomes settle after the session</span>',
            unsafe_allow_html=True,
        )
    with head_right:
        sync_today = st.button(
            "🔄 Sync today",
            key="alerts_sync_today_journal",
            use_container_width=True,
            help="Rebuild every setup from today's candles and settle TP/SL/EOD outcomes.",
        )

    if sync_today:
        summary = _journal_progress_run_multi(
            [today],
            resolved,
            strategies,
            unit,
            int(interval),
            notional,
            "alerts",
            title="📒 Syncing today",
        )
        if summary is not None:
            st.rerun()

    j1, j2, j3, j4, j5 = st.columns(5)
    with j1:
        metric_card("Setups", str(stats["count"]), color="cyan", icon="📒")
    with j2:
        metric_card("TP Hit", str(stats["targets"]), color="green", icon="🎯")
    with j3:
        metric_card("SL Hit", str(stats["stops"]), color="red", icon="🛑")
    with j4:
        metric_card("Open", str(stats["open"]), color="gold", icon="⏳")
    with j5:
        pnl_color = "green" if stats["net"] >= 0 else "red"
        metric_card(
            "Net P&L*",
            f"₹{stats['net']:,.0f}",
            color=pnl_color,
            icon="💰",
        )

    ordered_entries = sorted(
        entries, key=lambda e: str(e.get("entry_time") or ""), reverse=True
    )

    # Resolve a setup previously opened from the View button (survives reruns).
    focus_id = st.session_state.get("journal_chart_entry_id")
    focus_entry = None
    if focus_id:
        focus_entry = next(
            (
                e
                for e in ordered_entries
                if (e.get("id") or journal_entry_id(e)) == focus_id
            ),
            None,
        )
        if focus_entry is None:
            st.session_state.pop("journal_chart_entry_id", None)

    if entries:
        rows = []
        tiers = []
        for entry in ordered_entries:
            pnl = entry.get("net_pnl")
            tiers.append(
                _outcome_tier(
                    entry.get("outcome"),
                    bool(entry.get("settled", True)),
                    pnl,
                    entry.get("exit_reason", ""),
                )
            )
            rows.append({
                "Time": str(entry.get("entry_time") or "")[11:16],
                "Symbol": entry.get("symbol"),
                "Strategy": entry.get("strategy_name")
                or get_display_name(entry.get("strategy", "")),
                "Signal": entry.get("side"),
                "Entry": entry.get("entry_price"),
                "SL": entry.get("stop_loss"),
                "TP1": entry.get("tp1"),
                "Outcome": entry.get("outcome_label") or "Awaiting result",
                "P&L": pnl,
                "EOD Zone": entry.get("eod_zone") or "—",
            })

        st.markdown(
            '<div style="display:flex;align-items:center;justify-content:space-between;'
            'margin:14px 0 2px;">'
            '<span style="font-size:0.78rem;color:#94a3b8;text-transform:uppercase;'
            'letter-spacing:0.08em;font-weight:600;">Outcome tiers</span></div>',
            unsafe_allow_html=True,
        )
        _tier_legend("tp2", "tp1", "trail", "profit", "flat", "loss", "sl", "open")

        _render_signal_table(
            pd.DataFrame(rows),
            tiers,
            price_cols=("Entry", "SL", "TP1"),
            pnl_cols=("P&L",),
            chip_cols={
                "Signal": _side_chip,
                "Outcome": _outcome_chip,
                "EOD Zone": _zone_chip,
            },
            symbol_col="Symbol",
            max_height=340,
        )

        open_labels = [
            f"{str(e.get('entry_time') or '')[11:16]} · {e.get('symbol')} · "
            f"{e.get('outcome_label') or 'Awaiting result'}"
            for e in ordered_entries
        ]
        pick_col, btn_col = st.columns([3.4, 1])
        with pick_col:
            picked_idx = st.selectbox(
                "Open a setup on the chart",
                options=list(range(len(open_labels))),
                format_func=lambda i: open_labels[i],
                key="alerts_journal_open_pick",
                label_visibility="collapsed",
            )
        with btn_col:
            if st.button(
                "📈 View setup chart",
                key="alerts_journal_view_btn",
                use_container_width=True,
            ):
                target = ordered_entries[picked_idx]
                st.session_state["journal_chart_entry_id"] = (
                    target.get("id") or journal_entry_id(target)
                )
                st.session_state["_scroll_journal_chart"] = True
                st.rerun()

        st.caption(
            f"* Hypothetical P&L on ₹{notional:,.0f} notional per setup, after estimated costs. "
            "Pick a setup above to open it on the chart. "
            "Click **Sync today** after 15:30 IST for final outcomes."
        )
    else:
        st.info(
            "No setups logged today yet. New scan alerts are added automatically — "
            "use **Sync today** to backfill every setup from the full session."
        )

    # ── Highlighted setup chart (opened from the View column) ─────────────
    if focus_entry is not None:
        st.markdown(
            '<div id="journal-setup-chart"></div>', unsafe_allow_html=True
        )
        if st.session_state.pop("_scroll_journal_chart", False):
            components.html(
                """
                <script>
                  const el = window.parent.document.getElementById('journal-setup-chart');
                  if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
                </script>
                """,
                height=0,
            )

        focus_symbol = str(focus_entry.get("symbol") or "").upper()
        focus_side = str(focus_entry.get("side") or "").upper()
        focus_time = str(focus_entry.get("entry_time") or "")
        focus_when = focus_time.replace("T", " ")[:16] if focus_time else "—"
        focus_entry_px = focus_entry.get("entry_price")
        focus_px_text = (
            f"₹{float(focus_entry_px):,.2f}"
            if focus_entry_px is not None
            else "—"
        )

        chart_head, chart_close = st.columns([3, 1])
        with chart_head:
            st.markdown(
                f'<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
                f'font-size: 1.15rem; margin: 16px 0 4px;">'
                f'📈 Setup Chart &amp; Structure Levels: '
                f'<span style="color:#10b981;">{focus_symbol}</span></h3>'
                f'<span style="font-size:0.78rem;color:#94a3b8;">'
                f'Journal setup · {focus_when} · {focus_side} @ {focus_px_text}</span>',
                unsafe_allow_html=True,
            )
        with chart_close:
            st.write("")
            if st.button(
                "✖ Close chart",
                key="alerts_close_journal_chart",
                use_container_width=True,
            ):
                st.session_state.pop("journal_chart_entry_id", None)
                st.rerun()

        outcome_bits = [
            f"**Outcome:** {focus_entry.get('outcome_label') or 'Awaiting result'}",
            f"**EOD zone:** {focus_entry.get('eod_zone') or '—'}",
        ]
        if focus_entry.get("net_pnl") is not None:
            outcome_bits.append(
                f"**Net P&L\\*:** ₹{float(focus_entry['net_pnl']):,.2f} "
                f"on ₹{notional:,.0f} notional"
            )
        st.caption(" · ".join(outcome_bits))

        focus_note = (
            f"Journal setup: {focus_side} at {focus_px_text} on {focus_when}. "
            f"Outcome: {focus_entry.get('outcome_label') or 'Awaiting result'}."
        )
        if focus_entry.get("exit_reason"):
            focus_note += f" Exit: {focus_entry.get('exit_reason')}."
        if focus_entry.get("eod_zone"):
            focus_note += f" End-of-day zone: {focus_entry.get('eod_zone')}."

        focus_key = resolved.get(focus_symbol) or resolver.resolve(focus_symbol)
        _render_setup_chart(
            symbol=focus_symbol,
            instrument_key=focus_key,
            unit=focus_entry.get("unit") or unit,
            interval=int(focus_entry.get("interval") or interval),
            trade_info={
                "symbol": focus_symbol,
                "side": focus_side,
                "entry_price": focus_entry_px,
                "stop_loss": focus_entry.get("stop_loss"),
                "tp1": focus_entry.get("tp1"),
                "tp2": focus_entry.get("tp2"),
                "rr": focus_entry.get("rr"),
                "candle_time": focus_entry.get("entry_time"),
                "price": focus_entry.get("eod_close")
                or focus_entry.get("exit_price")
                or focus_entry_px,
                "message": focus_note,
            },
            key_prefix="journal_",
        )


def render_alerts():
    section_header(
        "🔔 Strategy Alerts",
        "Live watchlist scanner · Auto-refresh · Signal detection · Alert history"
    )

    _show_flash()
    _flush_pending_toasts()
    _maybe_play_alert_sound()

    # ── Compact safety note ──────────────────────────────────────────────
    st.markdown(
        '<div style="display:inline-flex;align-items:center;gap:8px;'
        'background:rgba(255,75,75,0.08);border:1px solid rgba(255,75,75,0.3);'
        'border-radius:999px;padding:6px 14px;margin-bottom:12px;'
        'color:#f43f5e;font-size:0.82rem;font-weight:600;">'
        '🚫 Alert only — place trades manually. No auto-order placement.'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Watchlist ────────────────────────────────────────────────────────
    current_wl = load_watchlist()
    resolved = resolver.resolve_many(current_wl)
    unresolved = [s for s, k in resolved.items() if not k]

    st.markdown(
        f'<div style="display:flex;align-items:baseline;justify-content:space-between;'
        f'margin-bottom:8px;">'
        f'<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;'
        f'font-size:1.1rem;margin:0;">📋 Watchlist</h3>'
        f'<span style="font-size:0.78rem;color:#94a3b8;">'
        f'{len(current_wl)}/{MAX_WATCHLIST} symbols</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if st.session_state.pop("_clear_add_input", False):
        st.session_state["add_sym"] = ""

    col_add, col_add_btn, col_sync_btn = st.columns([3, 1, 1])
    with col_add:
        new_sym = st.text_input(
            "Add symbols",
            key="add_sym",
            placeholder="e.g. HDFC, WIPRO (comma or newline separated)",
            label_visibility="collapsed",
        )
    with col_add_btn:
        add_clicked = st.button("➕ Add", use_container_width=True)
    with col_sync_btn:
        chartink_clicked = st.button(
            "🔄 Chartink",
            key="chartink_sync_btn",
            use_container_width=True,
            help=(
                "Replace this watchlist with today's UB Intra Scan results from "
                "chartink.com — no browser, no login, yesterday's list removed."
            ),
        )

    if add_clicked:
        raw_tokens = new_sym.replace(",", "\n").splitlines()
        candidates = []
        for token in raw_tokens:
            symbol = token.strip().upper()
            if symbol and symbol not in current_wl and symbol not in candidates:
                candidates.append(symbol)

        if not candidates:
            st.warning("Enter at least one new symbol.")
        else:
            free_slots = max(0, MAX_WATCHLIST - len(current_wl))
            added = candidates[:free_slots]
            skipped = candidates[free_slots:]
            if not added:
                st.warning(f"Watchlist is full ({MAX_WATCHLIST} symbols). Remove a symbol first.")
            else:
                save_watchlist(current_wl + added)
                notice = f"Added {', '.join(added)}."
                if skipped:
                    notice += (
                        f" Skipped {len(skipped)} symbol(s) — "
                        f"watchlist limit is {MAX_WATCHLIST}."
                    )
                st.session_state["_clear_add_input"] = True
                _flash("success", notice)
                st.rerun()

    # ── Chartink sync (one click, no browser, no login) ──────────────────
    if chartink_clicked:
        with st.spinner("Running the UB Intra Scan screener on Chartink…"):
            try:
                chartink_summary = chartink_sync.sync(resolver=resolver)
            except ChartinkError as exc:
                st.session_state[_CHARTINK_STATE_KEY] = {
                    "status": "error",
                    "message": str(exc),
                }
            except Exception as exc:  # network, DNS, timeout…
                st.session_state[_CHARTINK_STATE_KEY] = {
                    "status": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            else:
                st.session_state[_CHARTINK_STATE_KEY] = {
                    "status": "ok" if chartink_summary["ok"] else "blocked",
                    "at": chartink_summary["checked_at"],
                    "symbols": chartink_summary["symbols"],
                    "previous": chartink_summary["previous"],
                    "dropped": chartink_summary["dropped"],
                    "reason": chartink_summary["reason"],
                }
        st.rerun()

    _render_chartink_panel()

    # ── Show resolved status as badges ───────────────────────────────────
    if current_wl:
        badges_html = ""
        for sym, ikey in resolved.items():
            badge_class = "" if ikey else " unresolved"
            icon = "✓" if ikey else "✗"
            badges_html += f'<span class="wl-badge{badge_class}">{icon} {sym}</span>'
        st.markdown(
            f'<div style="margin:8px 0 6px;">{badges_html}</div>',
            unsafe_allow_html=True,
        )
        if unresolved:
            st.caption(
                f"⚠️ Could not resolve: {', '.join(unresolved)}. "
                "Use the manual instrument key fallback below."
            )

        with st.expander("✏️ Edit watchlist", expanded=False):
            remove_syms = st.multiselect(
                "Remove symbols",
                options=current_wl,
                key="rm_syms",
                wrap=True,
            )
            c_remove, c_clear = st.columns(2)
            with c_remove:
                if st.button(
                    "➖ Remove selected",
                    disabled=not remove_syms,
                    use_container_width=True,
                ):
                    save_watchlist([s for s in current_wl if s not in remove_syms])
                    _flash("success", f"Removed {', '.join(remove_syms)}.")
                    st.rerun()
            with c_clear:
                confirm_clear = st.checkbox("Confirm clear all", key="confirm_clear_wl")
                if st.button(
                    "🗑️ Clear watchlist",
                    disabled=not confirm_clear,
                    use_container_width=True,
                ):
                    save_watchlist([])
                    _flash("success", "Watchlist cleared.")
                    st.rerun()
    else:
        st.info("Watchlist is empty. Add a symbol above to start scanning.")

    if (unresolved or not resolver.is_loaded) and current_wl:
        with st.expander("🛠️ Manual Instrument Key Fallback", expanded=not resolver.is_loaded):
            st.caption(
                "If the instrument master cannot be downloaded or a symbol isn't listed, "
                "enter its instrument key manually (e.g. NSE_EQ|INE002A01018):"
            )
            col_m1, col_m2, col_m3 = st.columns([2, 3, 1])
            with col_m1:
                manual_sym_choice = st.selectbox(
                    "Symbol",
                    unresolved if unresolved else current_wl,
                    key="manual_sym_pick",
                )
            with col_m2:
                manual_key_val = st.text_input(
                    "Instrument Key",
                    placeholder="e.g. NSE_EQ|INE002A01018",
                    key="manual_key_val_input",
                )
            with col_m3:
                st.write("")
                if st.button("Set Key", key="btn_set_manual_key"):
                    if manual_key_val.strip():
                        resolver.set_manual_key(manual_sym_choice, manual_key_val.strip())
                        _flash(
                            "success",
                            f"Mapped {manual_sym_choice} → {manual_key_val.strip()}",
                        )
                        st.rerun()

    st.divider()

    # ── Scan controls ────────────────────────────────────────────────────
    st.markdown('<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; font-size: 1.1rem; margin-bottom: 12px;">🔍 Scanner Controls</h3>', unsafe_allow_html=True)

    col_s1, col_s2, col_s3 = st.columns(
        [2, 1.5, 1.5], vertical_alignment="center"
    )
    with col_s1:
        auto_refresh = st.toggle("Auto-refresh", value=True, key="alerts_auto_refresh")
    with col_s2:
        st.toggle("Sound alert", value=True, key="alerts_sound")
    with col_s3:
        scan_now = st.button(
            "🔍 Scan Now",
            type="primary",
            use_container_width=True,
        )

    refresh_minutes = st.session_state.get("alerts_refresh_minutes", 5)
    override_hours = st.session_state.get("alerts_override_hours", False)
    if auto_refresh:
        col_s4, col_s5 = st.columns(2)
        with col_s4:
            refresh_minutes = st.number_input(
                "Auto-refresh interval (min)",
                min_value=1,
                max_value=60,
                value=int(refresh_minutes),
                key="alerts_refresh_minutes",
            )
        with col_s5:
            override_hours = st.checkbox(
                "Run outside market hours",
                value=bool(override_hours),
                key="alerts_override_hours",
            )

    # ── Market hours indicator ───────────────────────────────────────────
    now_ist = datetime.now(IST)
    market_open = is_market_open(now_ist)
    if market_open:
        st.markdown(f"""
        <div class="market-status-open">
            <span class="pulse-dot green"></span>
            Market Open &nbsp;·&nbsp; {now_ist.strftime('%H:%M IST')}
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
        <div class="market-status-closed">
            <span class="pulse-dot red"></span>
            Market Closed &nbsp;·&nbsp; {now_ist.strftime('%H:%M IST')}
        </div>
        """, unsafe_allow_html=True)

    st.caption(
        f"Scope: **{len(resolved)}** symbols · "
        f"**{len(tracked_strategies)} strateg{'y' if len(tracked_strategies) == 1 else 'ies'}** · "
        f"**{int(interval)} {unit}** · Alert-only scan of the latest closed candle."
    )
    st.caption(
        "Tracks: "
        + ", ".join(get_display_name(sid) for sid in tracked_strategies)
        + ". Every triggered setup is logged to the Trade Journal with its strategy."
    )
    st.caption(
        "⚠️ Auto-refresh requires this Alerts page to stay open. "
        "A sleeping laptop stops scans."
    )

    # ── Scan function ────────────────────────────────────────────────────
    def _run_scan(scan_time=None):
        """Execute one scan cycle with live progress and real-time logging."""
        scan_time = scan_time or datetime.now(IST)

        if ts == "missing":
            st.error("Set UPSTOX_ACCESS_TOKEN in .env first.")
            return

        if not resolved:
            st.warning("Watchlist is empty. Add symbols to scan.")
            return

        scan_strategies = _normalize_strategy_ids(tracked_strategies)
        total_syms = len(resolved)
        strategy_label = (
            get_display_name(scan_strategies[0])
            if len(scan_strategies) == 1
            else f"{len(scan_strategies)} strategies"
        )

        start_t = time.time()
        with st.status(
            f"🔍 Scanning {total_syms} stocks · {strategy_label}...",
            expanded=True,
        ) as status_box:
            progress_bar = st.progress(0.0, text=f"Initializing scan for {total_syms} symbols...")
            log_container = st.container()

            def _on_scan_progress(idx, total, sym, msg, res):
                pct = min(float(idx) / max(float(total), 1.0), 1.0)
                t_str = datetime.now(IST).strftime("%H:%M:%S")
                progress_bar.progress(pct, text=f"[{idx}/{total}] Scanning {sym}...")
                with log_container:
                    st.write(f"⏱️ `{t_str}` &nbsp; **[{idx}/{total}]** &nbsp; {msg}")

            results_by_strategy = scan_watchlist_multi(
                symbols=resolved,
                strategy_ids=scan_strategies,
                unit=unit,
                interval=int(interval),
                fetch_historical_fn=fetch_historical,
                fetch_intraday_fn=fetch_intraday,
                now=scan_time,
                progress_callback=_on_scan_progress,
                progress_strategy=scan_strategies[0],
            )
            # Display every strategy's evaluation; the journal keeps them separate.
            results = [
                item
                for sid in scan_strategies
                for item in results_by_strategy.get(sid, [])
            ]

            elapsed = round(time.time() - start_t, 2)
            alerts_count = sum(1 for r in results if r.get("status") in ("NEW", "already_alerted", "ARMED"))
            progress_bar.progress(1.0, text=f"✅ Done! Evaluated {len(results)} symbols in {elapsed}s.")
            status_box.update(
                label=(
                    f"✅ Scan complete! {len(results)} symbols × "
                    f"{len(scan_strategies)} strateg"
                    f"{'y' if len(scan_strategies) == 1 else 'ies'} in {elapsed}s · "
                    f"{alerts_count} setups active"
                ),
                state="complete",
                expanded=False,
            )

        st.session_state.scan_results = results
        st.session_state.last_scan_time = scan_time

        # Persist NEW alerts once and queue their toasts/sound.
        history = st.session_state.alert_history
        existing_keys = {
            (
                h.get("symbol"),
                h.get("strategy"),
                h.get("side"),
                str(h.get("candle_time")),
            )
            for h in history
        }
        added_alerts = 0
        for r in results:
            if r.get("status") != "NEW":
                continue
            key = (
                r.get("symbol"),
                r.get("strategy"),
                r.get("side"),
                str(r.get("candle_time")),
            )
            if key in existing_keys:
                continue
            history.append({"time": scan_time.isoformat(), **r})
            existing_keys.add(key)
            added_alerts += 1
            pending = st.session_state.get("_pending_alert_toasts", [])
            pending.append(r["message"])
            st.session_state["_pending_alert_toasts"] = pending

        if added_alerts:
            st.session_state.alert_history = history[-ALERT_HISTORY_LIMIT:]
            save_alert_history(st.session_state.alert_history)
            st.session_state["_play_alert_sound"] = True

        # Also persist every triggered setup to the daily trade journal, keyed by
        # strategy so each engine's setups are tracked separately.
        try:
            journal_payload: dict[str, list[dict]] = {
                sid: results_by_strategy.get(sid, [])
                for sid in scan_strategies
                if results_by_strategy.get(sid)
            }
            journal_added, journal_updated = record_multi_scan_alerts(
                journal_payload,
                scan_time,
                unit,
                int(interval),
                notional=_journal_notional(),
            )
            if journal_added or journal_updated:
                st.session_state["journal_last_auto_log"] = datetime.now(IST).isoformat()
        except Exception:
            # Journal persistence must never break a live scan.
            pass

        # Auto-select the first symbol that has a setup or signal
        signaled = [
            r["symbol"]
            for r in results
            if r.get("side") or r.get("status") in ("NEW", "already_alerted", "ARMED")
        ]
        if signaled:
            st.session_state.selected_alert_symbol = signaled[0]
        elif results and not st.session_state.selected_alert_symbol:
            st.session_state.selected_alert_symbol = results[0]["symbol"]

    # ── Manual scan button ───────────────────────────────────────────────
    if scan_now:
        _run_scan()
        _flush_pending_toasts()
        _maybe_play_alert_sound()

    # ── Auto-refresh via st.fragment ─────────────────────────────────────
    if auto_refresh:
        if market_open or override_hours:
            @st.fragment(run_every=timedelta(minutes=int(refresh_minutes)))
            def _auto_scan_fragment():
                scan_time = datetime.now(IST)
                interval = timedelta(minutes=int(refresh_minutes))
                last_scan = st.session_state.get("last_scan_time")

                # Fragments also run on every full app rerun. Only scan when the
                # configured interval has actually elapsed.
                if last_scan and (scan_time - last_scan) < interval:
                    next_check = last_scan + interval
                    st.caption(
                        "⏳ Auto-scan active · next check ~"
                        f"{next_check.strftime('%H:%M:%S IST')}"
                    )
                    return

                if not override_hours and not is_market_open(scan_time):
                    st.caption("Auto-scan paused: market closed.")
                    return

                _run_scan(scan_time)
                # Refresh the results table/chart outside the fragment too.
                st.rerun()

            _auto_scan_fragment()
        else:
            st.info(
                "Auto-refresh is enabled but paused because the market is closed. "
                "Enable **Run outside market hours** above to scan anyway."
            )

    # ── Display Scan Results Table & Chart ───────────────────────────────
    if st.session_state.scan_results:
        results = st.session_state.scan_results
        counts = {}
        for r in results:
            status = r.get("status", "none")
            counts[status] = counts.get(status, 0) + 1

        issue_count = sum(counts.get(s, 0) for s in ("error", "insufficient_data", "no_candles"))

        st.markdown(
            '<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
            'font-size: 1.1rem; margin: 18px 0 10px;">📊 Scan Results</h3>',
            unsafe_allow_html=True,
        )
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        symbol_count = len({r.get("symbol") for r in results if r.get("symbol")})
        with k1:
            metric_card("Stocks", str(symbol_count), color="cyan", icon="🔍")
        with k2:
            metric_card("New", str(counts.get("NEW", 0)), color="green", icon="🔔")
        with k3:
            metric_card("Armed", str(counts.get("ARMED", 0)), color="gold", icon="⚡")
        with k4:
            metric_card("No Signal", str(counts.get("none", 0)), color="blue", icon="⚪")
        with k5:
            metric_card("Missed", str(counts.get("MISSED", 0)), color="purple", icon="🕘")
        with k6:
            metric_card(
                "Issues",
                str(issue_count),
                color="red" if issue_count else "gold",
                icon="⚠️",
            )

        # ── Compact new-signal cards ─────────────────────────────────────
        new_alerts = [r for r in results if r.get("status") == "NEW"]
        if new_alerts:
            st.markdown(
                '<p style="font-size:0.78rem;color:#94a3b8;text-transform:uppercase;'
                'letter-spacing:0.08em;margin:14px 0 4px;">New signals</p>',
                unsafe_allow_html=True,
            )
            for idx, na in enumerate(new_alerts[:5]):
                side = str(na.get("side") or "—")
                side_color = "#10b981" if "BUY" in side.upper() else "#f43f5e"
                c_info, c_action = st.columns([5, 1])
                with c_info:
                    st.markdown(
                        f'<div style="background:rgba(16,185,129,0.06);'
                        f'border:1px solid rgba(16,185,129,0.25);border-radius:10px;'
                        f'padding:10px 14px;margin:4px 0;">'
                        f'<span style="font-weight:700;color:#10b981;'
                        f'font-family:Space Grotesk,sans-serif;">{na["symbol"]}</span>'
                        f' &nbsp;<span style="color:{side_color};font-weight:700;">{side}</span>'
                        f' &nbsp;<span style="color:#94a3b8;font-size:0.78rem;">'
                        f'{get_display_name(na.get("strategy") or "")}</span>'
                        f'<div style="color:#94a3b8;font-size:0.82rem;margin-top:2px;">'
                        f'{na.get("message", "")}</div></div>',
                        unsafe_allow_html=True,
                    )
                with c_action:
                    st.write("")
                    if st.button(
                        "View chart",
                        key=f"view_new_{idx}_{na['symbol']}",
                        use_container_width=True,
                    ):
                        st.session_state.selected_alert_symbol = na["symbol"]
                        st.rerun()
            if len(new_alerts) > 5:
                st.caption(f"+{len(new_alerts) - 5} more new signals in the table below.")

        # ── Filters ───────────────────────────────────────────────────────
        filter_col1, filter_col2 = st.columns([2, 3])
        with filter_col1:
            status_filter = st.selectbox(
                "Show",
                ["All", "Actionable", "NEW", "ARMED", "MISSED", "No signal", "Issues"],
                key="alerts_status_filter",
            )
        with filter_col2:
            search_query = st.text_input(
                "Search symbol",
                placeholder="e.g. RELIANCE",
                key="alerts_search",
            ).strip().upper()

        def _matches_filter(r):
            status = r.get("status", "none")
            if status_filter == "Actionable" and status not in ("NEW", "ARMED", "already_alerted"):
                return False
            if status_filter == "NEW" and status != "NEW":
                return False
            if status_filter == "ARMED" and status != "ARMED":
                return False
            if status_filter == "MISSED" and status != "MISSED":
                return False
            if status_filter == "No signal" and status != "none":
                return False
            if status_filter == "Issues" and status not in ("error", "insufficient_data", "no_candles"):
                return False
            if search_query and search_query not in str(r.get("symbol", "")).upper():
                return False
            return True

        filtered_results = [r for r in results if _matches_filter(r)]
        filtered_results.sort(
            key=lambda r: (
                _status_priority(r.get("status", "none")),
                str(r.get("symbol", "")),
            )
        )

        st.markdown(
            '<div style="display:flex;align-items:center;justify-content:space-between;'
            'margin:12px 0 6px;">'
            '<span style="font-size:0.82rem;color:#34d399;font-weight:600;">'
            '👉 Backup scan ledger — graded by status. Open any symbol chart with the picker below.</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        if filtered_results:
            _tier_legend("new", "armed", "profit", "missed", "none")

            rows = []
            tiers = []
            for r in filtered_results:
                candle_time = r.get("candle_time")
                tiers.append(_scan_status_tier(r.get("status", "none")))
                rows.append({
                    "Symbol": r["symbol"],
                    "Strategy": get_display_name(r.get("strategy") or ""),
                    "Signal": r.get("side") or "—",
                    "Entry": r.get("entry_price"),
                    "Stop Loss": r.get("stop_loss"),
                    "TP1": r.get("tp1"),
                    "TP2": r.get("tp2"),
                    "R:R": r.get("rr") or "—",
                    "Current": r.get("price"),
                    "Candle Time": str(candle_time)[:19] if candle_time else "—",
                    "Status": _friendly_status(r.get("status", "none")),
                })

            _render_signal_table(
                pd.DataFrame(rows),
                tiers,
                price_cols=("Entry", "Stop Loss", "TP1", "TP2", "Current"),
                chip_cols={
                    "Signal": _side_chip,
                    "Status": _status_chip,
                },
                symbol_col="Symbol",
                max_height=430,
            )
        else:
            st.info("No results match the selected filter.")

        # ── Export + timing ───────────────────────────────────────────────
        export_items = filtered_results or results
        export_df = pd.DataFrame([
            {
                "Symbol": r.get("symbol"),
                "Signal": r.get("side"),
                "Entry": r.get("entry_price"),
                "Stop Loss": r.get("stop_loss"),
                "TP1": r.get("tp1"),
                "TP2": r.get("tp2"),
                "R:R": r.get("rr"),
                "Current": r.get("price"),
                "Candle Time": r.get("candle_time"),
                "Status": r.get("status"),
                "Strategy": get_display_name(r.get("strategy", selected_strategy)),
            }
            for r in export_items
        ])
        export_col, info_col = st.columns([1, 3])
        with export_col:
            st.download_button(
                "⬇️ Export results CSV",
                export_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"alerts_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                key="alerts_results_csv",
            )
        with info_col:
            scan_time_dt = st.session_state.last_scan_time
            timing_bits = []
            if scan_time_dt:
                timing_bits.append(
                    f"Last scanned: **{scan_time_dt.strftime('%Y-%m-%d %H:%M:%S IST')}**"
                )
            if auto_refresh and (market_open or override_hours) and scan_time_dt:
                next_scan = scan_time_dt + timedelta(minutes=int(refresh_minutes))
                timing_bits.append(f"Next: **{next_scan.strftime('%H:%M:%S IST')}**")
            elif auto_refresh:
                timing_bits.append("Auto-refresh paused (market closed)")
            if timing_bits:
                st.caption(" · ".join(timing_bits))

        if scan_time_dt:
            age = datetime.now(IST) - scan_time_dt
            if age > timedelta(minutes=max(int(refresh_minutes) * 2, 30)):
                st.warning(
                    "These results are getting old. Run a fresh scan for current prices."
                )

        # ── Interactive Setup Chart ───────────────────────────────────────
        all_symbols = list(dict.fromkeys(r["symbol"] for r in results))
        current_sym = st.session_state.selected_alert_symbol
        if not current_sym or current_sym not in all_symbols:
            current_sym = filtered_results[0]["symbol"] if filtered_results else all_symbols[0]

        st.markdown("---")
        chart_col1, chart_col2 = st.columns([3, 1])
        with chart_col1:
            st.markdown(
                f'<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; font-size: 1.15rem; margin-bottom: 4px;">'
                f'📈 Setup Chart &amp; Structure Levels: <span style="color:#10b981;">{current_sym}</span>'
                f'</h3>',
                unsafe_allow_html=True
            )
        with chart_col2:
            sym_idx = all_symbols.index(current_sym) if current_sym in all_symbols else 0
            picked_sym = st.selectbox(
                "Select stock to view chart",
                options=all_symbols,
                index=sym_idx,
                key="picked_chart_symbol_select",
                label_visibility="collapsed",
            )
            if picked_sym != current_sym:
                st.session_state.selected_alert_symbol = picked_sym
                current_sym = picked_sym
                st.rerun()

        # Find trade info dict for current_sym (prefer an actual signal row)
        trade_info = (
            next(
                (
                    r
                    for r in results
                    if r["symbol"] == current_sym and r.get("side")
                ),
                None,
            )
            or next((r for r in results if r["symbol"] == current_sym), {})
        )
        ikey = resolved.get(current_sym) or resolver.resolve(current_sym)

        _render_setup_chart(
            symbol=current_sym,
            instrument_key=ikey,
            unit=unit,
            interval=int(interval),
            trade_info=trade_info,
        )
    else:
        st.markdown(
            '<div style="background:rgba(15,23,42,0.72);'
            'border:1px dashed rgba(16,185,129,0.30);border-radius:14px;'
            'padding:22px;text-align:center;margin-top:8px;">'
            '<div style="font-size:1.5rem;margin-bottom:6px;">🔍</div>'
            '<div style="font-weight:600;color:#f1f5f9;'
            'font-family:Space Grotesk,sans-serif;">No live scan in this session yet</div>'
            '<div style="color:#94a3b8;font-size:0.86rem;margin-top:4px;">'
            'Add symbols to your watchlist, then click <b>Scan Now</b> '
            'to evaluate the latest closed candle. Meanwhile, explore the cached market pulse below.</div></div>',
            unsafe_allow_html=True,
        )

        # ── Pre-scan workspace: cached pulse + cached chart ───────────────
        if current_wl:
            st.markdown(
                '<div style="display:flex;align-items:baseline;justify-content:space-between;'
                'margin:18px 0 8px;">'
                '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;'
                'font-size:1.05rem;margin:0;">⚡ Watchlist Market Pulse</h3>'
                '<span style="font-size:0.76rem;color:#64748b;">Local disk cache · zero API calls</span>'
                '</div>',
                unsafe_allow_html=True,
            )
            _render_watchlist_snapshot_grid(current_wl, max_cards=8)

            cached_here = [s for s in current_wl if _load_local_symbol_df(s)[1]]
            if cached_here:
                ch_left, ch_right = st.columns([3, 1])
                with ch_left:
                    st.markdown(
                        '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;'
                        'font-size:1.05rem;margin:10px 0 0;">🕯️ Cached Chart Desk</h3>'
                        '<span style="font-size:0.76rem;color:#64748b;">'
                        'Interactive candlesticks &amp; drawing tools on saved candles</span>',
                        unsafe_allow_html=True,
                    )
                with ch_right:
                    pre_sym = st.selectbox(
                        "Cached symbol",
                        cached_here,
                        key="alerts_pre_chart_sym",
                        label_visibility="collapsed",
                    )
                _render_cached_chart_preview(pre_sym, key_prefix="alerts_pre_")

    # ── Today's session journal ──────────────────────────────────────────
    _render_today_journal(resolved, tracked_strategies, unit, int(interval))

    # ── Alert history ────────────────────────────────────────────────────
    st.divider()
    if st.session_state.pop("_reset_history_confirm", False):
        st.session_state["confirm_clear_history"] = False
    history = st.session_state.alert_history
    hist_df = pd.DataFrame(history)
    display_cols = [
        c
        for c in [
            "time",
            "symbol",
            "side",
            "entry_price",
            "stop_loss",
            "tp1",
            "tp2",
            "rr",
            "price",
            "candle_time",
            "strategy",
            "message",
        ]
        if c in hist_df.columns
    ]

    hcol1, hcol2, hcol3 = st.columns([3, 1.2, 1.2])
    with hcol1:
        st.markdown(
            f'<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
            f'font-size: 1.1rem; margin: 0;">🗂️ Alert History</h3>'
            f'<span style="font-size:0.78rem;color:#94a3b8;">'
            f'{len(history)} saved · persists across restarts</span>',
            unsafe_allow_html=True,
        )

    if history:
        rename_map = {
            "time": "Time",
            "symbol": "Symbol",
            "side": "Signal",
            "entry_price": "Entry",
            "stop_loss": "SL",
            "tp1": "TP1",
            "tp2": "TP2",
            "rr": "R:R",
            "price": "Price",
            "candle_time": "Candle Time",
            "strategy": "Strategy",
            "message": "Message",
        }
        hist_display = hist_df[display_cols].rename(columns=rename_map)
        if "Time" in hist_display.columns:
            hist_display["Time"] = pd.to_datetime(
                hist_display["Time"], errors="coerce"
            ).dt.strftime("%Y-%m-%d %H:%M:%S")
        if "Strategy" in hist_display.columns:
            hist_display["Strategy"] = hist_display["Strategy"].map(
                lambda s: get_display_name(s) if isinstance(s, str) else s
            )

        with hcol2:
            st.write("")
            st.download_button(
                "⬇️ Export history CSV",
                hist_df[display_cols].to_csv(index=False).encode("utf-8-sig"),
                file_name=f"alert_history_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                key="alerts_history_csv",
            )
        with hcol3:
            confirm_clear_history = st.checkbox(
                "Confirm clear", key="confirm_clear_history"
            )
            if st.button(
                "🗑️ Clear history",
                key="clear_alert_history",
                disabled=not confirm_clear_history,
                use_container_width=True,
            ):
                st.session_state.alert_history = []
                save_alert_history([])
                st.session_state["_reset_history_confirm"] = True
                _flash("success", "Alert history cleared.")
                st.rerun()

        _tier_legend("new", "armed", "profit", "missed", "none")

        hist_tiers = [
            _scan_status_tier(h.get("status", "none"))
            for h in history
            if isinstance(h, dict)
        ]
        hist_tiers = (hist_tiers + ["none"] * len(hist_display))[: len(hist_display)]

        _render_signal_table(
            hist_display,
            hist_tiers,
            price_cols=("Entry", "SL", "TP1", "TP2", "Price"),
            chip_cols={"Signal": _side_chip},
            symbol_col="Symbol",
            max_height=460,
        )
    else:
        st.caption(
            "No alerts yet. NEW signals will appear here and remain available after restarts."
        )


# ══════════════════════════════════════════════════════════════════════════
# VIEW 4 — TRADE JOURNAL & DAILY PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════
def render_journal():
    section_header(
        "📒 Trade Journal",
        "Daily setup log · TP / SL tracking · Hypothetical P&L · Week-by-week review",
    )
    _show_flash()

    if st.session_state.pop("_reset_journal_confirm", False):
        st.session_state["confirm_clear_journal"] = False

    entries_all = load_journal()
    watchlist = load_watchlist()
    resolved = resolver.resolve_many(watchlist)
    notional = _journal_notional()

    # Default to a window that actually contains setups, so the page is never blank.
    preset_default = "This week"
    if "journal_period_preset" not in st.session_state and entries_all:
        today_d = datetime.now(IST).date()
        week_start = today_d - timedelta(days=today_d.weekday())
        month_start = today_d - timedelta(days=29)
        have_week = any(str(e.get("date") or "") >= week_start.isoformat() for e in entries_all)
        have_month = any(str(e.get("date") or "") >= month_start.isoformat() for e in entries_all)
        preset_default = "This week" if have_week else ("Last 30 days" if have_month else "All")

    st.markdown(
        '<div style="display:inline-flex;align-items:center;gap:8px;'
        'background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);'
        'border-radius:999px;padding:6px 14px;margin-bottom:12px;'
        'color:#10b981;font-size:0.82rem;font-weight:600;">'
        '📒 Read-only journal — hypothetical outcomes, never real orders'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── View controls ─────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns([1.1, 1.4, 1.1, 1.3])
    with c1:
        preset = st.selectbox(
            "Period",
            JOURNAL_PRESETS,
            index=JOURNAL_PRESETS.index(preset_default),
            key="journal_period_preset",
        )
    with c2:
        strategy_ids = sorted(
            {e.get("strategy") for e in entries_all if e.get("strategy")}
            | set(tracked_strategies)
        )
        strategy_filter = st.selectbox(
            "Strategy",
            ["__all__", *strategy_ids],
            format_func=lambda s: "All strategies" if s == "__all__" else get_display_name(s),
            key="journal_strategy_filter",
        )
    with c3:
        notional = st.number_input(
            "Notional / trade (₹)",
            min_value=1000,
            max_value=10_000_000,
            value=int(notional),
            step=5000,
            key="journal_notional",
            help="Applied the next time you sync outcomes.",
        )
    with c4:
        st.write("")
        st.caption(
            f"Sync uses **{len(tracked_strategies)}** tracked "
            f"strateg{'y' if len(tracked_strategies) == 1 else 'ies'} · "
            f"**{int(interval)} {unit}**"
        )

    custom_start = custom_end = None
    if preset == "Custom":
        d1, d2 = st.columns(2)
        with d1:
            custom_start = st.date_input(
                "From",
                value=datetime.now(IST).date() - timedelta(days=6),
                key="journal_custom_from",
            )
        with d2:
            custom_end = st.date_input(
                "To",
                value=datetime.now(IST).date(),
                key="journal_custom_to",
            )

    start, end = _journal_preset_range(preset, custom_start, custom_end)
    filtered = _journal_filter(entries_all, start, end, strategy_filter)
    stats = _journal_stats(filtered)

    # ── Sync / settle controls ────────────────────────────────────────────
    sync_bits = []
    if preset == "All":
        sync_start = datetime.now(IST).date() - timedelta(days=6)
        sync_end = datetime.now(IST).date()
        sync_bits.append("All view syncs the last 7 days")
    else:
        sync_start, sync_end = start, end
    sync_dates = _journal_weekdays(sync_start, sync_end)
    if len(sync_dates) > 31:
        sync_dates = sync_dates[-31:]
        sync_bits.append("Sync capped to the last 31 weekdays")

    sc1, sc2 = st.columns([1.2, 3])
    with sc1:
        sync_clicked = st.button(
            "🔄 Sync & settle range",
            type="primary",
            use_container_width=True,
            key="journal_sync_range",
            help="Fetch candles for the watchlist, rebuild every setup in the range and settle outcomes.",
        )
    with sc2:
        st.caption(
            f"Syncs **{len([d for d in sync_dates])}** weekday(s) × "
            f"**{len(tracked_strategies)}** strategies for "
            f"**{len([k for k in resolved.values() if k])}** resolved watchlist symbols. "
            + (" · ".join(sync_bits) if sync_bits else "")
        )
        last_sync = st.session_state.get("journal_last_sync")
        if last_sync:
            try:
                last_dt = datetime.fromisoformat(last_sync)
                st.caption(f"Last journal sync: **{last_dt.strftime('%d %b %H:%M IST')}**")
            except Exception:
                pass

    if sync_clicked:
        summary = _journal_progress_run_multi(
            sync_dates,
            resolved,
            tracked_strategies,
            unit,
            int(interval),
            notional,
            "dashboard",
            title="📒 Rebuilding journal",
        )
        if summary is not None:
            st.rerun()

    if unit not in ("minutes", "hours"):
        st.warning(
            "Outcome tracking is designed for **intraday** timeframes (minutes/hours). "
            "Daily/weekly candles are supported, but TP/SL/EOD-zone semantics are intraday."
        )

    if not entries_all:
        st.markdown(
            '<div style="background:rgba(13,21,48,0.6);'
            'border:1px dashed rgba(16,185,129,0.25);border-radius:12px;'
            'padding:24px;text-align:center;margin-top:12px;">'
            '<div style="font-size:1.5rem;margin-bottom:6px;">📒</div>'
            '<div style="font-weight:600;color:#f1f5f9;'
            'font-family:Space Grotesk,sans-serif;">No journal entries yet</div>'
            '<div style="color:#94a3b8;font-size:0.86rem;margin-top:4px;">'
            'Add symbols on the Alerts page, scan during the session, then use '
            '<b>Sync &amp; settle range</b> to build the full daily log.</div></div>',
            unsafe_allow_html=True,
        )
        _render_journal_assumptions()
        return

    # ── KPI strip ─────────────────────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        metric_card("Setups", str(stats["count"]), color="cyan", icon="📒")
    with k2:
        metric_card("TP Hit Rate", f"{stats['tp_rate']:.0f}%", color="green", icon="🎯")
    with k3:
        metric_card("SL Hit", str(stats["stops"]), color="red", icon="🛑")
    with k4:
        metric_card(
            "Net P&L*",
            f"₹{stats['net']:,.0f}",
            color="green" if stats["net"] >= 0 else "red",
            icon="💰",
        )

    k5, k6, k7, k8 = st.columns(4)
    with k5:
        metric_card(
            "Win Rate",
            f"{stats['win_rate']:.0f}%",
            color="green" if stats["win_rate"] >= 50 else "gold",
            icon="🏆",
        )
    with k6:
        metric_card(
            "Avg / Setup",
            f"₹{stats['avg']:,.0f}",
            color="green" if stats["avg"] >= 0 else "red",
            icon="📊",
        )
    with k7:
        pf = stats["profit_factor"]
        pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
        metric_card("Profit Factor", pf_text, color="purple", icon="⚖️")
    with k8:
        metric_card("Open / Live", str(stats["open"]), color="gold", icon="⏳")

    st.caption(
        f"* Hypothetical net P&L on ₹{notional:,.0f} notional per setup, after "
        f"₹{stats['costs']:,.0f} estimated costs. Gross: ₹{stats['gross']:,.0f}. "
        "Conservative same-candle rule: SL is assumed before TP."
    )

    # ── Day-wise overview ─────────────────────────────────────────────────
    st.markdown(
        '<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
        'font-size: 1.1rem; margin: 20px 0 8px;">📅 Day-wise Performance</h3>',
        unsafe_allow_html=True,
    )

    include_empty = preset != "All"
    if start is not None:
        days = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        ]
    else:
        days = sorted(
            {date.fromisoformat(str(e.get("date"))) for e in filtered if e.get("date")}
        )

    day_rows = []
    cumulative = 0.0
    for day in days:
        day_entries = [e for e in filtered if str(e.get("date")) == day.isoformat()]
        if not day_entries and not include_empty:
            continue
        day_stats = _journal_stats(day_entries)
        cumulative += day_stats["net"]
        eod_profit = sum(1 for e in day_entries if e.get("eod_zone") == "PROFIT")
        eod_loss = sum(1 for e in day_entries if e.get("eod_zone") == "LOSS")
        day_rows.append({
            "Date": day.isoformat(),
            "Day": day.strftime("%a"),
            "Setups": day_stats["count"],
            "TP": day_stats["targets"],
            "SL": day_stats["stops"],
            "Win %": round(day_stats["win_rate"], 0),
            "EOD Profit": eod_profit,
            "EOD Loss": eod_loss,
            "Open": day_stats["open"],
            "Gross": round(day_stats["gross"], 2),
            "Costs": round(day_stats["costs"], 2),
            "Net P&L": round(day_stats["net"], 2),
            "Cumulative": round(cumulative, 2),
        })

    if day_rows:
        day_df = pd.DataFrame(day_rows)
        _tier_legend("tp2", "profit", "flat", "loss", "sl")
        day_tiers = []
        for _, drow in day_df.iterrows():
            net_v = float(drow.get("Net P&L") or 0.0)
            stops = int(drow.get("SL") or 0)
            targets = int(drow.get("TP") or 0)
            if net_v > 0 and targets and not stops:
                day_tiers.append("tp2")
            elif net_v > 0:
                day_tiers.append("profit")
            elif net_v < 0 and stops:
                day_tiers.append("sl")
            elif net_v < 0:
                day_tiers.append("loss")
            else:
                day_tiers.append("flat")
        _render_signal_table(
            day_df,
            day_tiers,
            pnl_cols=("Gross", "Costs", "Net P&L", "Cumulative"),
            plain_pct_cols=("Win %",),
            max_height=420,
        )

        chart_df = day_df.copy()
        chart_df["Date"] = pd.to_datetime(chart_df["Date"]).dt.strftime("%d %b")
        ch1, ch2 = st.columns(2)
        with ch1:
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:12px 0 2px;">'
                '📈 Cumulative Net P&L</p>',
                unsafe_allow_html=True,
            )
            cum_color = "#10b981" if float(day_df["Cumulative"].iloc[-1]) >= 0 else "#f43f5e"
            st.altair_chart(
                _altair_area_chart(
                    chart_df, "Date", "Cumulative", "Cumulative Net P&L (₹)",
                    color=cum_color, height=245,
                ),
                use_container_width=True,
            )
        with ch2:
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:12px 0 2px;">'
                '📊 Daily Net P&L</p>',
                unsafe_allow_html=True,
            )
            st.altair_chart(
                _altair_pnl_bar_chart(
                    chart_df, "Date", "Net P&L", "Net P&L (₹)", height=245
                ),
                use_container_width=True,
            )
    else:
        st.info("No journal entries in the selected period. Sync the range to build the log.")

    # ── Setup log ─────────────────────────────────────────────────────────
    st.markdown(
        '<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
        'font-size: 1.1rem; margin: 22px 0 8px;">📋 Setup Log</h3>',
        unsafe_allow_html=True,
    )

    f1, f2 = st.columns([1.2, 3])
    with f1:
        outcome_filter = st.selectbox(
            "Outcome",
            ["All", "Targets", "SL", "EOD Profit", "EOD Loss", "Open", "Ambiguous"],
            key="journal_outcome_filter",
        )
    with f2:
        journal_search = st.text_input(
            "Search symbol",
            placeholder="e.g. RELIANCE",
            key="journal_symbol_search",
        ).strip().upper()

    def _journal_log_match(entry):
        outcome = entry.get("outcome")
        if outcome_filter == "Targets" and outcome not in JOURNAL_TARGET_OUTCOMES:
            return False
        if outcome_filter == "SL" and outcome != "SL_HIT":
            return False
        if outcome_filter == "EOD Profit" and outcome != "EOD_PROFIT":
            return False
        if outcome_filter == "EOD Loss" and outcome != "EOD_LOSS":
            return False
        if outcome_filter == "Open" and entry.get("settled"):
            return False
        if outcome_filter == "Ambiguous" and not entry.get("ambiguous"):
            return False
        if journal_search and journal_search not in str(entry.get("symbol", "")).upper():
            return False
        return True

    log_entries = [e for e in filtered if _journal_log_match(e)]
    log_entries.sort(
        key=lambda e: (str(e.get("date")), str(e.get("entry_time"))),
        reverse=True,
    )

    if log_entries:
        log_rows = []
        log_tiers = []
        for entry in log_entries:
            log_tiers.append(
                _outcome_tier(
                    entry.get("outcome"),
                    bool(entry.get("settled", True)),
                    entry.get("net_pnl"),
                    entry.get("exit_reason", ""),
                )
            )
            log_rows.append({
                "Date": entry.get("date"),
                "Time": str(entry.get("entry_time") or "")[11:16],
                "Symbol": entry.get("symbol"),
                "Signal": entry.get("side"),
                "Strategy": entry.get("strategy_name") or get_display_name(entry.get("strategy", "")),
                "Entry": entry.get("entry_price"),
                "SL": entry.get("stop_loss"),
                "TP1": entry.get("tp1"),
                "TP2": entry.get("tp2"),
                "Qty": entry.get("qty"),
                "Outcome": entry.get("outcome_label") or "Awaiting result",
                "Exit": entry.get("exit_price"),
                "Exit Reason": str(entry.get("exit_reason") or "—").replace("_", " ").title(),
                "Gross": entry.get("gross_pnl"),
                "Costs": entry.get("costs"),
                "Net P&L": entry.get("net_pnl"),
                "EOD Zone": entry.get("eod_zone") or "—",
                "EOD %": entry.get("eod_move_pct"),
                "MFE": entry.get("mfe"),
                "MAE": entry.get("mae"),
                "Source": entry.get("source") or "sync",
            })
        log_df = pd.DataFrame(log_rows)
        _tier_legend("tp2", "tp1", "trail", "profit", "flat", "loss", "sl", "open")
        _render_signal_table(
            log_df,
            log_tiers,
            price_cols=("Entry", "SL", "TP1", "TP2", "Exit", "MFE", "MAE"),
            pnl_cols=("Gross", "Costs", "Net P&L"),
            pct_cols=("EOD %",),
            chip_cols={
                "Signal": _side_chip,
                "Outcome": _outcome_chip,
                "EOD Zone": _zone_chip,
            },
            symbol_col="Symbol",
            max_height=560,
        )
        st.download_button(
            "⬇️ Export journal CSV",
            log_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"trade_journal_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            key="journal_csv_export",
        )
    else:
        st.info("No setups match the selected filters.")

    # ── Symbol / strategy breakdown ───────────────────────────────────────
    if filtered:
        st.markdown(
            '<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; '
            'font-size: 1.1rem; margin: 22px 0 8px;">🧩 Breakdown</h3>',
            unsafe_allow_html=True,
        )
        b1, b2 = st.columns(2)
        with b1:
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:0 0 4px;">By symbol</p>',
                unsafe_allow_html=True,
            )
            sym_rows = []
            for symbol in sorted({e.get("symbol") for e in filtered}):
                sym_entries = [e for e in filtered if e.get("symbol") == symbol]
                sym_stats = _journal_stats(sym_entries)
                sym_rows.append({
                    "Symbol": symbol,
                    "Setups": sym_stats["count"],
                    "TP": sym_stats["targets"],
                    "SL": sym_stats["stops"],
                    "Win %": round(sym_stats["win_rate"], 0),
                    "Net P&L": round(sym_stats["net"], 2),
                })
            sym_df = pd.DataFrame(sym_rows).sort_values("Net P&L", ascending=False)
            sym_tiers = []
            for _, srow in sym_df.iterrows():
                net_v = float(srow.get("Net P&L") or 0.0)
                stops = int(srow.get("SL") or 0)
                targets = int(srow.get("TP") or 0)
                if net_v > 0 and targets and not stops:
                    sym_tiers.append("tp2")
                elif net_v > 0:
                    sym_tiers.append("profit")
                elif net_v < 0 and stops:
                    sym_tiers.append("sl")
                elif net_v < 0:
                    sym_tiers.append("loss")
                else:
                    sym_tiers.append("flat")
            _render_signal_table(
                sym_df,
                sym_tiers,
                pnl_cols=("Net P&L",),
                plain_pct_cols=("Win %",),
                symbol_col="Symbol",
                max_height=340,
            )
        with b2:
            st.markdown(
                '<p style="font-weight:600;color:#94a3b8;margin:0 0 4px;">By strategy</p>',
                unsafe_allow_html=True,
            )
            strat_rows = []
            for sid in sorted({e.get("strategy") for e in filtered}):
                strat_entries = [e for e in filtered if e.get("strategy") == sid]
                strat_stats = _journal_stats(strat_entries)
                strat_rows.append({
                    "Strategy": get_display_name(sid),
                    "Setups": strat_stats["count"],
                    "TP": strat_stats["targets"],
                    "SL": strat_stats["stops"],
                    "Win %": round(strat_stats["win_rate"], 0),
                    "Net P&L": round(strat_stats["net"], 2),
                })
            strat_df = pd.DataFrame(strat_rows).sort_values("Net P&L", ascending=False)
            strat_tiers = []
            for _, srow in strat_df.iterrows():
                net_v = float(srow.get("Net P&L") or 0.0)
                stops = int(srow.get("SL") or 0)
                targets = int(srow.get("TP") or 0)
                if net_v > 0 and targets and not stops:
                    strat_tiers.append("tp2")
                elif net_v > 0:
                    strat_tiers.append("profit")
                elif net_v < 0 and stops:
                    strat_tiers.append("sl")
                elif net_v < 0:
                    strat_tiers.append("loss")
                else:
                    strat_tiers.append("flat")
            _render_signal_table(
                strat_df,
                strat_tiers,
                pnl_cols=("Net P&L",),
                plain_pct_cols=("Win %",),
                symbol_col="Strategy",
                max_height=340,
            )

    _render_journal_assumptions()

    # ── Journal data settings ─────────────────────────────────────────────
    with st.expander("⚙️ Journal data & settings", expanded=False):
        st.caption(
            f"File: `{JOURNAL_FILE}` · {len(entries_all)} entries · "
            "The journal persists across restarts."
        )
        st.caption(
            "To log a full week without keeping the app open, run "
            "`python journal_sync.py --days 5` after the close or schedule it "
            "with Windows Task Scheduler."
        )
        confirm_clear_journal = st.checkbox(
            "Confirm clear journal", key="confirm_clear_journal"
        )
        if st.button(
            "🗑️ Clear journal",
            key="clear_journal_button",
            disabled=not confirm_clear_journal,
        ):
            save_journal([])
            st.session_state["_reset_journal_confirm"] = True
            _flash("success", "Trade journal cleared.")
            st.rerun()


def _render_journal_assumptions():
    with st.expander("ℹ️ How outcomes & P&L are calculated", expanded=False):
        st.markdown(
            """
- **Entry** is the strategy's trigger price on the candle where the setup fired.
- **Same-candle rule:** if a candle touches both SL and TP, the **SL is assumed first** (conservative). Such rows are flagged as *ambiguous*.
- **TP1** books 50% and moves the stop to entry + 0.15%. **TP2** (if the strategy defines one) books another 25%. The remainder trails at break-even and exits at square-off or the close.
- **Square-off:** 14:55 IST by default, matching the 3-Candle strategy.
- **EOD zone** compares the session's last close with the entry: `PROFIT`, `LOSS`, or `FLAT`. It is shown separately from the realized outcome.
- **MFE / MAE** are the maximum favourable / adverse excursions per share after entry.
- **Costs** are an estimate of 0.1% per side. Brokerage, STT, GST and slippage vary.
- **Notional** is configurable above; quantity is `floor(notional / entry)`.
- Outcomes for the current session are **provisional** until 15:30 IST. Run **Sync & settle range** after the close for final numbers.
- This is a research journal only. It never places orders.
            """
        )


# ══════════════════════════════════════════════════════════════════════════
# VIEW 5 — TODAY'S POSITION & P&L (BROKER-STYLE DAY DASHBOARD)
# ══════════════════════════════════════════════════════════════════════════
def _pnl_float(value) -> float | None:
    """Coerce a stored journal value into a float, or None when unusable."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(num) else num


def _pnl_color(amount) -> str:
    """Emerald / rose / slate tone for a rupee amount."""
    num = _pnl_float(amount) or 0.0
    if num > 0:
        return "#10b981"
    if num < 0:
        return "#f43f5e"
    return "#94a3b8"


def _pnl_rgb(amount) -> str:
    """RGB triplet (no parentheses) matching `_pnl_color`, for translucent fills."""
    num = _pnl_float(amount) or 0.0
    if num > 0:
        return "16,185,129"
    if num < 0:
        return "244,63,94"
    return "148,163,184"


def _pnl_hero_tile(label: str, value: str, hint: str = "") -> str:
    """One compact stat tile for the Total P&L hero band."""
    hint_html = ""
    if hint:
        hint_html = (
            '<div style="font-size:0.68rem;color:#64748b;margin-top:2px;">'
            + html_escape(hint) + "</div>"
        )
    return (
        '<div style="flex:1;min-width:128px;background:rgba(15,23,42,0.55);'
        'border:1px solid rgba(148,163,184,0.16);border-radius:12px;padding:10px 13px;">'
        '<div style="font-size:0.66rem;letter-spacing:0.08em;text-transform:uppercase;'
        'color:#94a3b8;font-weight:700;">' + html_escape(label) + "</div>"
        '<div style="font-size:1rem;color:#e2e8f0;font-weight:600;margin-top:3px;">'
        + html_escape(value) + "</div>" + hint_html + "</div>"
    )


def _journal_day(value) -> date | None:
    """Parse the session date stored on a journal entry."""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _pnl_session_label(day: date, today: date) -> str:
    """Human label for the session picker."""
    if day == today:
        return "Today · " + day.strftime("%a %d %b %Y")
    return day.strftime("%a %d %b %Y")


def _pnl_day_short(value) -> str:
    """Short '13 Nov' label for a cached-candle date."""
    day = _journal_day(value)
    return day.strftime("%d %b") if day else "—"


def _hit_kind(entry: dict) -> str:
    """Classify a setup as TP2 / TP1 / TRAIL / SL, or '' when no level was hit."""
    if entry.get("tp2_hit"):
        return "TP2"
    if entry.get("tp1_hit"):
        if str(entry.get("exit_reason") or "").upper() == "TRAILING_SL":
            return "TRAIL"
        return "TP1"
    if entry.get("sl_hit"):
        return "SL"
    return {
        "TP2_HIT": "TP2",
        "TP1_TRAIL": "TRAIL",
        "TP1_HIT": "TP1",
        "SL_HIT": "SL",
    }.get(str(entry.get("outcome") or "").upper(), "")


def _hit_label(kind: str) -> str:
    """Readable label for a hit kind."""
    return {
        "TP2": "TP2 hit",
        "TP1": "TP1 hit",
        "TRAIL": "TP1 trailed",
        "SL": "SL hit",
    }.get(kind, "—")


def _position_metrics(entry: dict, mark_price=None) -> dict:
    """Broker-style numbers for one setup: invested, mark, realised + unrealised."""
    side = str(entry.get("side") or "").upper()
    direction = 1 if side == "BUY" else (-1 if side == "SELL" else 0)
    entry_px = _pnl_float(entry.get("entry_price"))
    qty = int(_pnl_float(entry.get("qty")) or 0)
    if qty <= 0 and entry_px:
        qty = max(1, int(DEFAULT_NOTIONAL / entry_px))
    notional = _pnl_float(entry.get("notional"))
    if notional is None and entry_px:
        notional = round(qty * entry_px, 2)
    notional = notional or 0.0

    settled = bool(entry.get("settled"))
    realised = _pnl_float(entry.get("net_pnl")) if settled else None
    charges = _pnl_float(entry.get("costs"))
    mark = None
    unrealised = None

    if settled:
        mark = _pnl_float(entry.get("exit_price"))
        if mark is None:
            mark = _pnl_float(entry.get("eod_close"))
    elif entry_px and qty and direction:
        mark = _pnl_float(mark_price)
        if mark is None:
            mark = _pnl_float(entry.get("eod_close"))
        if mark is not None:
            est_charges = round(qty * entry_px * COST_PER_SIDE * 2.0, 2)
            unrealised = round(direction * (mark - entry_px) * qty - est_charges, 2)
            if charges is None:
                charges = est_charges

    total = round((realised or 0.0) + (unrealised or 0.0), 2)
    value = round(mark * qty, 2) if (mark is not None and qty) else notional
    return {
        "qty": qty,
        "notional": notional,
        "mark": mark,
        "value": value,
        "realised": realised,
        "unrealised": unrealised,
        "charges": charges,
        "total": total,
        "return_pct": round(total / notional * 100.0, 2) if notional else None,
        "settled": settled,
    }


def render_today_pnl():
    """Broker-style day dashboard: total P&L, open positions and TP/SL hit ledger."""
    section_header(
        "💼 Today's Position & P&L",
        "Broker-style session summary · Realised + mark-to-market P&L · Every TP and SL hit in one ledger",
    )
    _show_flash()

    st.markdown(
        '<div style="display:inline-flex;align-items:center;gap:8px;'
        'background:rgba(56,189,248,0.08);border:1px solid rgba(56,189,248,0.28);'
        'border-radius:999px;padding:6px 14px;margin-bottom:12px;'
        'color:#38bdf8;font-size:0.82rem;font-weight:600;">'
        '🧾 Read-only research P&amp;L from the trade journal — hypothetical fills, no orders placed.'
        '</div>',
        unsafe_allow_html=True,
    )

    now_ist = datetime.now(IST)
    today = now_ist.date()
    journal = load_journal()
    notional_cfg = _journal_notional()

    # ── Session picker (defaults to today, falls back to the last session) ──
    day_set = sorted(
        {d for d in (_journal_day(e.get("date")) for e in journal) if d},
        reverse=True,
    )
    if today not in day_set:
        day_set = [today] + day_set
    has_today = any(_journal_day(e.get("date")) == today for e in journal)
    default_day = today if (has_today or len(day_set) == 1) else day_set[1]

    ctl_head, ctl_sync, ctl_alerts = st.columns(
        [2.1, 1, 1], vertical_alignment="bottom"
    )
    with ctl_head:
        session_day = st.selectbox(
            "Session",
            options=day_set,
            index=day_set.index(default_day) if default_day in day_set else 0,
            format_func=lambda d: _pnl_session_label(d, today),
            key="pnl_session_day",
            help="Any session that has journal setups. Defaults to today.",
        )
    with ctl_sync:
        sync_label = "🔄 Sync today" if session_day == today else "🔄 Re-sync day"
        sync_clicked = st.button(
            sync_label,
            key="pnl_sync_btn",
            use_container_width=True,
            help=(
                "Rebuild every setup from this session's candles and settle "
                "TP / SL / EOD outcomes (uses one candle request per symbol)."
            ),
        )
    with ctl_alerts:
        st.button(
            "🔔 Open scanner",
            key="pnl_go_alerts",
            use_container_width=True,
            on_click=_navigate_to,
            args=("🔔 Alerts",),
        )

    if sync_clicked:
        summary = _journal_progress_run_multi(
            [session_day],
            resolver.resolve_many(load_watchlist()),
            _normalize_strategy_ids(tracked_strategies),
            unit,
            int(interval),
            notional_cfg,
            "today_pnl",
            title=f"💼 Syncing {session_day.strftime('%d %b')}",
        )
        if summary is not None:
            st.rerun()

    if session_day == today:
        st.caption(
            f"Live session · {now_ist.strftime('%H:%M:%S IST')} · outcomes are provisional "
            "until 15:30 IST, then use **Sync today** for final TP / SL settlement."
        )
    else:
        st.caption(
            f"Reviewing the {session_day.strftime('%d %b %Y')} session · "
            "hit results below are final once settled."
        )

    # ── Build broker-style views for the selected session ─────────────────
    entries = [e for e in journal if _journal_day(e.get("date")) == session_day]
    entries.sort(key=lambda e: str(e.get("entry_time") or ""), reverse=True)

    marks: dict[str, dict] = {}

    def _mark_for(symbol: str) -> dict:
        """Last cached close for a symbol — local CSV only, no API calls."""
        if symbol not in marks:
            snap = _symbol_cache_snapshot(symbol)
            if snap.get("cached"):
                marks[symbol] = {
                    "price": _pnl_float(snap.get("close")),
                    "as_of": str(snap.get("date") or ""),
                }
            else:
                marks[symbol] = {"price": None, "as_of": ""}
        return marks[symbol]

    mark_dates: dict[str, str] = {}
    views: list[tuple[dict, dict]] = []
    for entry in entries:
        symbol_key = str(entry.get("symbol") or "")
        if entry.get("settled"):
            views.append((entry, _position_metrics(entry)))
        else:
            mark = _mark_for(symbol_key)
            views.append((entry, _position_metrics(entry, mark["price"])))
            if mark["price"] is not None:
                mark_dates[symbol_key] = mark["as_of"]

    if not views:
        st.markdown(
            '<div style="background:rgba(15,23,42,0.72);'
            'border:1px dashed rgba(16,185,129,0.30);border-radius:14px;'
            'padding:22px;text-align:center;margin-top:8px;">'
            '<div style="font-size:1.5rem;margin-bottom:6px;">💼</div>'
            '<div style="font-weight:600;color:#f1f5f9;font-family:Space Grotesk,sans-serif;">'
            'No setups logged for this session yet</div>'
            '<div style="color:#94a3b8;font-size:0.86rem;margin-top:4px;">'
            'Run a scan on <b>🔔 Alerts</b> or press <b>Sync today</b> above to backfill '
            'every setup from the session\'s candles.</div></div>',
            unsafe_allow_html=True,
        )
        if len(day_set) > 1:
            st.caption(
                "Latest session with setups: **"
                + _pnl_session_label(day_set[1], today)
                + "** — pick it in the session selector above."
            )
        _render_journal_assumptions()
        return

    total = sum(v["total"] or 0.0 for _, v in views)
    realised = sum(v["realised"] or 0.0 for _, v in views)
    unrealised = sum(v["unrealised"] or 0.0 for e, v in views if not v["settled"])
    charges = sum(v["charges"] or 0.0 for _, v in views)
    invested = sum(v["notional"] for _, v in views)
    current_value = invested + total
    open_views = [(e, v) for e, v in views if not v["settled"]]
    closed_views = [(e, v) for e, v in views if v["settled"]]
    hits = [(e, v) for e, v in views if _hit_kind(e)]

    # ── Total P&L hero band (broker "overall P&L" equivalent) ──────────────
    hero_rgb = _pnl_rgb(total)
    hero_tone = _pnl_color(total)
    pct_txt = f"{total / invested * 100:+.2f}%" if invested else "—"
    hero_session = "Today" if session_day == today else session_day.strftime("%a %d %b %Y")
    tile_invested = _pnl_hero_tile("Invested", f"₹{invested:,.0f}", "notional turnover")
    tile_value = _pnl_hero_tile("Current value", f"₹{current_value:,.0f}", "invested + P&L")
    tile_charges = _pnl_hero_tile("Charges", f"₹{charges:,.0f}", "est. 0.1% per side")
    hero_html = (
        f'<div style="display:flex;flex-wrap:wrap;gap:18px;align-items:center;'
        f'background:linear-gradient(135deg,rgba({hero_rgb},0.16) 0%,'
        f'rgba(15,23,42,0.88) 55%,rgba(2,6,23,0.92) 100%);'
        f'border:1px solid rgba({hero_rgb},0.32);border-radius:18px;padding:20px 24px;'
        f'margin:2px 0 16px;backdrop-filter:blur(14px);box-shadow:0 20px 44px rgba(2,6,23,0.55);">'
        f'<div style="flex:1.35;min-width:255px;">'
        f'<div style="font-size:0.7rem;letter-spacing:0.16em;text-transform:uppercase;'
        f'color:#94a3b8;font-weight:700;">Total P&amp;L · {html_escape(hero_session)}</div>'
        f'<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-top:2px;">'
        f'<span style="font-family:Space Grotesk,sans-serif;font-size:2.55rem;line-height:1.1;'
        f'font-weight:700;letter-spacing:-0.02em;color:{hero_tone};">{_fmt_money(total, 0)}</span>'
        f'<span style="font-size:0.86rem;font-weight:700;color:{hero_tone};'
        f'background:rgba({hero_rgb},0.16);border:1px solid rgba({hero_rgb},0.40);'
        f'border-radius:999px;padding:3px 10px;">{pct_txt}</span>'
        f'</div>'
        f'<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px;'
        f'font-size:0.83rem;color:#94a3b8;">'
        f'<span>Realised <b style="color:{_pnl_color(realised)};">{_fmt_money(realised, 0)}</b></span>'
        f'<span>Unrealised <b style="color:{_pnl_color(unrealised)};">{_fmt_money(unrealised, 0)}</b></span>'
        f'<span>{len(views)} setups · {len(open_views)} open · {len(closed_views)} closed</span>'
        f'</div></div>'
        f'<div style="display:flex;gap:10px;flex:1;min-width:290px;flex-wrap:wrap;">'
        + tile_invested + tile_value + tile_charges +
        f'</div></div>'
    )
    st.markdown(hero_html, unsafe_allow_html=True)

    # ── Day KPI strip ─────────────────────────────────────────────────────
    tp_count = sum(1 for e, _ in hits if _hit_kind(e) != "SL")
    sl_count = len(hits) - tp_count
    tp2_count = sum(1 for e, _ in hits if _hit_kind(e) == "TP2")
    tp1_count = sum(1 for e, _ in hits if _hit_kind(e) == "TP1")
    trail_count = sum(1 for e, _ in hits if _hit_kind(e) == "TRAIL")

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    with k1:
        metric_card(
            "Total P&L",
            _fmt_money(total, 0),
            delta=pct_txt if invested else None,
            color="green" if total >= 0 else "red",
            icon="💰",
        )
    with k2:
        metric_card(
            "Realised",
            _fmt_money(realised, 0),
            delta=f"{len(closed_views)} closed",
            color="green" if realised >= 0 else "red",
            icon="✅",
        )
    with k3:
        metric_card(
            "Unrealised",
            _fmt_money(unrealised, 0),
            delta=f"{len(open_views)} running",
            color="gold" if not open_views else ("green" if unrealised >= 0 else "red"),
            icon="⏳",
        )
    with k4:
        metric_card(
            "TP Hits",
            str(tp_count),
            delta=f"{tp2_count} TP2 · {tp1_count} TP1" if tp_count else None,
            color="green",
            icon="🎯",
        )
    with k5:
        metric_card(
            "SL Hits",
            str(sl_count),
            delta=f"{sl_count} of {len(hits)} level exits" if hits else None,
            color="red" if sl_count else "gold",
            icon="🛑",
        )
    with k6:
        metric_card(
            "Positions",
            str(len(views)),
            delta=f"{len(open_views)} open · {len(closed_views)} closed",
            color="cyan",
            icon="📌",
        )

    # ── TP / SL hit ledger ────────────────────────────────────────────────
    st.markdown(
        '<div style="display:flex;align-items:baseline;justify-content:space-between;'
        'flex-wrap:wrap;gap:8px;margin:20px 0 8px;">'
        '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;'
        'font-size:1.1rem;margin:0;">🎯 Trades that hit TP or SL</h3>'
        f'<span style="font-size:0.78rem;color:#94a3b8;">'
        f'{len(hits)} of {len(views)} setups exited at a level</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    if hits:
        mix = {"TP2": tp2_count, "TP1": tp1_count, "TRAIL": trail_count, "SL": sl_count}
        chip_items = []
        for key, chip_css, chip_label in (
            ("TP2", "tp2", "TP2 hit"),
            ("TP1", "tp1", "TP1 hit"),
            ("TRAIL", "trail", "TP1 trailed"),
            ("SL", "sl", "SL hit"),
        ):
            if not mix[key]:
                continue
            chip_items.append(
                '<span class="sig-chip ' + chip_css + '">' + chip_label + " · "
                + str(mix[key]) + "</span>"
            )
        st.markdown(
            '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">'
            + "".join(chip_items) + "</div>",
            unsafe_allow_html=True,
        )

        f_col1, f_col2 = st.columns([1.2, 2.8])
        with f_col1:
            hit_filter = st.selectbox(
                "Show",
                ["All hits", "Take profit", "Stop loss"],
                key="pnl_hit_filter",
            )
        with f_col2:
            hit_search = st.text_input(
                "Search symbol",
                placeholder="e.g. RELIANCE",
                key="pnl_hit_search",
            ).strip().upper()

        shown_hits = []
        for entry, view in hits:
            kind = _hit_kind(entry)
            if hit_filter == "Take profit" and kind == "SL":
                continue
            if hit_filter == "Stop loss" and kind != "SL":
                continue
            if hit_search and hit_search not in str(entry.get("symbol") or "").upper():
                continue
            shown_hits.append((entry, view))

        if shown_hits:
            hit_rows = []
            hit_tiers = []
            for entry, view in shown_hits:
                hit_rows.append({
                    "Time": str(entry.get("entry_time") or "")[11:16],
                    "Symbol": entry.get("symbol"),
                    "Strategy": entry.get("strategy_name")
                    or get_display_name(entry.get("strategy") or ""),
                    "Signal": entry.get("side"),
                    "Entry": entry.get("entry_price"),
                    "SL": entry.get("stop_loss"),
                    "TP1": entry.get("tp1"),
                    "Exit": entry.get("exit_price"),
                    "Hit": _hit_label(_hit_kind(entry)),
                    "P&L": view["total"],
                    "P&L %": view["return_pct"],
                })
                hit_tiers.append(
                    _outcome_tier(
                        entry.get("outcome"),
                        bool(entry.get("settled")),
                        view["total"],
                        entry.get("exit_reason") or "",
                    )
                )
            _tier_legend("tp2", "tp1", "trail", "sl", "open")
            _render_signal_table(
                pd.DataFrame(hit_rows),
                hit_tiers,
                price_cols=("Entry", "SL", "TP1", "Exit"),
                pnl_cols=("P&L",),
                pct_cols=("P&L %",),
                wrap_cols=("Strategy",),
                chip_cols={"Signal": _side_chip, "Hit": _outcome_chip},
                symbol_col="Symbol",
                max_height=430,
            )
        else:
            st.info("No trades match the selected filter.")
    else:
        st.markdown(
            '<div style="background:rgba(15,23,42,0.6);border:1px dashed '
            'rgba(148,163,184,0.28);border-radius:12px;padding:16px 18px;color:#94a3b8;'
            'font-size:0.86rem;">No take-profit or stop-loss exits yet for this session. '
            'Running setups are listed below.</div>',
            unsafe_allow_html=True,
        )

    # ── Open positions (mark-to-market) ───────────────────────────────────
    st.markdown(
        '<div style="display:flex;align-items:baseline;justify-content:space-between;'
        'flex-wrap:wrap;gap:8px;margin:22px 0 8px;">'
        '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;'
        'font-size:1.1rem;margin:0;">📌 Open Positions</h3>'
        f'<span style="font-size:0.78rem;color:#94a3b8;">'
        f'{len(open_views)} running · marked to the last cached close</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    if open_views:
        pos_rows = []
        pos_tiers = []
        for entry, view in open_views:
            unreal = view["unrealised"] or 0.0
            pos_rows.append({
                "Time": str(entry.get("entry_time") or "")[11:16],
                "Symbol": entry.get("symbol"),
                "Strategy": entry.get("strategy_name")
                or get_display_name(entry.get("strategy") or ""),
                "Signal": entry.get("side"),
                "Entry": entry.get("entry_price"),
                "SL": entry.get("stop_loss"),
                "TP1": entry.get("tp1"),
                "LTP": view["mark"],
                "Marked": _pnl_day_short(mark_dates.get(str(entry.get("symbol") or ""))),
                "Unrealised": view["unrealised"],
                "Return %": view["return_pct"],
            })
            if unreal > 0:
                pos_tiers.append("profit")
            elif unreal < 0:
                pos_tiers.append("loss")
            else:
                pos_tiers.append("open")
        _tier_legend("profit", "loss", "open")
        _render_signal_table(
            pd.DataFrame(pos_rows),
            pos_tiers,
            price_cols=("Entry", "SL", "TP1", "LTP"),
            pnl_cols=("Unrealised",),
            pct_cols=("Return %",),
            wrap_cols=("Strategy",),
            chip_cols={"Signal": _side_chip},
            symbol_col="Symbol",
            max_height=360,
        )
        st.caption(
            "LTP is the last close in the local candle cache (no API call); **Marked** is the "
            "candle date that price came from. Run **Sync today** after 15:30 IST to settle "
            "these into realised P&L."
        )
        stale_dates = sorted(
            {d for d in mark_dates.values() if d and d != session_day.isoformat()}
        )
        if stale_dates:
            stale_positions = [
                (e, v)
                for e, v in open_views
                if mark_dates.get(str(e.get("symbol") or "")) in stale_dates
            ]
            st.warning(
                f"{len(stale_positions)} open position(s) are marked with candles from "
                f"{', '.join(_pnl_day_short(d) for d in stale_dates)} — older than this "
                "session. Run a scan to refresh prices; unrealised P&L is indicative until then."
            )
    else:
        st.caption("No open positions — every setup in this session has been settled.")

    # ── Session breakdown ─────────────────────────────────────────────────
    st.markdown(
        '<div style="display:flex;align-items:baseline;justify-content:space-between;'
        'flex-wrap:wrap;gap:8px;margin:22px 0 8px;">'
        '<h3 style="font-family:Space Grotesk,sans-serif;color:#f1f5f9;'
        'font-size:1.1rem;margin:0;">📊 Session Breakdown</h3>'
        '<span style="font-size:0.78rem;color:#94a3b8;">Net P&amp;L by engine and by symbol</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    by_strategy: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    for entry, view in views:
        strat_label = entry.get("strategy_name") or get_display_name(
            entry.get("strategy") or "—"
        )
        by_strategy[strat_label] = by_strategy.get(strat_label, 0.0) + (view["total"] or 0.0)
        symbol_key = str(entry.get("symbol") or "—")
        by_symbol[symbol_key] = by_symbol.get(symbol_key, 0.0) + (view["total"] or 0.0)

    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.markdown(
            '<p style="font-size:0.8rem;font-weight:600;color:#94a3b8;margin:0 0 4px;">'
            '🏆 Net P&amp;L by strategy engine</p>',
            unsafe_allow_html=True,
        )
        strat_df = pd.DataFrame(
            [{"Strategy": k, "Net P&L": round(v, 2)} for k, v in by_strategy.items()]
        ).sort_values("Net P&L", ascending=False)
        st.altair_chart(
            _altair_horiz_bar_chart(
                strat_df,
                "Strategy",
                "Net P&L",
                height=max(170, 36 * len(strat_df)),
            ),
            use_container_width=True,
        )
    with chart_right:
        st.markdown(
            '<p style="font-size:0.8rem;font-weight:600;color:#94a3b8;margin:0 0 4px;">'
            '📈 Top symbols by net P&amp;L</p>',
            unsafe_allow_html=True,
        )
        sym_df = pd.DataFrame(
            [{"Symbol": k, "Net P&L": round(v, 2)} for k, v in by_symbol.items()]
        ).sort_values("Net P&L", ascending=False).head(8)
        st.altair_chart(
            _altair_horiz_bar_chart(
                sym_df,
                "Symbol",
                "Net P&L",
                height=max(170, 36 * len(sym_df)),
            ),
            use_container_width=True,
        )

    # ── Full session ledger + export ──────────────────────────────────────
    with st.expander("📋 All setups this session", expanded=False):
        all_rows = []
        all_tiers = []
        for entry, view in views:
            all_rows.append({
                "Time": str(entry.get("entry_time") or "")[11:16],
                "Symbol": entry.get("symbol"),
                "Strategy": entry.get("strategy_name")
                or get_display_name(entry.get("strategy") or ""),
                "Signal": entry.get("side"),
                "Entry": entry.get("entry_price"),
                "SL": entry.get("stop_loss"),
                "TP1": entry.get("tp1"),
                "Outcome": entry.get("outcome_label") or "Awaiting result",
                "P&L": view["total"],
                "EOD Zone": entry.get("eod_zone") or "—",
            })
            all_tiers.append(
                _outcome_tier(
                    entry.get("outcome"),
                    bool(entry.get("settled")),
                    view["total"],
                    entry.get("exit_reason") or "",
                )
            )
        _render_signal_table(
            pd.DataFrame(all_rows),
            all_tiers,
            price_cols=("Entry", "SL", "TP1"),
            pnl_cols=("P&L",),
            wrap_cols=("Strategy",),
            chip_cols={"Signal": _side_chip, "Outcome": _outcome_chip},
            symbol_col="Symbol",
            max_height=420,
        )
        export_df = pd.DataFrame([
            {
                "Symbol": e.get("symbol"),
                "Strategy": e.get("strategy_name") or get_display_name(e.get("strategy") or ""),
                "Signal": e.get("side"),
                "Entry": e.get("entry_price"),
                "Stop Loss": e.get("stop_loss"),
                "TP1": e.get("tp1"),
                "TP2": e.get("tp2"),
                "Outcome": e.get("outcome_label"),
                "Hit": _hit_label(_hit_kind(e)),
                "Exit": e.get("exit_price"),
                "Quantity": v["qty"],
                "P&L": v["total"],
                "P&L %": v["return_pct"],
                "EOD Zone": e.get("eod_zone"),
            }
            for e, v in views
        ])
        st.download_button(
            "⬇️ Export session CSV",
            export_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"today_pnl_{session_day.strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="pnl_session_csv",
        )

    _render_journal_assumptions()


# ══════════════════════════════════════════════════════════════════════════
# VIEW 6 — STRATEGIES MANAGER
# ══════════════════════════════════════════════════════════════════════════
def render_strategies():
    section_header(
        "🧠 Strategies Manager",
        "Study existing strategies · Inspect parameters & logic · Create & register new strategies"
    )

    tab_study, tab_create = st.tabs(["📖  Study Strategies", "✏️  Create New Strategy"])

    # ─────────────────────────────────────────────────────────────────────
    # TAB 1 — STUDY
    # ─────────────────────────────────────────────────────────────────────
    with tab_study:
        from strategies import REGISTRY, load_strategy

        strats_available = list(REGISTRY.keys())
        if not strats_available:
            st.info("No strategies registered yet. Create one in the 'Create New Strategy' tab.")
        else:
            # Strategy selector
            sel_sid = st.selectbox(
                "Select a strategy to study",
                strats_available,
                format_func=lambda x: REGISTRY[x][1],
                key="study_strategy_select",
            )

            st.markdown("")

            try:
                mod = load_strategy(sel_sid)
                mod_path = Path(mod.__file__)
                source_code = mod_path.read_text(encoding="utf-8")

                # ── Parameter Cards ──────────────────────────────────────
                sl  = getattr(mod, "STOP_LOSS_PCT", None)
                tp  = getattr(mod, "TAKE_PROFIT_PCT", None)
                mc  = getattr(mod, "MIN_CANDLES", None)

                # Collect any custom numeric or configurable constants
                custom_params = {
                    k: v for k, v in vars(mod).items()
                    if not k.startswith("_")
                    and k not in ("signal", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "MIN_CANDLES")
                    and isinstance(v, (int, float, str, bool))
                    and not isinstance(v, type)
                }

                p1, p2, p3 = st.columns(3)
                with p1:
                    metric_card("Stop Loss", f"{sl*100:.1f}%" if sl else "N/A", color="red", icon="🛑")
                with p2:
                    metric_card("Take Profit", f"{tp*100:.1f}%" if tp else "N/A", color="green", icon="🎯")
                with p3:
                    metric_card("Min Candles", str(mc) if mc else "N/A", color="blue", icon="🕯️")

                if custom_params:
                    st.markdown('<p style="color:#94a3b8; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.08em; margin-top:16px; margin-bottom:8px;">Strategy Parameters</p>', unsafe_allow_html=True)
                    items = list(custom_params.items())
                    colors = ["cyan", "purple", "gold", "blue"]
                    for row_idx in range(0, len(items), 4):
                        row_items = items[row_idx:row_idx + 4]
                        cols = st.columns(len(row_items))
                        for j, (k, v) in enumerate(row_items):
                            with cols[j]:
                                label = k.replace("_", " ").title()
                                if isinstance(v, float):
                                    if "pct" in k.lower() or "fraction" in k.lower():
                                        val_str = f"{v*100:.2g}%"
                                    else:
                                        val_str = f"{v:.4g}"
                                elif isinstance(v, bool):
                                    val_str = "Yes" if v else "No"
                                else:
                                    val_str = str(v)
                                metric_card(label, val_str, color=colors[(row_idx + j) % len(colors)])

                # ── Logic Summary ────────────────────────────────────────
                st.markdown("")
                docstring = inspect.getdoc(mod) or ""
                sig_doc   = inspect.getdoc(mod.signal) or ""
                if docstring:
                    st.markdown(
                        f'<div class="glass-panel" style="margin-top:8px;">'
                        f'<p style="font-family: Space Grotesk, sans-serif; font-weight:600; '
                        f'color:#f1f5f9; margin-bottom:8px;">📋 Strategy Overview</p>'
                        f'<pre style="color:#94a3b8; font-size:0.82rem; white-space:pre-wrap; '
                        f'background:none; border:none; margin:0;">{docstring}</pre>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                # ── State Machine Architecture & Visual Flow ─────────────
                is_tc = sel_sid in INTRADAY_5M_STRATEGIES
                if is_tc or hasattr(mod, "get_strategy_state"):
                    st.markdown("""
                    <div style="margin-top:22px; margin-bottom:12px;">
                        <h4 style="font-family: 'Space Grotesk', sans-serif; color: #10b981; margin-bottom:4px; font-size:1.05rem;">
                            🔄 Bullish Detector State Machine
                        </h4>
                        <p style="color:#94a3b8; font-size:0.83rem; margin:0;">
                            Zero look-ahead: the 3-candle and VWAP detectors arm, shift their triggers, and report entries candle by candle:
                        </p>
                    </div>
                    """, unsafe_allow_html=True)

                    sm1, sm2, sm3, sm4 = st.columns(4)
                    with sm1:
                        st.markdown("""
                        <div class="state-flow-card">
                            <div class="state-flow-header">
                                <span class="state-badge impulse">State 1</span>
                                <span>Impulse Scan</span>
                            </div>
                            <div style="font-size:0.77rem; color:#94a3b8; line-height:1.5;">
                                <b style="color:#10b981;">3C:</b> 3 green candles; C2 closes &gt; C1 High; C3 closes &gt; C2 High.<br>
                                <b style="color:#10b981;">VWAP:</b> first candle that closes back above session VWAP.
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with sm2:
                        st.markdown("""
                        <div class="state-flow-card">
                            <div class="state-flow-header">
                                <span class="state-badge pullback">State 2</span>
                                <span>Wait Pullback</span>
                            </div>
                            <div style="font-size:0.77rem; color:#94a3b8; line-height:1.5;">
                                Wait for the <b>red pullback candle</b> after the 3C impulse, or for price to trade through the VWAP setup candle's high.
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with sm3:
                        st.markdown("""
                        <div class="state-flow-card">
                            <div class="state-flow-header">
                                <span class="state-badge armed">State 3</span>
                                <span>Armed &amp; Shift</span>
                            </div>
                            <div style="font-size:0.77rem; color:#94a3b8; line-height:1.5;">
                                <b>Mark Trigger:</b> red candle high / VWAP setup high.<br>
                                <b style="color:#f59e0b;">Dynamic Shift:</b> newer red pullback candles lower/refresh the 3C trigger; a close back below VWAP cancels the VWAP setup.
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with sm4:
                        st.markdown("""
                        <div class="state-flow-card">
                            <div class="state-flow-header">
                                <span class="state-badge intrade">State 4</span>
                                <span>In Trade</span>
                            </div>
                            <div style="font-size:0.77rem; color:#94a3b8; line-height:1.5;">
                                • <b>Intrabar Entry</b> on breakout.<br>
                                • <b>TP1 1:1.5:</b> Book 50%, lock SL to risk-free.<br>
                                • <b>Runner:</b> A+ plans book 1/3 at 3R; H1/H5 can pyramid once risk-free.<br>
                                • <b>Structure TSL:</b> Trail under swing lows; VWAP floor on H2-H5.<br>
                                • <b>Time Exit:</b> 14:55 IST hard exit.
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

                    with st.expander("📊 Complete Strategy Rules & Parameter Matrix", expanded=False):
                        rules_data = [
                            {"Parameter": "Applies To", "Rule": "Bullish NSE stocks, 5-minute intraday candles"},
                            {"Parameter": "Initial Stop Loss", "Rule": "Structural pullback / setup low ± 0.1% buffer; skip if risk > 2%"},
                            {"Parameter": "Entry Trigger", "Rule": "Intrabar break of the red candle high (3C) or setup candle high (VWAP)"},
                            {"Parameter": "Target 1", "Rule": "1 : 1.5 R:R → book 50%; stop moves to entry + 0.25R"},
                            {"Parameter": "Target 2 / Runner", "Rule": "A+ plans: 1/3 at 3R; H2-H5 extend T2 and loosen the trail"},
                            {"Parameter": "Trailing Logic", "Rule": "Only after a swing high breaks; trail under the pullback low"},
                            {"Parameter": "Pyramid", "Rule": "H1/H5 only, once risk-free (T1 booked + stop ≥ every entry)"},
                            {"Parameter": "VWAP Floor", "Rule": "H2-H5 close the runner on a candle close back below VWAP"},
                            {"Parameter": "Time Exit", "Rule": "Mandatory flat by 14:55 IST"},
                            {"Parameter": "Risk Discipline", "Rule": "Full exit on initial stop; never average down"},
                        ]
                        _render_signal_table(
                            pd.DataFrame(rules_data),
                            ["none"] * len(rules_data),
                            wrap_cols=("Rule",),
                            symbol_col="Parameter",
                            max_height=None,
                        )

                    # Live Strategy Pattern & State Inspector
                    st.markdown("""
                    <div style="margin-top:20px; margin-bottom:8px;">
                        <h4 style="font-family: 'Space Grotesk', sans-serif; color: #f1f5f9; font-size:1.0rem;">
                            🔍 Live Pattern &amp; State Inspector
                        </h4>
                        <p style="color:#94a3b8; font-size:0.8rem; margin:0;">
                            Inspect the live state machine status on any stock using real historical/intraday candles:
                        </p>
                    </div>
                    """, unsafe_allow_html=True)

                    current_wl_study = load_watchlist()
                    insp_col1, insp_col2, insp_col3 = st.columns([2, 1, 1])
                    with insp_col1:
                        default_sym = current_wl_study[0] if current_wl_study else "RELIANCE"
                        insp_symbol = st.text_input("Inspect Symbol", value=default_sym, key="insp_symbol_input")
                    with insp_col2:
                        insp_unit = st.selectbox("Unit", ["minutes", "days"], index=0, key="insp_unit_select")
                    with insp_col3:
                        insp_interval = st.number_input("Interval", min_value=1, max_value=60, value=5 if insp_unit == "minutes" else 1, key="insp_interval_input")

                    if st.button("🔎 Inspect Live Pattern State", key="btn_inspect_state"):
                        sym_clean = insp_symbol.strip().upper()
                        if not sym_clean:
                            st.warning("Enter a valid symbol.")
                        elif ts == "missing":
                            st.error("Set UPSTOX_ACCESS_TOKEN in .env to fetch live candles.")
                        else:
                            ikey = resolver.resolve(sym_clean)
                            if not ikey:
                                st.error(f"Symbol '{sym_clean}' not found in instrument master.")
                            else:
                                with st.spinner(f"Analyzing {sym_clean} pattern state..."):
                                    try:
                                        now_t = datetime.now(IST)
                                        from_d = (now_t - timedelta(days=15 if insp_unit == "minutes" else 120)).date()
                                        to_d = now_t.date()
                                        if insp_unit == "days":
                                            df_insp = fetch_historical(ikey, sym_clean, insp_unit, int(insp_interval), from_d, to_d)
                                        else:
                                            df_hist = fetch_historical(ikey, sym_clean, insp_unit, int(insp_interval), from_d, (now_t - timedelta(days=1)).date())
                                            df_intra = fetch_intraday(ikey, sym_clean, insp_unit, int(insp_interval))
                                            if not df_hist.empty and not df_intra.empty:
                                                df_insp = pd.concat([df_hist, df_intra]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                                            elif not df_intra.empty:
                                                df_insp = df_intra
                                            else:
                                                df_insp = df_hist

                                        if df_insp.empty:
                                            st.warning("No candle data available.")
                                        else:
                                            state_info = mod.get_strategy_state(df_insp)
                                            st.success(f"Analyzed {len(df_insp)} candles for **{sym_clean}**.")

                                            ic1, ic2, ic3, ic4 = st.columns(4)
                                            with ic1:
                                                st_color = "gold" if state_info["state"] == "ARMED" else ("green" if state_info["state"] in ("TRIGGERED", "IN_TRADE", "WAITING_PULLBACK") else "blue")
                                                metric_card("Current State", state_info["state"], color=st_color, icon="🔄")
                                            with ic2:
                                                trig_str = f"₹{state_info['trigger_price']:.2f}" if state_info.get("trigger_price") else "—"
                                                metric_card("Trigger Level", trig_str, color="cyan", icon="🎯")
                                            with ic3:
                                                sl_str = f"₹{state_info['pullback_extreme']:.2f}" if state_info.get("pullback_extreme") else "—"
                                                metric_card("Pullback Extreme", sl_str, color="red", icon="🛑")
                                            with ic4:
                                                sig_str = state_info.get("signal") or "None"
                                                metric_card("Active Signal", sig_str, color="green" if sig_str == "BUY" else ("red" if sig_str == "SELL" else "blue"), icon="🔔")

                                            st.info(f"📋 **State Analysis:** {state_info['message']}")

                                            # Last 5 candles preview
                                            with st.expander("🕯️ Recent Candles Preview", expanded=False):
                                                recent_df = df_insp.tail(5)[["timestamp", "open", "high", "low", "close", "volume"]].copy()
                                                recent_df["Colour"] = recent_df.apply(lambda r: "🟢 Green" if r["close"] > r["open"] else ("🔴 Red" if r["close"] < r["open"] else "⚪ Doji"), axis=1)
                                                _render_ohlcv_table(recent_df, max_height=None)
                                    except Exception as ex:
                                        st.error(f"Inspector error: {ex}")

                # ── Source Code Viewer ───────────────────────────────────
                st.markdown('<p style="color:#94a3b8; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.08em; margin-top:16px; margin-bottom:4px;">Source Code</p>', unsafe_allow_html=True)
                st.code(source_code, language="python")

                # ── File path info ───────────────────────────────────────
                st.caption(f"📁 File: `{mod_path}`")

            except Exception as ex:
                st.error(f"Could not load strategy '{sel_sid}': {ex}")

        # ── All strategies overview table (performance-graded matrix) ────
        st.divider()
        st.markdown('<h3 style="font-family: Space Grotesk, sans-serif; color: #f1f5f9; font-size: 1.0rem; margin-bottom: 10px;">📊 All Registered Strategies</h3>', unsafe_allow_html=True)

        journal_all = load_journal()
        overview_rows = []
        overview_tiers = []
        for sid, (mpath, dname) in REGISTRY.items():
            sid_entries = [e for e in journal_all if e.get("strategy") == sid]
            sid_stats = summary_stats(sid_entries)
            net_v = float(sid_stats["net"])
            stops = int(sid_stats["stops"])
            targets = int(sid_stats["targets"])
            if not sid_entries:
                tier = "none"
            elif net_v > 0 and targets and not stops:
                tier = "tp2"
            elif net_v > 0:
                tier = "profit"
            elif net_v < 0 and stops:
                tier = "sl"
            elif net_v < 0:
                tier = "loss"
            else:
                tier = "flat"
            overview_tiers.append(tier)
            try:
                m = importlib.import_module(mpath)
                row = {
                    "Strategy": dname,
                    "ID": sid,
                    "Setups": sid_stats["count"],
                    "TP": targets,
                    "SL": stops,
                    "Win %": round(sid_stats["win_rate"], 0),
                    "Net P&L": round(net_v, 2),
                    "Stop Loss": f"{getattr(m, 'STOP_LOSS_PCT', 0) * 100:.1f}%",
                    "Take Profit": f"{getattr(m, 'TAKE_PROFIT_PCT', 0) * 100:.1f}%",
                    "Min Candles": getattr(m, "MIN_CANDLES", "—"),
                }
            except Exception:
                row = {
                    "Strategy": dname,
                    "ID": sid,
                    "Setups": 0,
                    "TP": 0,
                    "SL": 0,
                    "Win %": 0,
                    "Net P&L": 0.0,
                    "Stop Loss": "—",
                    "Take Profit": "—",
                    "Min Candles": "—",
                }
            overview_rows.append(row)

        if overview_rows:
            _tier_legend("tp2", "profit", "flat", "loss", "sl", "none")
            _render_signal_table(
                pd.DataFrame(overview_rows),
                overview_tiers,
                pnl_cols=("Net P&L",),
                plain_pct_cols=("Win %",),
                symbol_col="Strategy",
                max_height=420,
            )
            st.caption(
                "Setup / TP / SL / Win % / Net P&L are read from your persisted trade journal. "
                "Empty rows mean the engine has not been scanned yet."
            )

    # ─────────────────────────────────────────────────────────────────────
    # TAB 2 — CREATE NEW STRATEGY
    # ─────────────────────────────────────────────────────────────────────
    with tab_create:
        st.markdown(
            '<div class="glass-panel" style="margin-bottom:20px;">'
            '<p style="font-family: Space Grotesk, sans-serif; font-weight:600; color:#f1f5f9; margin-bottom:6px;">🛠️ How it works</p>'
            '<p style="color:#94a3b8; font-size:0.84rem; margin:0;">'
            'Fill in the form below. A <code style="color:#10b981;">.py</code> strategy file will be generated '
            'in <code style="color:#10b981;">strategies/</code> and automatically added to the registry. '
            'The strategy will immediately appear in Backtest and Alerts dropdowns after saving.'
            '</p></div>',
            unsafe_allow_html=True,
        )

        # Strategy type presets
        PRESETS = {
            "3-Candle Pullback (built-in engine)": {
                "desc": "Delegates to the root-level 3-candle engine: three progressive green candles -> red pullback -> intrabar breakout with 50% booked at 1:1.5 and a structure trail.",
                "logic": textwrap.dedent("""\
                    # Delegates to the shared 5-minute 3-candle engine
                    return _tc_signal(df)"""),
                "imports": "from strategies.bullish_3c import signal as _tc_signal",
                "params": "MODE = 'INTRADAY_5M'\nDIRECTION_BIAS = 'BULLISH'",
                "min_candles": 5,
            },
            "Consecutive Momentum Breakout": {
                "desc": "BUY when N consecutive green candles close higher with expanding momentum; SELL on break of low.",
                "logic": textwrap.dedent("""\
                    if len(df) < CONSECUTIVE_BARS + 1:
                        return None
                    is_green = df["close"] > df["open"]
                    higher_close = df["close"].diff() > 0
                    if is_green.iloc[-CONSECUTIVE_BARS:].all() and higher_close.iloc[-CONSECUTIVE_BARS:].all():
                        return "BUY"
                    return None"""),
                "imports": "from indicators import ema",
                "params": "CONSECUTIVE_BARS = 3",
                "min_candles": 10,
            },
            "9:30 AM VWAP Breakout": {
                "desc": "Bonus setup: After 9:30 AM, enter on candle close beyond VWAP followed by extreme breakout with 1:1.5 RR.",
                "logic": textwrap.dedent("""\
                    if len(df) < 2:
                        return None
                    typical_price = (df["high"] + df["low"] + df["close"]) / 3
                    cum_vol = df["volume"].cumsum()
                    if (cum_vol == 0).all():
                        return None
                    vwap = (typical_price * df["volume"]).cumsum() / cum_vol
                    curr_c = df["close"].iloc[-1]
                    prev_c = df["close"].iloc[-2]
                    curr_vwap = vwap.iloc[-1]
                    prev_vwap = vwap.iloc[-2]
                    if prev_c <= prev_vwap and curr_c > curr_vwap:
                        return "BUY"
                    if prev_c >= prev_vwap and curr_c < curr_vwap:
                        return "SELL"
                    return None"""),
                "imports": "import pandas as pd",
                "params": "START_TIME = '09:30'\nSTOP_LOSS_PCT = 0.01\nTAKE_PROFIT_PCT = 0.02",
                "min_candles": 15,
            },
            "SMA Crossover": {
                "desc": "BUY when fast SMA crosses above slow SMA; SELL on cross below.",
                "logic": textwrap.dedent("""\
                    fast = sma(close, FAST_PERIOD)
                    slow = sma(close, SLOW_PERIOD)
                    if len(fast) < 2:
                        return None
                    if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
                        return "BUY"
                    if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
                        return "SELL"
                    return None"""),
                "imports": "from indicators import sma",
                "params": "FAST_PERIOD = 10\nSLOW_PERIOD = 30",
                "min_candles": 35,
            },
            "EMA + RSI": {
                "desc": "BUY when price closes above EMA and RSI is not overbought; SELL on reversal.",
                "logic": textwrap.dedent("""\
                    ema_vals = ema(close, EMA_PERIOD)
                    rsi_vals = rsi(close, RSI_PERIOD)
                    curr_close = close.iloc[-1]
                    curr_ema   = ema_vals.iloc[-1]
                    curr_rsi   = rsi_vals.iloc[-1]
                    if curr_close > curr_ema and curr_rsi < RSI_OVERBOUGHT:
                        return "BUY"
                    if curr_close < curr_ema and curr_rsi > RSI_OVERBOUGHT:
                        return "SELL"
                    return None"""),
                "imports": "from indicators import ema, rsi",
                "params": "EMA_PERIOD = 20\nRSI_PERIOD = 14\nRSI_OVERBOUGHT = 70",
                "min_candles": 50,
            },
            "RSI Reversal": {
                "desc": "BUY when RSI crosses above oversold level; SELL when RSI crosses below overbought.",
                "logic": textwrap.dedent("""\
                    rsi_vals = rsi(close, RSI_PERIOD)
                    if len(rsi_vals) < 2:
                        return None
                    prev_rsi = rsi_vals.iloc[-2]
                    curr_rsi = rsi_vals.iloc[-1]
                    if prev_rsi <= RSI_OVERSOLD and curr_rsi > RSI_OVERSOLD:
                        return "BUY"
                    if prev_rsi >= RSI_OVERBOUGHT and curr_rsi < RSI_OVERBOUGHT:
                        return "SELL"
                    return None"""),
                "imports": "from indicators import rsi",
                "params": "RSI_PERIOD = 14\nRSI_OVERSOLD = 30\nRSI_OVERBOUGHT = 70",
                "min_candles": 20,
            },
            "Blank (Custom)": {
                "desc": "Write your own logic from scratch.",
                "logic": "    # Write your signal logic here\n    return None",
                "imports": "from indicators import sma, ema, rsi",
                "params": "",
                "min_candles": 50,
            },
        }

        # ── Form ─────────────────────────────────────────────────────────
        preset_choice = st.selectbox(
            "Start from a preset template",
            list(PRESETS.keys()),
            key="new_strat_preset",
        )
        preset = PRESETS[preset_choice]

        col_a, col_b = st.columns(2)
        with col_a:
            new_name = st.text_input(
                "Strategy Display Name *",
                placeholder="e.g. My Momentum Strategy",
                help="Human-readable name shown in dropdowns",
                key="new_strat_name",
            )
        with col_b:
            new_id_auto = re.sub(r"[^a-z0-9]+", "_", new_name.lower()).strip("_") if new_name else ""
            new_id = st.text_input(
                "Strategy ID (auto-generated) *",
                value=new_id_auto,
                help="Lowercase snake_case identifier. Used as filename and registry key.",
                key="new_strat_id",
            )

        new_desc = st.text_area(
            "Description / Docstring",
            value=preset["desc"],
            height=80,
            key="new_strat_desc",
        )

        col_c, col_d, col_e = st.columns(3)
        with col_c:
            new_sl = st.number_input("Stop Loss %", min_value=0.1, max_value=50.0, value=3.0, step=0.5, key="new_strat_sl")
        with col_d:
            new_tp = st.number_input("Take Profit %", min_value=0.1, max_value=100.0, value=8.0, step=0.5, key="new_strat_tp")
        with col_e:
            new_mc = st.number_input("Min Candles", min_value=1, max_value=500, value=preset["min_candles"], key="new_strat_mc")

        new_imports = st.text_input(
            "Imports",
            value=preset["imports"],
            help="Python import statements your strategy needs",
            key="new_strat_imports",
        )

        new_params = st.text_area(
            "Custom Parameters (optional)",
            value=preset["params"],
            height=100,
            help="Any extra constants, e.g. FAST_PERIOD = 10",
            key="new_strat_params",
        )

        new_logic = st.text_area(
            "Signal Logic *",
            value=preset["logic"],
            height=200,
            help="Python code inside signal(df). Use 'close = df[\"close\"]'. Return 'BUY', 'SELL', or None.",
            key="new_strat_logic",
        )

        # ── Live Preview ─────────────────────────────────────────────────
        with st.expander("👁️ Preview generated code", expanded=False):
            sid_preview = new_id or "my_strategy"
            name_preview = new_name or "My Strategy"
            desc_preview = new_desc or "Custom strategy."
            params_block = "\n".join(new_params.splitlines()) if new_params.strip() else ""
            logic_indented = "\n".join(
                "    " + ln if not ln.startswith("    ") else ln
                for ln in new_logic.splitlines()
            )
            preview_code = (
                f'"""{name_preview}\n{desc_preview}\n"""\n'
                f"{new_imports}\n\n"
                + (f"STOP_LOSS_PCT   = {new_sl/100:.4f}   # {new_sl:.1f}%\n"
                   f"TAKE_PROFIT_PCT = {new_tp/100:.4f}  # {new_tp:.1f}%\n"
                   f"MIN_CANDLES     = {int(new_mc)}\n")
                + (f"{params_block}\n" if params_block else "")
                + f"\n\ndef signal(df):\n"
                + f'    """Evaluate the last row of df. Returns \'BUY\', \'SELL\', or None."""\n'
                + f"    if len(df) < MIN_CANDLES:\n        return None\n"
                + f"    close = df[\"close\"]\n\n"
                + f"{logic_indented}\n"
            )
            st.code(preview_code, language="python")

        # ── Save button ───────────────────────────────────────────────────
        st.markdown("")
        save_col, _ = st.columns([1, 3])
        with save_col:
            do_save = st.button("💾 Save & Register Strategy", type="primary", key="btn_save_strategy")

        if do_save:
            # Validation
            errors = []
            sid_clean = re.sub(r"[^a-z0-9]+", "_", new_id.lower()).strip("_") if new_id else ""
            if not new_name.strip():
                errors.append("Strategy Display Name is required.")
            if not sid_clean:
                errors.append("Strategy ID is required (letters/numbers only).")
            elif not re.match(r"^[a-z][a-z0-9_]*$", sid_clean):
                errors.append("Strategy ID must start with a letter and contain only lowercase letters, numbers, and underscores.")
            if not new_logic.strip():
                errors.append("Signal logic cannot be empty.")

            # Check for duplicate ID
            from strategies import REGISTRY as _REG
            strat_file = Path("strategies") / f"{sid_clean}.py"
            if sid_clean in _REG and strat_file.exists():
                errors.append(f"Strategy ID '{sid_clean}' already exists. Choose a different ID.")

            if errors:
                for e in errors:
                    st.error(f"❌ {e}")
            else:
                # Build the file content
                params_block = "\n".join(new_params.splitlines()) if new_params.strip() else ""
                logic_indented = "\n".join(
                    "    " + ln if not ln.startswith("    ") else ln
                    for ln in new_logic.splitlines()
                )
                file_content = (
                    f'"""{new_name.strip()}\n'
                    f'{new_desc.strip()}\n"""\n'
                    f"{new_imports.strip()}\n\n"
                    f"STOP_LOSS_PCT   = {new_sl/100:.4f}   # {new_sl:.1f}%\n"
                    f"TAKE_PROFIT_PCT = {new_tp/100:.4f}  # {new_tp:.1f}%\n"
                    f"MIN_CANDLES     = {int(new_mc)}\n"
                    + (f"{params_block}\n" if params_block else "")
                    + f"\n\ndef signal(df):\n"
                    + f'    \"\"\"Evaluate the last row of df. Returns \'BUY\', \'SELL\', or None.\"\"\"\n'
                    + f"    if len(df) < MIN_CANDLES:\n        return None\n"
                    + f"    close = df[\"close\"]\n\n"
                    + f"{logic_indented}\n"
                )

                # Write the .py file
                strat_file.write_text(file_content, encoding="utf-8")

                # Patch the registry __init__.py
                init_path = Path("strategies") / "__init__.py"
                init_src = init_path.read_text(encoding="utf-8")
                # Find the REGISTRY dict closing brace and insert before it
                new_entry = f'    "{sid_clean}": ("strategies.{sid_clean}", "{new_name.strip()}"),\n'
                # Insert before the closing } of REGISTRY
                updated = re.sub(
                    r"(REGISTRY[^}]*)(})",
                    lambda m: m.group(1) + new_entry + m.group(2),
                    init_src,
                    count=1,
                    flags=re.DOTALL,
                )
                init_path.write_text(updated, encoding="utf-8")

                st.success(f"✅ Strategy **{new_name.strip()}** saved as `strategies/{sid_clean}.py` and registered!")
                st.info("🔄 Restart the app (or press R in the terminal) to reload the strategy registry for Backtest & Alerts.")
                st.code(file_content, language="python")


# ══════════════════════════════════════════════════════════════════════════
# VIEW 7 — THEME STUDIO
# ══════════════════════════════════════════════════════════════════════════
def _theme_swatch_strip(theme: dict) -> str:
    pairs = (
        ("Page", theme["bg"]),
        ("Panel", theme["surface"]),
        ("Accent", theme["accent"]),
        ("Glow", theme["glow"]),
        ("Text", theme["text"]),
    )
    return "".join(
        f'<span class="ts-swatch" title="{html_escape(label)} · {color}" '
        f'style="background:{color};"></span>'
        for label, color in pairs
    )


def _theme_preview_html() -> str:
    return """
<div class="ts-preview">
    <div class="ts-preview-head">
        <span class="brand-logo" style="width:34px;height:34px;flex:0 0 34px;font-size:1.05rem;">📈</span>
        <div style="flex:1;min-width:0;">
            <div class="ts-preview-title">NSE Strategy Desk</div>
            <div class="ts-preview-sub">Read-only alerts · Upstox v3</div>
        </div>
        <span class="brand-live on">
            <span class="pulse-dot green" style="width:6px;height:6px;"></span>LIVE
        </span>
    </div>
    <div class="ts-nav">
        <span class="on">🏠 Command Center</span>
        <span>💼 Today's P&amp;L</span>
        <span>🔔 Alerts</span>
        <span>📊 Backtest</span>
    </div>
    <div class="ts-btns">
        <span class="ts-btn primary">Run scan</span>
        <span class="ts-btn">Backtest</span>
    </div>
    <div class="ts-metrics">
        <div class="ts-metric">
            <span>Tracked engines</span><strong>7</strong><em>▲ 5 minutes</em>
        </div>
        <div class="ts-metric">
            <span>Journal win rate</span><strong>26%</strong><em>▲ TP 21%</em>
        </div>
        <div class="ts-metric">
            <span>Hypothetical P&amp;L</span><strong>₹-37,431</strong><em>avg ₹-82</em>
        </div>
    </div>
    <div class="ts-pills">
        <span class="ribbon-pill emerald">● NSE LIVE</span>
        <span class="ribbon-pill cyan">⏱ 5m · 7 engines</span>
        <span class="ribbon-pill amber">🛡 Alert-only</span>
    </div>
</div>
"""


def render_theme_studio():
    section_header(
        "🎨 Theme Studio",
        "Make the terminal yours · edit every colour, density and motion setting · save reusable palettes",
    )

    notice = st.session_state.pop("_theme_notice", None)
    if notice:
        kind, message = notice
        {"success": st.success, "error": st.error, "info": st.info}.get(
            kind, st.info
        )(message)

    options = _theme_names()
    if st.session_state.get("theme_preset") not in options:
        st.session_state["theme_preset"] = CUSTOM_THEME
    theme = _active_theme()

    contrast = _contrast_ratio(theme["text"], theme["bg"])
    st.markdown(
        f'<div class="ts-hero">'
        f'<div><div class="ts-hero-title">{html_escape(theme["preset"])}</div>'
        f'<div class="ts-hero-sub">Saved to state/ui_prefs.json · applies to every page · '
        f"text contrast {contrast:.1f}:1</div></div>"
        f'<div class="ts-swatches">{_theme_swatch_strip(theme)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    if contrast < 4.5:
        st.warning(
            f"Low text contrast ({contrast:.1f}:1). Aim for 4.5:1 or higher — "
            "brighten the text colour or darken the page background."
        )

    left, right = st.columns([1.12, 1.0], gap="large")

    with left:
        with st.container(border=True, key="theme_palette"):
            st.markdown('<p class="ts-heading">Palette</p>', unsafe_allow_html=True)
            st.selectbox(
                "Starting point",
                options=options,
                key="theme_preset",
                on_change=_sync_theme_colors,
                help=(
                    "Pick a built-in or saved palette, then tweak any colour. "
                    "Editing a colour switches the selection to Custom."
                ),
            )

            saved_now = st.session_state.get("theme_saved_themes") or {}
            catalog = {**THEME_PRESETS, **saved_now}
            swatches = "".join(
                f'<span class="ts-swatch{" active" if name == theme["preset"] else ""}" '
                f'title="{html_escape(name)}" '
                f'style="background:linear-gradient(135deg,{spec.get("bg", "#0b1220")} 0%,'
                f'{spec.get("bg", "#0b1220")} 45%,{spec.get("accent", "#10b981")} 100%);">'
                f"</span>"
                for name, spec in catalog.items()
            )
            st.markdown(
                f'<div class="ts-swatches" style="margin:2px 0 6px;">{swatches}</div>',
                unsafe_allow_html=True,
            )

            c1, c2 = st.columns(2)
            with c1:
                st.color_picker(
                    "Page background",
                    key="theme_bg",
                    on_change=_mark_theme_custom,
                    help="The canvas behind every page.",
                )
                st.color_picker(
                    "Panel surface",
                    key="theme_surface",
                    on_change=_mark_theme_custom,
                    help="Cards, inputs and expanded panels.",
                )
                st.color_picker(
                    "Bright text",
                    key="theme_text",
                    on_change=_mark_theme_custom,
                    help="Headings and primary labels. Softer tones are derived automatically.",
                )
            with c2:
                st.color_picker(
                    "Accent",
                    key="theme_accent",
                    on_change=_mark_theme_custom,
                    help="Buttons, active navigation and chart highlights.",
                )
                st.color_picker(
                    "Glow / secondary",
                    key="theme_glow",
                    on_change=_mark_theme_custom,
                    help="Ambient background glow and the brand gradient.",
                )
            st.caption(
                "Accent and glow also drive the gradient brand mark, focus rings and "
                "the selected engine pill. Changes apply to the whole app instantly."
            )

        with st.container(border=True, key="theme_behaviour"):
            st.markdown('<p class="ts-heading">Behaviour</p>', unsafe_allow_html=True)
            st.toggle(
                "Ambient background glow",
                key="theme_ambient",
                help="Slowly drifts the radial glows. Automatically disabled for reduced-motion users.",
            )
            st.toggle(
                "Compact sidebar density",
                key="theme_compact",
                help="Tighter navigation rows and chips for smaller laptop screens.",
            )
            st.toggle(
                "Hover-expand collapsed sidebar",
                key="theme_hover_sidebar",
                help=(
                    "Collapsing the sidebar turns it into a slim rail that peeks open on hover. "
                    "The native collapse / expand buttons keep working exactly as before."
                ),
            )

        action_left, action_right = st.columns(2)
        with action_left:
            st.button(
                "↩ Reset to Midnight Emerald",
                key="theme_reset",
                use_container_width=True,
                on_click=_reset_theme,
            )
        with action_right:
            st.download_button(
                "⬇️ Export palette",
                data=json.dumps(_capture_theme_spec(), indent=2),
                file_name="nse-theme-palette.json",
                mime="application/json",
                use_container_width=True,
                key="theme_export",
            )

    with right:
        st.markdown('<p class="ts-heading">Live preview</p>', unsafe_allow_html=True)
        st.markdown(_theme_preview_html(), unsafe_allow_html=True)

        with st.container(border=True, key="theme_saved"):
            st.markdown('<p class="ts-heading">Saved themes</p>', unsafe_allow_html=True)
            saved_themes = st.session_state.get("theme_saved_themes") or {}
            if not saved_themes:
                st.markdown(
                    '<div class="ts-empty">No saved palettes yet. Dial in the colours '
                    "on the left, then save the palette below.</div>",
                    unsafe_allow_html=True,
                )
            else:
                for idx, (name, spec) in enumerate(list(saved_themes.items())):
                    clean = _sanitize_theme_spec(spec)
                    dots = "".join(
                        f'<i style="background:{clean[key]};"></i>'
                        for key in ("bg", "surface", "accent", "glow", "text")
                    )
                    row = st.columns(
                        [3.4, 1.0, 0.72], vertical_alignment="center"
                    )
                    row[0].markdown(
                        f'<div class="ts-saved"><div>'
                        f'<span class="ts-saved-name">{html_escape(name)}</span>'
                        f'<div class="ts-saved-dots" style="margin-top:4px;">{dots}</div>'
                        f"</div></div>",
                        unsafe_allow_html=True,
                    )
                    row[1].button(
                        "Apply",
                        key=f"theme_apply_{idx}",
                        use_container_width=True,
                        on_click=_apply_theme_name,
                        args=(name,),
                    )
                    row[2].button(
                        "🗑",
                        key=f"theme_delete_{idx}",
                        use_container_width=True,
                        on_click=_delete_saved_theme,
                        args=(name,),
                        help=f"Delete “{name}”",
                    )

            st.text_input(
                "Save current palette as",
                key="theme_save_name",
                placeholder="e.g. Desk Violet",
                max_chars=30,
                help="Captures the live colours on the left as a reusable theme.",
            )
            st.button(
                "💾 Save palette",
                key="theme_save_btn",
                type="primary",
                use_container_width=True,
                on_click=_save_current_theme,
            )
            st.caption(
                "Saved palettes live in `state/ui_prefs.json` next to the app."
            )


# ── Render Selected View ─────────────────────────────────────────────────
if nav == "🏠 Command Center":
    render_command_center()
elif nav == "💼 Today's P&L":
    render_today_pnl()
elif nav == "📡 API Calls & Data":
    render_api_monitor()
elif nav == "📊 Backtest":
    render_backtest()
elif nav == "🔔 Alerts":
    render_alerts()
elif nav == "📒 Trade Journal":
    render_journal()
elif nav == "🧠 Strategies":
    render_strategies()
elif nav == "🎨 Theme Studio":
    render_theme_studio()
