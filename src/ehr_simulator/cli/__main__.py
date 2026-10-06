"""``python -m ehr_simulator.cli`` (the e2e harness runs commands this way)."""

import sys

from ehr_simulator.cli import main

if __name__ == "__main__":  # pragma: no cover - manual smoke
    sys.exit(main())
