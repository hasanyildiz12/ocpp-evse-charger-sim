#!/usr/bin/env python3
"""OCPP 1.6J Charge Point Simulator — Direct Root Runner.

This script allows you to run the simulator directly from the project root
with a single command:
    python main.py
"""

import sys
from pathlib import Path

# Add src to sys.path automatically
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ocpp_charger.main import main

if __name__ == "__main__":
    main()
