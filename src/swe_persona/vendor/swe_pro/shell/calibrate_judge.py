#!/usr/bin/env python3
"""Create a source-matched judge gate outside the immutable release."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parent

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--judge-model', required=True)
    ap.add_argument('--model-config', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if out == ROOT or ROOT in out.parents:
        ap.error('Calibration outputs must be outside the frozen release')
    env = dict(os.environ, PCB_MODEL_CONFIG=str(args.model_config.resolve()),
               PCB_PERSONA_FILE=str(ROOT / 'portable/personas.json'), PYTHONDONTWRITEBYTECODE='1')
    return subprocess.call([sys.executable, '-B', str(ROOT / 'portable/runtime-v3/runner/validate_rubric.py'),
                           '--judge-model', args.judge_model, '--samples', '3', '--out', str(out)], env=env)
if __name__ == '__main__':
    raise SystemExit(main())
