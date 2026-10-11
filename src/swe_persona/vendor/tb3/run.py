"""Run the packaged persona controller through a native Harbor trial."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'runtime'))
from protocol import read, write
from screening import benchmark_outcome


def configuration(task_id, output, arm='persona', *, dry_run=False):
    from harbor.models.task.task import Task
    from harbor.models.trial.config import TrialConfig
    task_dir = ROOT / 'tasks' / task_id
    task = Task(task_dir)
    binding_path = task_dir / 'tests/persona/binding.json'
    binding = read(binding_path)
    from requirement_loop import load_bundle
    load_bundle(binding, binding_path)
    if not binding['accepted_for_screening']:
        raise ValueError('Task did not pass construction review')
    limit = task.config.agent.timeout_sec
    config = {'task': {'path': str(task_dir)},
              'trial_name': task_id + '-' + arm + '-' + uuid.uuid4().hex[:8],
              'trials_dir': str(Path(output).resolve()),
              'agent': {'import_path': 'terminal_agent:PersonaTerminalAgent',
                        'model_name': os.environ.get('PCB_MODEL', 'gpt-6-astra'),
                        'override_timeout_sec': limit + 60,
                        'kwargs': {'arm': arm, 'limit_seconds': limit, 'binding_path': str(binding_path)}},
              'environment': {'type': 'docker', 'delete': True}}
    from gpu_environment import pin_task_gpu
    # Metadata validation must also work on a host without an allocated GPU.
    # Actual execution still requires an explicit device before any trial starts.
    gpu = None if dry_run else pin_task_gpu(task_dir, task.config.environment.gpus)
    if task.config.environment.gpus:
        config["environment"]["import_path"] = "gpu_environment:PinnedNvidiaDockerEnvironment"
    if gpu is not None:
        config["agent"]["kwargs"]["gpu_device"] = gpu
    TrialConfig.model_validate(config)
    return config, binding


async def execute(config, binding, experience=False):
    from trial_adapter import make_trial, check_persona, tree_index
    trial = await make_trial(config)
    # Only the original baseline receives the complete original instruction.
    if config['agent']['kwargs']['arm'] == 'single_turn':
        trial.task.instruction = binding['public_contract']['instruction']
    result = await trial.run()
    ep_path = trial.paths.agent_dir.with_name('agent-private') / 'episode.json'
    episode = read(ep_path) if ep_path.exists() else {}
    row = {'task_id': binding['task_id'], 'arm': config['agent']['kwargs']['arm'],
           'construction_status': binding['status'], 'runtime_controls_validated': False,
           'episode': episode, 'terminal': episode.get('terminal', 'setup_error'),
           'original_reward': (result.verifier_result.rewards or {}).get('reward') if result.verifier_result else None,
           'exception': result.exception_info.model_dump(mode='json') if result.exception_info else None,
           'required_requirement_ids': [] if config['agent']['kwargs']['arm'] == 'vague_no_evolution' else
                                       [r['id'] for r in binding['proposal']['requirements']],
           'persona_check': {'status': 'unknown', 'checks': {}}}
    row['gpu_allocation'] = getattr(trial.agent_environment, 'gpu_receipt', None)
    row['requirement_review'] = episode.get('requirement_loop')
    cutoff = trial.paths.agent_dir / 'cutoff-artifacts.json'
    before = await asyncio.to_thread(tree_index, trial.paths.artifacts_dir)
    row['artifact_integrity'] = cutoff.exists() and read(cutoff) == before
    if row['artifact_integrity']:
        row['persona_check'] = await check_persona(trial, binding,
            Path(config['agent']['kwargs']['binding_path']).with_name('checker.py'))
        row['artifact_integrity'] = before == await asyncio.to_thread(tree_index, trial.paths.artifacts_dir)
    row['benchmark_outcome'] = benchmark_outcome(row)
    if not row['artifact_integrity']:
        row['benchmark_outcome'] = {'status': 'invalid', 'score': None, 'reason': 'Artifact integrity unavailable'}
    if experience and row['arm'] != 'single_turn' and episode:
        from personalization import evaluate
        row['personalization'] = await evaluate(binding, episode, trial.paths.trial_dir / 'persona-experience.json')
    # This is a diagnostic run. Qualification requires separate real positive
    # and per-requirement negative controls; a successful episode is insufficient.
    row['qualified_benchmark_result'] = False
    write(trial.paths.trial_dir / 'persona-result.json', row)
    print(json.dumps({'trial_dir': str(trial.paths.trial_dir), 'original_reward': row['original_reward'],
                      'persona_check': row['persona_check']['status'], 'outcome': row['benchmark_outcome']}, indent=2))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--arm', choices=['persona', 'single_turn', 'persona_static', 'vague_no_evolution'], default='persona')
    parser.add_argument('--output', default=str(ROOT / 'runs'))
    parser.add_argument('--dry-run', action='store_true', help='Validate configuration without model or Docker calls')
    parser.add_argument('--experience', action='store_true', help='Run the independent persona-experience judge after the episode')
    args = parser.parse_args()
    tasks = {t['task_id'] for t in read(ROOT / 'inventory.json')['cases']}
    if args.task not in tasks:
        parser.error('Unknown packaged task')
    config, binding = configuration(args.task, args.output, args.arm, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(config, indent=2))
        return
    asyncio.run(execute(config, binding, args.experience))


if __name__ == '__main__':
    main()
