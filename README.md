# NSE Strategy Alerts & Backtest

A minimal Streamlit application for **backtesting** NSE stock strategies and running **buy/sell alerts** on a watchlist, using **Upstox market data**.

> [!CAUTION]
> **Alert only.** This tool NEVER places, modifies, or cancels any order. Every alert must be reviewed manually. Trade at your own risk.

---

## Quick Start

### 1. Get Your Upstox Analytics Token

1. Go to [https://account.upstox.com/developer/apps](https://account.upstox.com/developer/apps)
2. Click the **Analytics** tab → **Generate Token**
3. Copy the token — it is read-only, valid for 1 year, and cannot place orders

> [!IMPORTANT]
> Never share your token. Never commit it to git. This project's `.gitignore` excludes `.env`.

### 2. Store the Token

```bash
# Copy the example and paste your token
cp .env.example .env
# Edit .env and set:
# UPSTOX_ACCESS_TOKEN=your_token_here
```

### 3. Install Dependencies

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 4. Run the App

```bash
streamlit run app.py
```

The app opens in your browser at `http://localhost:8501`.

---

## How It Works

### Project Layout

```
├── app.py                  # Streamlit UI with Left Menu Navigation
├── api_tracker.py          # Session API call logger & metrics tracker
├── config.py               # Central configuration
├── instruments.py          # NSE instrument master resolver
├── data_loader.py          # Upstox historical/intraday data fetcher with telemetry
├── indicators.py           # EMA, SMA, RSI
├── scanner.py              # Alert/scan logic (no Streamlit imports)
├── journal.py              # Trade journal, setup backfill & outcome tracking
├── journal_sync.py         # CLI: rebuild/settle the journal (schedulable)
├── chartink.py             # Chartink screener client (headless, no login)
├── chartink_sync.py        # CLI: replace the watchlist with today's Chartink scan
├── run_backtest.py         # Optional CLI backtest runner
├── backtest/
│   └── engine.py           # Backtest engine (no Streamlit imports)
├── strategies/
│   ├── __init__.py         # Strategy registry (7 bullish 5-minute engines)
│   ├── template.py         # Copy this to create a new signal()-based strategy
│   ├── bullish_adapter.py  # Bridges the engine to signal()/levels/backtest
│   ├── bullish_3c.py       # 3-Candle Bullish adapter
│   ├── bullish_vwap.py     # VWAP Reclaim adapter
│   └── bullish_h1.py ... h5.py
├── strategy_core.py        # Shared bulls-only 5-minute backtest engine
├── three_candle_bullish.py # 3-candle detector + strategy
├── vwap_bullish.py         # VWAP reclaim detector + strategy
├── hybrid_engine.py        # Hybrid 3C + VWAP engine
├── hybrid_h1_pyramid.py ... hybrid_h5_adaptive_master.py
├── run_all.py              # Side-by-side engine comparison CLI
├── tests/
│   ├── test_engine.py      # Offline engine tests
│   ├── test_strategies.py  # Root-engine strategy/detector tests
│   ├── test_scanner.py     # Offline scanner tests
│   ├── test_journal.py     # Offline journal / outcome tests
│   ├── test_multi_strategy.py # Offline multi-strategy scan / logging tests
│   ├── test_instruments.py # Offline instrument tests
│   ├── test_api_tracker.py # Offline API tracker tests
│   ├── test_chartink.py    # Offline Chartink client / watchlist sync tests
│   └── test_app.py         # Streamlit app render test
├── data/                   # Cached candle data (git-ignored)
├── results/                # Backtest trade CSVs (git-ignored)
├── state/                  # Watchlist, alerts, trade journal (git-ignored)
├── .env.example            # Token placeholder
├── .gitignore
├── requirements.txt
└── README.md
```

### Registered Strategies

All seven strategies are **long-only, 5-minute intraday engines**. They share the same
risk model: structural stop with a 0.1% buffer (skip if risk > 2%), 50% booked at
1:1.5R, stop moved to entry + 0.25R, a structure trail for the runner, and a hard
square-off at 14:55 IST.

| ID | Strategy | Edge |
|---|---|---|
| `three_candle_bullish` | 3-Candle Bullish | Three progressive green candles → compulsory red pullback → intrabar break of the pullback high |
| `vwap_bullish` | VWAP Reclaim Bullish | First close back above session VWAP after a close below; buy through the setup candle's high |
| `h1_pyramid` | H1 · Pyramid | First 3C/VWAP trigger; adds up to two 0.5R legs once the position is risk-free |
| `h2_confirm_extend` | H2 · Confirmation Extension | First trigger; extends T2 (≥2.5R) and loosens the trail when the other setup confirms |
| `h3_aplus_confluence` | H3 · A+ Confluence | Takes only entries where 3C and VWAP agree inside a 12-bar window; A+ target plan |
| `h4_trend_runner` | H4 · Trend Runner | First trigger with a wide swing trail and a VWAP regime floor for trend days |
| `h5_adaptive_master` | H5 · Adaptive Master | Switches between class, A+, extension and pyramid behaviour based on what confirms |

### Left Menu Navigation & Tabs

Navigate using the **Left Menu** in the sidebar. Use the native collapse arrow to fold it into a slim rail at the left edge — hovering that rail peeks the full menu back open, and the arrow at the top-left docks it again. Prefer a permanently docked menu? Turn off **Hover-expand collapsed sidebar** in **🎨 Theme Studio → Behaviour**. Hover expansion is disabled on narrow and touch screens.

The **⚙️ Engine & Timeframe** card under the menu is the single source of truth for the rest of the app:
- **Engine** — the strategy used for backtests, charts and journal attribution.
- **Timeframe** — 5m / 15m / 1h / 1D pills, or **Custom** for any unit + interval.
- **Alert coverage** — *Scan all engines* by default, or switch it off to choose a subset.

Any change here immediately updates the top command ribbon and every view that uses candles.

#### 1. 📡 API Calls & Data Monitor
- **Key Metrics KPI cards**: Total Live Calls, Cache Hits & Savings %, Average Latency (ms), Throttling Events (429 retries), Total Candles Processed, and Gateway Health Status.
- **Quick Test API Call**: Test your Upstox Analytics Token with a single click and observe round-trip latency.
- **API Call History Log**: Real-time table of every request (Time, Symbol, Endpoint, Interval, Range, HTTP Status, Latency, Candles, Details) with CSV export.
- **Data Fetched & OHLCV Inspector**: Inspect any session or disk-cached candle DataFrame, view summary statistics (latest close, price range, volume), run a data-integrity audit, and see interactive price trend charts.
- **Disk Cache Storage**: View files in `data/`, their size, and candle coverage.

#### 2. 💼 Today's P&L
- **Broker-style day summary**: one hero card showing **Total P&L** in rupees, return on deployed capital, the realised / unrealised split, invested turnover, current value and estimated charges
- **Open positions ledger**: every running setup with entry, SL, TP1, mark-to-market **LTP** (last locally cached close), the candle date that price came from, and unrealised P&L
- **Trades that hit TP or SL**: a dedicated ledger of every setup that exited at a level — TP2 / TP1 / TP1-trailed / SL — with entry, SL, TP1, exit price, P&L and P&L %, filterable by take-profit or stop-loss and searchable by symbol
- **Session breakdown**: net P&L by strategy engine and by symbol, plus an expander with the full session ledger and a CSV export
- **Session picker**: defaults to today and falls back to the most recent session that has setups, so the page is never blank
- **Sync today**: rebuilds every setup from the session's candles and settles TP / SL / EOD outcomes (same engine as the Trade Journal)
- Mark-to-market uses the last close in `data/` — no extra API calls — and clearly warns when those candles are older than the selected session
- Read-only research P&L only: hypothetical fills from the journal, never an order

#### 3. 📊 Backtest
- Pick a strategy, symbol, date range, capital, and quantity
- Click **Run Backtest** to see metrics, equity curve, and trade list
- Download the trades as CSV
- Results are **estimates** — costs use 0.1% per side (real brokerage/STT/GST differ)

#### 4. 🔔 Alerts
- **Watchlist editor**: add one or many symbols at once (comma/newline separated), remove multiple, or clear the list (max 20 to respect API rate limits)
- **Scan now**: evaluate the latest closed candle for each symbol with live progress
- **Auto-refresh**: **on by default**, every **5 min** (adjustable 1–60); uses `st.fragment(run_every=...)` — no extra dependencies. Only scans when the interval has actually elapsed, and the Alerts page must stay open
- **Strategies to track**: *Scan all engines* (the default) evaluates every registered engine on each scan, or switch the toggle off to pick a subset. Candles are fetched **once per symbol**, and each setup is logged to the Trade Journal with the strategy that produced it
- Scan results show one row per symbol × strategy, with a **Strategy** column, and separate per-strategy journal rows
- Auto-scan only runs during NSE market hours (Mon–Fri 09:15–15:30 IST) unless **Run outside market hours** is checked
- **Summary & filters**: scan counts for NEW / ARMED / no-signal / missed / issues, plus status filtering and symbol search
- **Export**: download the current scan results or alert history as CSV
- **On-chart drawing tools**: select the **Long** or **Short** position tool and click entry → stop-loss → target to plot shaded risk/reward boxes with R:R and % labels. Trend line, ray, horizontal line and rectangle tools are also available, with erase / undo / clear-all. Drawings are saved per symbol and timeframe in the browser.
- New signals show a toast, a compact signal card, and an optional best-effort sound beep
- **Catch-up alerts**: for the registered 5-minute strategies, a scan that runs *after* the entry candle has closed still reports the trigger (status `NEW` while the trade is open, `MISSED` once it has already exited) instead of silently dropping it. Alerts are de-duplicated per entry candle, so each trigger is reported once.
- Alert history persists across restarts in `state/alert_history.json` (last 500 alerts), with CSV export and a clear button
- **Automatic journal logging**: every new triggered setup is also written to `state/trade_journal.json`, and the **Today's Session Journal** strip shows live setups, TP/SL hits and provisional P&L. Click **Sync today** after the session to backfill any setups the app missed
- **View any setup**: each journal row has a **View** button that opens **📈 Setup Chart & Structure Levels** below the table, with that setup's entry, SL, TP1/TP2 lines and trigger marker highlighted. Close it with **✖ Close chart**

#### 5. 📒 Trade Journal
- Day-wise and week-wise review of every triggered setup: entries, SL/TP1/TP2, hit/miss, exit, MFE/MAE and EOD profit/loss zone
- **Sync & settle range**: fetches candles for the watchlist (one request per symbol), rebuilds every setup in the range, and settles outcomes
- **KPI strip**: setups, TP hit rate, SL hits, win rate, net P&L, avg P&L, profit factor and open/live count
- **Day-wise table + charts**: daily wins/losses, net P&L, cumulative equity curve and setup log with CSV export
- **Breakdowns** by symbol and strategy
- Hypothetical P&L uses a configurable **notional per trade** (default ₹25,000) and estimated 0.1% costs per side
- Journal entries persist across restarts in `state/trade_journal.json`

#### 6. 🧠 Strategies
- Study existing strategies, inspect parameters and signal logic, and create/register new strategies from the UI

#### 7. 🎨 Theme Studio
- **Live palette editing**: page background, panel surface, accent, glow/secondary and bright text — the rest of the palette (soft text, muted tones, secondary accents and button label contrast) is derived and updates instantly across every view
- **Six curated presets** (Midnight Emerald, Ocean Sapphire, Royal Violet, Sunset Amber, Crimson Rose, Graphite Mono) plus a **Custom** scratch palette
- **Behaviour**: ambient background glow, compact sidebar density, and hover-expand collapsed sidebar
- **Saved themes**: name and save the current palette, apply or delete saved palettes later, and export the current palette as JSON
- Preferences persist in `state/ui_prefs.json`; the sidebar **🎨 Appearance** popover shows the live palette and jumps straight to the studio
- A WCAG contrast readout warns when text and background are too close to read comfortably

---

## Writing a New Strategy

### Step 1: Copy the Template

```bash
cp strategies/template.py strategies/my_strategy.py
```

### Step 2: Implement `signal(df)`

Edit `strategies/my_strategy.py`:

```python
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.08
MIN_CANDLES = 50

def signal(df):
    """Return 'BUY', 'SELL', or None for the last row."""
    # Your logic here, using only df (data up to current row)
    close = df["close"].iloc[-1]
    # ...
    return None
```

### Step 3: Register It (ONE line)

Edit `strategies/__init__.py`, add to the `REGISTRY` dict:

```python
REGISTRY = {
    "three_candle_bullish": ("strategies.bullish_3c", "3-Candle Bullish (5m)"),
    "vwap_bullish": ("strategies.bullish_vwap", "VWAP Reclaim Bullish (5m)"),
    "my_strategy": ("strategies.my_strategy", "My Strategy"),  # ← add this
}
```

That's it. Both the backtest and the alert scanner will pick up your new strategy automatically.

---

## Daily / Weekly Trade Journal

The journal turns intraday setups into a reviewable track record.

1. **Morning** — add the day's stocks on the **Alerts** page and keep auto-refresh (or Scan Now) running through the session.
2. **During the session** — every new triggered setup is logged automatically. The **Today's Session Journal** strip shows live setups, TP/SL hits and provisional P&L.
3. **Any time** — open **💼 Today's P&L** for a broker-style read on the day: total P&L, open positions marked to the last cached price, and a ledger of every trade that hit TP or SL.
4. **After the close (15:30 IST)** — open **📒 Trade Journal** and click **Sync & settle range** (or **Sync today** on the **💼 Today's P&L** page). This rebuilds every setup from the session's candles (including ones the app missed) and settles TP / SL / EOD outcomes.
5. **Review** — use *This week* / *Last 7 days* to see day-wise P&L, win rate, target hits, EOD profit/loss zones and cumulative equity.
6. **Repeat** — the journal persists in `state/trade_journal.json`, so a week (or longer) builds up automatically.

### Automated end-of-day sync

The CLI can rebuild/settle the journal without the Streamlit app running:

```bash
# Today's session
python journal_sync.py

# Last 5 calendar days (weekdays only)
python journal_sync.py --days 5

# A specific day and symbol list
python journal_sync.py --date 2026-11-10 --symbols RELIANCE,INFY

# Every strategy you track in the Alerts sidebar
python journal_sync.py --strategies all
```

Schedule `journal_sync.py --days 1` with **Windows Task Scheduler** at 15:35 IST to keep a complete log even if the app is closed.

### Outcome assumptions

- **Entry** is the strategy's trigger price on the candle where the setup fired.
- If a single candle touches both SL and TP, the **SL is assumed first** (conservative) and the row is flagged *ambiguous*.
- **TP1** books 50% at 1:1.5R and moves the stop to entry + 0.25R. The runner trails on swing lows and exits at square-off or the close (H2/H5 may extend TP2 to 2.5R when another setup confirms).
- **Square-off** is 14:55 IST, matching every registered 5-minute strategy.
- **EOD zone** compares the session's last close with the entry (`PROFIT` / `LOSS` / `FLAT`) and is reported separately from the realised outcome.
- **P&L** is hypothetical: `floor(notional / entry)` quantity, configurable notional (default ₹25,000), and estimated 0.1% costs per side.
- Current-session outcomes are **provisional** until 15:30 IST. Run the sync after the close for final numbers.
- This is a research journal only. It never places orders.

---

## Daily Watchlist from Chartink

The **UB Intra Scan** screener on chartink.com can fill the watchlist for you — no browser,
no login, no copy-paste.

### From the Alerts page (no terminal)

The **Alerts** page has a **Chartink** button right next to **Add**. One click runs the screener,
validates every symbol against the Upstox master, replaces the watchlist (yesterday's list
disappears in the same atomic write) and reports what changed — *N new · N removed · N carried
over* — with an **Undo** button. A caption under the watchlist shows the last sync time, so you
can confirm a scheduled run actually happened.

It writes the same two files as the CLI: `state/watchlist.json` and `state/chartink_sync_log.json`.

### From the terminal

```bash
python chartink_sync.py --dry-run      # preview today's list, write nothing
python chartink_sync.py                # replace state/watchlist.json
python chartink_sync.py --top 10       # keep only the 10 strongest % movers
python chartink_sync.py --max 20       # hard cap on symbol count
python chartink_sync.py --no-validate  # trust raw Chartink symbols
```

What it does:

1. `GET https://chartink.com/screener/ub-intra-scan` to mint a session, then reads the `XSRF-TOKEN` cookie.
2. `POST https://chartink.com/screener/process` with the captured `scan_clause` / `debug_clause` / `column_clause`.
3. Parses `data[].nsecode` — the same list the page's **Copy** button produces, plus close / %change / volume.
4. Validates every symbol against the Upstox instrument master; unknown tickers are dropped and reported.
5. Replaces `state/watchlist.json` in one **atomic** write — yesterday's symbols disappear in the same step, so there is no "clear then add" window and a crash cannot leave a half-written list.

Safety rails:

- **0 rows** from Chartink (broken screener, block page, login wall) → abort and leave the existing watchlist untouched.
- A failed instrument-master download skips validation instead of emptying the list.
- Every run appends to `state/chartink_sync_log.json` (last 60 runs): timestamp, symbols, dropped tickers.

> [!NOTE]
> The screener runs anonymously and works without an account. Chartink serves **end-of-day**
> data on the free plan — the "Realtime data" banner in the page refers to their premium tier —
> so treat the list as a *universe* to watch, not as live confirmation of a 5-minute signal.

### Scheduling it

Register a **Windows Task Scheduler** task at **09:16 IST** (weekdays), with *Wake the computer
to run this task* enabled:

| Field | Value |
|---|---|
| Program | `D:\workspace\Stretegy_alert_Upstocks\venv\Scripts\python.exe` |
| Arguments | `chartink_sync.py` |
| Start in | `D:\workspace\Stretegy_alert_Upstocks` |

Then reload the **Alerts** page once (Ctrl+R) — auto-refresh scans the new list from there on.
Skip NSE holidays, or the task will reload a stale list.

The screener clauses live at the top of `chartink.py`. If you edit the screener's conditions on
chartink.com, re-capture them (DevTools → Network → `process` → Payload) and update the constants.

---

## CLI Backtest

```bash
python run_backtest.py \
    --strategy three_candle_bullish \
    --symbol RELIANCE \
    --unit minutes --interval 5 \
    --from 2024-01-01 --to 2024-12-31 \
    --capital 100000 --qty 10
```

Add `--no-cache` to force re-fetch.

---

## Upstox API Chunk Limits

The data fetcher chunks requests automatically:

| Unit | Interval | Max per Request |
|------|----------|----------------|
| minutes | 1–15 | 1 month |
| minutes | 16–300 | 1 quarter |
| hours | 1–5 | 1 quarter |
| days | 1 | 10 years |
| weeks | 1 | Unlimited |
| months | 1 | Unlimited |

A 0.35s delay is added between requests. On HTTP 429, the fetcher retries once after 2 seconds.

---

## Running Tests

```bash
# All offline — no Upstox token needed
python tests/test_engine.py
python tests/test_scanner.py
python tests/test_instruments.py
python tests/test_journal.py
python tests/test_multi_strategy.py
python tests/test_app.py
```

---

## Known Limitations

- **Browser must stay open** for auto-scan alerts. There are no push notifications.
- **Sleeping laptop** stops scans.
- **Cache**: data is cached in `data/`. Delete files there to force re-fetch, or use the "Skip cache" checkbox.
- **Costs are estimates**: 0.1% per side. Real brokerage, STT, stamp duty, SEBI fees, and GST differ.
- **Intraday warm-up**: early in the session, there may not be enough candles for EMA/RSI. The scanner marks these as "insufficient data" instead of generating false signals.
- **Holidays**: the scanner does not have a holiday calendar. On non-trading days, you may get "no candles" or stale data.
- **Journal outcomes are estimates**: same-candle SL/TP ties are resolved conservatively (SL first), partial bookings and break-even trailing are simplified, and costs are approximate. Current-session results stay provisional until 15:30 IST.
- **Journal sync needs candle history**: a symbol that was not in the watchlist on a past day is not backfilled automatically for that day unless you add it and sync again.
- **Auto-logging requires the app**: new scan alerts are logged live only while the app is open, but the post-close **Sync** (or `journal_sync.py`) rebuilds the complete session from candles.
- **Multi-strategy scans share one fetch per symbol**: selecting several strategies (or all seven) does not multiply API calls, but it does multiply journal rows when several engines trigger on the same symbol. Each row keeps its own strategy id.

---

## Safety Checklist

Before trading any signal:

- [ ] Backtest on **daily** data
- [ ] Backtest on **intraday** data (e.g. 15-min candles)
- [ ] Check the **max drawdown** — can you tolerate it?
- [ ] Verify **stop-loss** is set in the strategy
- [ ] Review **every single alert** manually before placing a trade
- [ ] Use **paper trading** first
- [ ] Understand that past performance does not predict future results

---

## Future Additions

These can be added later without changing the strategy or engine:

- **Telegram alerts**: each alert is a plain dict with a `message` field — plug in a Telegram notifier
- **GitHub Actions**: schedule scans via CI/CD for server-side alerts
- **More strategies**: just copy the template and register

---

## License

This project is for personal educational use. Not financial advice. Trade at your own risk.
