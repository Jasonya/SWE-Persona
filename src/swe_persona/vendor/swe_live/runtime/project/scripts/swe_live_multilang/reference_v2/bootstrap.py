"""Load the repository's reference modules before any pilot modules.

Use this entry point in a separate interpreter: both benchmarks have a module
named task_adapter. No reference source is copied or rewritten.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
REFERENCE = ROOT / "tb2_persona_pilot"
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REFERENCE))
