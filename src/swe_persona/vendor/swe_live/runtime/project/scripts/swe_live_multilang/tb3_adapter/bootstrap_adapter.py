"""Load the native SWE runtime without changing frozen historical modules."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parents[1]
REFERENCE = SCRIPTS / 'reference_v2'
sys.path.insert(0, str(REFERENCE))
import bootstrap
sys.path.insert(0, str(HERE))
