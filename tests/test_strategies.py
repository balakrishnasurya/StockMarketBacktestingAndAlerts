import sys
from pathlib import Path

# Ensure the project root (where the standalone engine lives) is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

import math

import pandas as pd
import pytest

import hybrid_h1_pyramid
import hybrid_h2_confirmation_extension
import hybrid_h3_aplus_confluence
import hybrid_h5_adaptive_master
import run_all
import strategy_core
from strategy_core import Bar, ExitPlan, Position, TradeRecord, cli_main, run_backtest, synthetic_5min
from three_candle_bullish import ThreeCandleBullishStrategy, ThreeCandleDetector
from vwap_bullish import VwapDetector, VwapParams

DAY = pd.Timestamp("2026-01-05")
SOLDIERS = [(100, 101, 99.8, 100.8), (100.8, 102, 100.6, 101.8), (101.8, 103, 101.6, 102.9)]
RED = (102.9, 103.2, 102.2, 102.4)
BREAK = (102.4, 103.4, 102.3, 103.3)


def seq(rows, start="09:30", vwap=50.0):
    hours, minutes = map(int, start.split(":"))
    t0 = DAY + pd.Timedelta(hours=hours, minutes=minutes)
    bars = []
    for k, row in enumerate(rows):
        o, hi, lo, c, *rest = row
        bars.append(Bar(t0 + pd.Timedelta(minutes=5 * k), o, hi, lo, c, 1000.0, rest[0] if rest else vwap))
    return bars


def feed(detector, bars):
    return [t for i in range(len(bars)) if (t := detector.update(i, bars)) is not None]


def run_day(strategy, bars, finish=True):
    strategy.on_day_start("TEST", DAY.date())
    for i in range(len(bars)):
        strategy.on_bar(i, bars)
    if finish:
        strategy.on_day_end(bars)
    return strategy.trades


def block(b):
    return [
        (b, b + 1, b - 0.2, b + 0.8),
        (b + 0.8, b + 2, b + 0.6, b + 1.8),
        (b + 1.8, b + 3, b + 1.6, b + 2.9),
        (b + 2.9, b + 3.2, b + 2.2, b + 2.4),
        (b + 3.3, b + 3.5, b + 2.3, b + 2.5),
    ]


# ------------------------------------------------------------------ 3-candle detector


def test_three_candle_trigger_moves_to_latest_red_high():
    bars = seq(SOLDIERS + [RED, (102.4, 102.8, 102.0, 102.1), (102.1, 103.0, 102.05, 102.9)])
    trig = feed(ThreeCandleDetector(), bars)
    assert len(trig) == 1
    assert trig[0].index == 5
    assert trig[0].price == pytest.approx(102.8)
    assert trig[0].stops[0] == pytest.approx(102.0 * 0.999)


def test_three_candle_needs_close_above_prior_high():
    det = ThreeCandleDetector()
    feed(det, seq(SOLDIERS[:2] + [(101.8, 103, 101.6, 101.9)]))
    assert det.setups_today == 0


def test_three_candle_waits_for_compulsory_red_candle():
    det = ThreeCandleDetector()
    assert feed(det, seq(SOLDIERS + [(102.9, 104, 102.8, 103.9)])) == []
    assert det.state == "wait_red"


def test_three_candle_ignores_pattern_before_0930():
    assert feed(ThreeCandleDetector(), seq(SOLDIERS + [RED, BREAK], start="09:15")) == []


def test_only_first_two_patterns_of_the_day_trade():
    assert len(feed(ThreeCandleDetector(), seq(block(100) + block(103) + block(106)))) == 2


# ------------------------------------------------------------------ VWAP detector

VWAP_ROWS = [
    (100.0, 100.5, 99.5, 100.2, 100.0),  # 09:15 above
    (100.2, 100.3, 99.4, 99.6, 100.0),  # 09:20 below
    (99.6, 99.8, 99.3, 99.5, 100.0),  # 09:25 below
    (99.7, 100.6, 99.6, 100.4, 100.0),  # 09:30 closes back above -> setup candle
]


