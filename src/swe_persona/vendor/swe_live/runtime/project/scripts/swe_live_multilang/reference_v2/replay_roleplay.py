"""Rescore a retained historical patch; never rerun its coding agent."""
import bootstrap
import argparse
from pathlib import Path
from support import Manifest, Checkers, read, save, file_hash, source_hashes
from roleplay import finish, PROTOCOL
from execution_policy import Deadline


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task', type=Path, required=True)
    p.add_argument('--persona', required=True)
    p.add_argument('--replay-worker', type=Path, required=True)
    p.add_argument('--sample-seconds', type=float, required=True)
    p.add_argument('--deadline-monotonic', type=float)
    p.add_argument('--output-dir', type=Path, required=True)
    a = p.parse_args()
    a.output_dir.mkdir(parents=True, exist_ok=False)
    m = Manifest(a.task); old = a.replay_worker
    protocol = read(old/'protocol.json')
    if protocol['task_sha256'] != m.task_sha256 or protocol['persona'] != a.persona:
        raise ValueError('Historical task/persona identity mismatch')
    prediction = read(old/'predictions.json')[m.task_id]['model_patch']
    transcript = read(old/'transcript.json')
    save(a.output_dir/'protocol.json', {'protocol': PROTOCOL, 'cohort': 'historical_generation',
        'generation_protocol': protocol['protocol'], 'task_sha256': m.task_sha256, 'persona': a.persona,
        'source_worker': str(old.resolve()), 'prediction_sha256': file_hash(old/'predictions.json'),
        'transcript_sha256': file_hash(old/'transcript.json'), 'sources': source_hashes(),
        'timing': 'Rescoring duration only; excluded from online whole-sample P20'})
    save(a.output_dir/'transcript.json', transcript)
    checker = Checkers(None, None, a.output_dir, m)
    checker.protect_official_tests = True
    checker.review_deadline = Deadline(a.sample_seconds, 'sample', a.deadline_monotonic)
    status, reason, evidence = checker.official(prediction)
    save(a.output_dir/'official-check.json', {'status': status, 'reason': reason, 'evidence': evidence})
    finish(a.output_dir, a.persona, m, 'completed' if status != 'unknown' else 'evaluation_invalid',
        int(status == 'pass') if status != 'unknown' else None, transcript,
        cohort='historical_generation', official=evidence,
        historical_dialogue_rating=read(old/'dialogue-rating.json') if (old/'dialogue-rating.json').exists() else None)
    return 0 if status != 'unknown' else 2


if __name__ == '__main__':
    raise SystemExit(main())
