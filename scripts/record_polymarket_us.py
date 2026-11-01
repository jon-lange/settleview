"""Compatibility shim: the Polymarket US settlement recorder now lives in the package.

    docker compose up -d rec-pm-settle                              # how it runs: the compose stack's recorder service
    python -m btc_signal_engine.recorders pm-settle [--every 5]     # the same entry point, wherever it is installed

Same table and behaviour as before (pm_windows in <research dir>/polymarket_us.db; public client, no keys; see
btc_signal_engine/adapters/polymarket_us/recorders.py). The folder is BTCFLOW_RESEARCH_DIR (default
`<db dir>/research`).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btc_signal_engine.bootstrap.recorders import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["pm-settle", *sys.argv[1:]]))