def test_vwap_reclaim_then_break_of_setup_high():
    bars = seq(VWAP_ROWS + [(100.4, 100.5, 100.1, 100.3, 100.0), (100.3, 100.8, 100.2, 100.7, 100.0)], start="09:15")
    trig = feed(VwapDetector(), bars)
    assert [t.index for t in trig] == [5]
    assert trig[0].price == pytest.approx(100.6)
    assert trig[0].stops[0] == pytest.approx(99.6 * 0.999)


def test_vwap_setup_cancelled_by_close_back_below():
    bars = seq(VWAP_ROWS + [(100.4, 100.5, 99.7, 99.8, 100.0), (99.8, 100.8, 99.7, 100.7, 100.0)], start="09:15")
    assert feed(VwapDetector(), bars) == []


def test_vwap_no_trade_without_a_close_below_first():
    rows = [(100 + k, 101 + k, 99.9 + k, 100.9 + k, 99.0) for k in range(8)]
    assert feed(VwapDetector(), seq(rows, start="09:15")) == []


def test_vwap_pre_0930_reclaim_only_used_when_enabled():
    rows = [(100.0, 100.2, 99.5, 99.6, 100.0), (99.6, 100.5, 99.5, 100.4, 100.0)]
    rows += [(100.4 + k, 101.0 + k, 100.3 + k, 100.9 + k, 100.0) for k in range(5)]
    bars = seq(rows, start="09:20")
    assert feed(VwapDetector(), bars) == []
    assert len(feed(VwapDetector(VwapParams(allow_pre_start_cross=True)), bars)) == 1


# ------------------------------------------------------------------ position management


def make_position(plan=None):
    pos = Position("TEST", plan or ExitPlan(), cost_pct=0.0)
    entry_bar = seq([(99.8, 100.2, 99.7, 100.1)], start="09:35")[0]
    pos.add_leg("3C", entry_bar.ts, 100.0, 1.0, 99.0)
    pos.process_bar(entry_bar, entry_bar=True)
    return pos


def test_t1_books_half_and_locks_stop_above_entry():
    pos = make_position()
    b1, b2 = seq([(100.1, 101.6, 100.5, 101.4), (101.4, 101.5, 100.2, 100.3)], start="09:40")
    pos.process_bar(b1)
    assert pos.open_qty == pytest.approx(0.5)
    assert pos.sl == pytest.approx(100.25)
    pos.process_bar(b2)
    assert pos.closed
    rec = TradeRecord.from_position(pos, "t", DAY.date())
    assert rec.gross_r == pytest.approx(0.5 * 1.5 + 0.5 * 0.25)


def test_runner_trails_only_after_swing_high_breaks():
    pos = make_position()
    bars = seq(
        [
            (100.1, 101.6, 100.5, 101.4),  # T1 at 101.5 -> stop 100.25
            (101.4, 101.5, 100.9, 101.0),  # pullback candle 1
            (101.0, 101.3, 100.8, 101.1),  # pullback candle 2
            (101.1, 102.0, 101.0, 101.9),  # breaks 101.6 -> stop under 100.8
            (101.9, 101.95, 100.6, 100.7),  # trailing stop hit
        ],
        start="09:40",
    )
    for b in bars[:4]:
        pos.process_bar(b)
    assert pos.sl == pytest.approx(100.8 * 0.999)
    pos.process_bar(bars[4])
    assert pos.closed and pos.fills[-1].reason == "trail_stop"


def test_time_exit_at_1455():
    pos = make_position()
    pos.process_bar(seq([(100.5, 100.9, 100.3, 100.6)], start="14:55")[0])
    assert pos.closed and pos.fills[-1].reason == "time_exit" and pos.fills[-1].price == 100.5


def test_stop_assumed_first_when_candle_spans_stop_and_target():
    pos = make_position()
    pos.process_bar(seq([(100.1, 101.8, 98.9, 101.0)], start="09:40")[0])
    assert pos.closed and pos.fills[-1].reason == "stop"


