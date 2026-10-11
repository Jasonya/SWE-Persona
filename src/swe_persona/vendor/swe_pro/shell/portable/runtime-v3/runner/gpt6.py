"""Compatibility shim for the reference pilot's module layout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tb2_persona_pilot.gpt6 import *  # noqa: F401,F403
