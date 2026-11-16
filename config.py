"""
Central configuration. All settings in one place.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
STATE_DIR = BASE_DIR / "state"

for d in (DATA_DIR, RESULTS_DIR, STATE_DIR):
    d.mkdir(exist_ok=True)

# ── Upstox token ─────────────────────────────────────────────────────────
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")

def token_status() -> str:
    """Return 'found' or 'missing' — never the token value."""
    return "found" if UPSTOX_ACCESS_TOKEN else "missing"

# ── API ──────────────────────────────────────────────────────────────────
UPSTOX_BASE = "https://api.upstox.com"
HISTORICAL_URL = UPSTOX_BASE + "/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
INTRADAY_URL = UPSTOX_BASE + "/v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# ── Chunk limits per request ─────────────────────────────────────────────
# Source: Upstox API v3 docs
# minutes 1-15  → max 1 month per request
# minutes >15   → max 1 quarter per request
# hours          → max 1 quarter per request
# days           → max 10 years per request
# weeks/months   → no practical limit (full history)
CHUNK_LIMITS = {
    "minutes_short": 30,    # days (≈1 month, conservative)
    "minutes_long": 89,     # days (≈1 quarter, conservative)
    "hours": 89,            # days (≈1 quarter)
    "days": 3650,           # days (≈10 years)
    "weeks": 36500,         # effectively unlimited
    "months": 36500,        # effectively unlimited
}

def get_chunk_days(unit: str, interval: int) -> int:
    """Return the max number of days per chunk for a given unit+interval."""
    if unit == "minutes":
        return CHUNK_LIMITS["minutes_short"] if interval <= 15 else CHUNK_LIMITS["minutes_long"]
    return CHUNK_LIMITS.get(unit, 89)

# ── Trading ──────────────────────────────────────────────────────────────
COST_PER_SIDE = 0.001  # 0.1% per side — estimate; real brokerage/STT/GST differ

# ── Watchlist ────────────────────────────────────────────────────────────
MAX_WATCHLIST = 20
WATCHLIST_FILE = STATE_DIR / "watchlist.json"
LAST_ALERTS_FILE = STATE_DIR / "last_alerts.json"
CHARTINK_SYNC_LOG_FILE = STATE_DIR / "chartink_sync_log.json"

# ── Trade journal ────────────────────────────────────────────────────────
JOURNAL_FILE = STATE_DIR / "trade_journal.json"
DEFAULT_NOTIONAL = 25_000     # ₹ deployed per hypothetical journal trade

# ── Defaults ─────────────────────────────────────────────────────────────
DEFAULT_CAPITAL = 100_000
DEFAULT_QTY = 10

# ── IST timezone ─────────────────────────────────────────────────────────
import zoneinfo
IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ── Request settings ─────────────────────────────────────────────────────
REQUEST_DELAY = 0.35       # seconds between API calls
RETRY_DELAY = 2.0          # seconds to wait on 429
REQUEST_TIMEOUT = 15       # seconds
