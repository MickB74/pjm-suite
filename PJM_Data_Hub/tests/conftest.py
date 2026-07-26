"""Shared pytest config for the PJM Data Hub test suite.

Adds the repo root to sys.path so `pjm_core` and `datasets` import the way
they do everywhere else in the project.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
