"""`python -m btc_signal_engine.ops <command>`: stop, release, stop-supervisor, fresh, pair-pc, signout-pc, migrate,
migrate-sqlite-to-pg
(bootstrap/ops.py)."""
import sys

from btc_signal_engine.bootstrap.ops import main

if __name__ == "__main__":
    sys.exit(main())
