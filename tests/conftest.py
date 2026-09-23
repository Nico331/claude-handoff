"""
Pytest configuration: make the plugin scripts and the test helpers importable.

The scripts are plain modules in ``scripts/`` (the plugin runs them by path, it
does not install a package), so both folders go on ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"

for extra in (SCRIPTS, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
