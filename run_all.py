"""Backtest every strategy on the same data and print a side-by-side comparison.

Examples:
  python run_all.py --demo
  python run_all.py --yf COFORGE.NS TATAELXSI.NS PIDILITIND.NS --save-dir data
  python run_all.py --csv data/COFORGE.csv data/TATAELXSI.csv --watchlist bullish_list.csv --trades-out trades.csv
"""

from __future__ import annotations

import hybrid_h1_pyramid
import hybrid_h2_confirmation_extension
import hybrid_h3_aplus_confluence
import hybrid_h4_trend_runner
import hybrid_h5_adaptive_master
import three_candle_bullish
import vwap_bullish
from strategy_core import cli_main

MODULES = (
    three_candle_bullish,
    vwap_bullish,
    hybrid_h1_pyramid,
    hybrid_h2_confirmation_extension,
    hybrid_h3_aplus_confluence,
    hybrid_h4_trend_runner,
    hybrid_h5_adaptive_master,
)
FACTORIES = {m.NAME: m.build for m in MODULES}


if __name__ == "__main__":
    cli_main(FACTORIES, __doc__)
