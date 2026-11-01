"""Compatibility shim: the Polymarket US quote recorder now lives in the package.

    python -m btc_signal_engine.recorders pm-quotes        # preferred (any OS, any working directory)
    .venv/Scripts/python.exe scripts/record_polymarket_us_ws.py

Same tables and behaviour as before (pm_ws_quotes, pm_trades in <research dir>/polymarket_us.db; see
btc_signal_engine/adapters/polymarket_us/recorders.py). Settings come from the environment or `.env` in the working
directory (BTCFLOW_POLYMARKET_KEY_ID / _SECRET_KEY, BTCFLOW_RESEARCH_DIR); no path is hard-coded any more.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btc_signal_engine.bootstrap.recorders import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["pm-quotes", *sys.argv[1:]]))
