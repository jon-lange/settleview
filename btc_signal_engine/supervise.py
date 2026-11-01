"""`python -m btc_signal_engine.supervise`: the portable process supervisor (bootstrap/supervise.py)."""
import sys

from btc_signal_engine.bootstrap.supervise import main

if __name__ == "__main__":
    sys.exit(main())
