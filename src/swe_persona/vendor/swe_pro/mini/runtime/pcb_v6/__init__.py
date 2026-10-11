"""PersonaCodeBench SWE-bench Pro runtime for the TB3 v6 requirement loop.

This package is shipped inside the release (`runtime_bundle.tar.gz`), so it must not
import anything from the development repository.  It needs Python >= 3.12, Docker,
Harbor and the packages pinned in the release `requirements.txt`.
"""

PROTOCOL_VERSION = "tb3-requirement-loop-v1"
RUNTIME_VERSION = "pcb-swebenchpro-v6-runtime-3-host"
VERDICTS = ("MET", "PARTIAL", "UNMET", "UNOBSERVABLE")
CONDITIONS = ("single_turn", "vague_no_evolution", "persona_static", "persona")