# ------------------------------------------------------------------ hybrids

PYRAMID_DAY = SOLDIERS + [
    RED,
    BREAK,
    (103.3, 104.0, 103.2, 103.9),
    (103.9, 105.0, 103.8, 104.9),  # T1 + 2nd 3-candle pattern
    (104.9, 105.1, 104.3, 104.4),
    (104.4, 105.3, 104.35, 105.2),  # 2nd trigger while risk-free
]

# VWAP reclaim at 09:35 and 3-candle pattern on the same 09:40 candle, then a 3-candle trigger.
CONFLUENCE_DAY = [
    (100.5, 100.8, 100.2, 100.4, 101.5),
    *[(*row, 101.5) for row in SOLDIERS],
    (*RED, 101.5),
    (*BREAK, 101.5),
]


def test_pure_three_candle_takes_one_position_at_a_time():
    trades = run_day(ThreeCandleBullishStrategy(), seq(PYRAMID_DAY))
    assert len(trades) == 1 and trades[0].legs == 1


def test_h1_adds_a_leg_once_risk_free():
    trades = run_day(hybrid_h1_pyramid.build(), seq(PYRAMID_DAY))
    assert len(trades) == 1
    assert trades[0].legs == 2
    assert "pyramid_3C" in trades[0].notes


def test_h2_extends_targets_when_other_setup_confirms():
    strat = hybrid_h2_confirmation_extension.build()
    run_day(strat, seq(CONFLUENCE_DAY, start="09:25"), finish=False)
    base = strat.position.base
    assert base.tag == "VWAP"
    assert [round(p, 2) for p, _ in base.targets] == [104.25, 105.75]
    assert [q for _, q in base.targets] == pytest.approx([1 / 3, 1 / 3])


def test_h3_skips_lone_setups_and_takes_confluent_ones():
    assert run_day(hybrid_h3_aplus_confluence.build(), seq(PYRAMID_DAY)) == []
    trades = run_day(hybrid_h3_aplus_confluence.build(), seq(CONFLUENCE_DAY, start="09:25"))
    assert len(trades) == 1 and "confluent_entry" in trades[0].notes


def test_h5_uses_aplus_plan_on_confluent_entry():
    strat = hybrid_h5_adaptive_master.build()
    run_day(strat, seq(CONFLUENCE_DAY, start="09:25"), finish=False)
    assert len(strat.position.base.targets) == 2


# ------------------------------------------------------------------ end to end


@pytest.mark.parametrize("name", list(run_all.FACTORIES))
def test_every_strategy_runs_and_never_loses_more_than_1r(name):
    trades = run_backtest(synthetic_5min(days=30, seed=3), run_all.FACTORIES[name](None), "DEMO")
    assert all(math.isfinite(t.net_r) for t in trades)
    assert all(t.gross_r >= -1.0 - 1e-9 for t in trades)


def test_cli_demo_prints_summary_for_all_strategies(capsys):
    summary = cli_main(run_all.FACTORIES, None, ["--demo"])
    assert list(summary.index) == list(run_all.FACTORIES)
    assert "total_R" in capsys.readouterr().out


def test_cli_reports_missing_csv_clearly(capsys):
    with pytest.raises(SystemExit):
        cli_main(run_all.FACTORIES, None, ["--csv", "no_such_dir/COFORGE.csv"])
    assert "file(s) not found" in capsys.readouterr().err


def test_cli_yf_download_is_saved_and_merged(tmp_path, monkeypatch):
    pytest.importorskip("yfinance")
    days = [synthetic_5min(days=5, seed=1), synthetic_5min(days=8, seed=1)]
    monkeypatch.setattr(strategy_core, "fetch_yfinance", lambda sym, period: days.pop(0))
    for _ in range(2):
        cli_main({"3C": run_all.FACTORIES["3C_bullish"]}, None, ["--yf", "COFORGE.NS", "--save-dir", str(tmp_path)])
    assert len(pd.read_csv(tmp_path / "COFORGE.csv")) == 8 * 75
