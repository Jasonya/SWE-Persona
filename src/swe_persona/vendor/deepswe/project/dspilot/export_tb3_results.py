"""Export recorded DeepSWE results with separate judgments, reward and outcome.

This is a file adapter, not a TB3 evaluator. It makes no API or Docker calls.
Input: runs/<batch>/manifest.json with jobs[], plus the referenced run directories.
Output must be new; source files, scores and frozen run identities are never edited.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import tomllib

VERSION = 'deepswe-result-adapter-v3'
OUTCOME_POLICY = 'deepswe-staged-outcome-v1'
ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = {
    'target_layout': 'TB3 v6 result delivery layout',
    'source_semantics': 'historical DeepSWE staged experiment',
    'tb3_protocol_executed': False,
    'tb3_schema_conformance': 'not_certified; adapter extension schema',
    'benchmark_outcome_scope': 'DeepSWE staged evaluation; not TB3 acceptance',
    'reason': 'Recorded DeepSWE judgments and scores only. Export does not execute a judge, certify objective Persona checks, or infer TB3 four-level verdicts.',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dumps(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', value):
        raise ValueError(f'Unsafe or missing identifier: {value!r}')
    return value


def outcome(result, official=None):
    """Classify legacy records only; never infer a TB3 benchmark outcome."""
    if result.get('error') or result.get('final_error'):
        return 'infrastructure_error', 'Recorded runtime, model, protocol or evaluation exception'
    integrity = result.get('integrity') or {}
    reasons = [str(reason) for c in integrity.get('checks', {}).values() for reason in c.get('reasons', [])]
    if (official or {}).get('code') == 124 and result.get('reward') is None:
        return 'infrastructure_error', 'Official evaluation timed out without a reward'
    if result.get('reward') is None and any('Missing or malformed official reward' in r for r in reasons):
        return 'infrastructure_error', 'Official evaluator did not produce a usable reward'
    if result.get('evaluation_valid') is not True or result.get('original_report_eligible') is not True:
        return 'invalid', 'Historical validity or reporting eligibility check failed'
    if result.get('arm') == 'pivot' and result.get('persona_evaluation_exclusion'):
        return 'invalid', 'Historical Persona reporting exclusion'
    if result.get('coding_time_budget_exhausted') is True:
        return 'fail', 'Cumulative coding time budget exhausted; original scores preserved'
    key = 'joint_pass' if result.get('arm') == 'pivot' else 'base_pass'
    if result.get(key) is True:
        return 'pass', f'Historical {key}=true; not TB3 acceptance'
    if result.get(key) is False:
        return 'fail', f'Historical {key}=false; not TB3 acceptance'
    return 'invalid', f'Historical {key} is unknown'


def benchmark_assessment(result, official=None):
    """Recompute the recorded DeepSWE aggregate, with validity before capability.

    Reward/base_pass is the original task component. Pivot additionally uses the
    recorded bound-Persona component (which may include LLM checks). Missing
    components remain unknown. joint_pass is checked for consistency, not used
    as the definition. UX and TB3 protocol conformance do not participate.
    """
    legacy, legacy_reason = outcome(result, official)
    inputs = {k: result.get(k) for k in (
        'arm', 'base_pass', 'persona_pass', 'joint_pass', 'evaluation_valid',
        'original_evaluation_valid', 'original_report_eligible',
        'persona_evaluation_valid', 'persona_evaluation_exclusion')}
    inputs.update({k: result[k] for k in ('coding_time_budget_exhausted','coding_budget') if k in result})
    value = {'policy': OUTCOME_POLICY, 'scope': 'DeepSWE staged bound requirements',
             'inputs': inputs, 'ux_affects_outcome': False,
             'independent_objective_persona_acceptance': False}
    def finish(status, reason):
        return {**value, 'status': status, 'reason': reason}
    if legacy == 'infrastructure_error':
        return finish(legacy, legacy_reason)
    if (result.get('evaluation_valid') is not True
            or result.get('original_report_eligible') is not True
            or result.get('original_evaluation_valid') is False):
        return finish('invalid', 'Recorded evaluation validity or original reporting eligibility failed or is missing')
    if result.get('arm') not in ('pivot', 'baseline', 'control'):
        return finish('invalid', 'Unknown experiment arm')
    if result['arm'] == 'pivot' and (result.get('persona_evaluation_valid') is False
                                    or result.get('persona_evaluation_exclusion')):
        return finish('invalid', 'Persona evaluation is invalid or excluded')
    reward = result.get('reward')
    if not isinstance(reward, dict) or not finite_seconds(reward.get('reward')):
        return finish('invalid', 'Original numeric reward is missing; no score inferred')
    base = result.get('base_pass')
    if not isinstance(base, bool) or base != (reward['reward'] == 1):
        return finish('invalid', 'Original reward and base_pass are missing or inconsistent')
    if result.get('coding_time_budget_exhausted') is True:
        return finish('fail', 'Cumulative coding time budget exhausted; original scores preserved')
    passed = base
    if result['arm'] == 'pivot':
        persona = result.get('persona_pass')
        if persona is not None and not isinstance(persona, bool):
            return finish('invalid', 'Malformed Persona pass component')
        # Preserve the staged runner's False-and-unknown short circuit.
        passed = base and persona
        if passed is None:
            return finish('invalid', 'Persona outcome is unknown; no pass/fail inferred')
        joint = result.get('joint_pass')
        if joint is not None and (not isinstance(joint, bool) or joint != passed):
            return finish('invalid', 'Recorded joint_pass contradicts the original and Persona components')
    return finish('pass' if passed else 'fail',
                  'Valid original task and bound Persona components both pass' if passed and result['arm'] == 'pivot'
                  else 'Valid original task component passes' if passed
                  else 'Valid evaluation with a failing required component')


def assessment_fields(raw):
    """Describe recorded execution, never infer an objective pass from no votes."""
    source = raw.get('assessment_kind') if raw is not None else None
    explicit = {'programmatic_trusted_probe':'programmatic', 'programmatic_precheck':'programmatic',
                'programmatic':'programmatic', 'llm':'llm', 'mixed':'mixed',
                'mixed_programmatic_and_llm':'mixed'}.get(source)
    votes = bool(raw and raw.get('votes'))
    if votes:
        kind = 'mixed' if explicit in ('programmatic','mixed') or 'errors' in raw else 'llm'
        basis = 'recorded_votes_and_prechecks' if kind == 'mixed' else 'recorded_llm_votes'
    else:
        kind = explicit or 'unknown'
        basis = 'explicit_recorded_kind' if explicit else 'not_recorded' if raw is None else 'insufficient_source_metadata'
    return {'assessment_kind':kind, 'source_assessment_kind':source, 'assessment_basis':basis}


def requirement_records(arm, plan, stage, checks, final_states, online, references, *, legacy_assessment=False):
    """Keep online judgments distinct from post-run mixed checker results."""
    lifecycle = plan.get('requirements', {})
    states = (stage or {}).get('requirements', {})
    ids = sorted(set(lifecycle) | set(states) | set(checks or {}) | set(final_states)
                 | {key for _, record in online for key in record}) if arm == 'pivot' else []
    def pointer(key):
        return '/' + key.replace('~', '~0').replace('/', '~1')
    judgments = []
    final = []
    for key in ids:
        history = []
        for name, record in online:
            if key in record:
                history.append({'turn': int(re.search(r'checks-turn-(\d+)\.json', name).group(1)),
                                'status': record[key].get('status'),
                                **({'verdict': record[key]['verdict'],
                                    'criteria': record[key]['criteria'],
                                    'reason': record[key]['reason'],
                                    'feedback': record[key]['feedback'],
                                    'protocol': record[key]['protocol']}
                                   if record[key].get('protocol') in ('deepswe-visible-requirement-judge-v1',
                                       'deepswe-visible-requirement-judge-v2', 'deepswe-visible-requirement-judge-v3') else {}),
                                'source': references[name], 'json_pointer': pointer(key)})
        judgments.append({'requirement_id': key, 'stage': lifecycle.get(key, {}).get('stage'),
                          'last_online_status': history[-1]['status'] if history else None,
                          **({'last_online_verdict': history[-1]['verdict']} if history and 'verdict' in history[-1] else {}),
                          'controller_status': states.get(key), 'judgments': history})
        raw = (checks or {}).get(key)
        final.append({'requirement_id': key, 'status': raw.get('status') if raw is not None else None,
                      'record_present': raw is not None, 'final_lifecycle_status': final_states.get(key),
                      'source': references.get('checks.json') if raw is not None else None,
                      'json_pointer': pointer(key) if raw is not None else None,
                      **({'assessment_kind': 'recorded_deepswe_mixed_checker'} if legacy_assessment else assessment_fields(raw)),
                      **({'programmatic_checks': raw['programmatic_checks']} if raw is not None and 'programmatic_checks' in raw else {}),
                      'independent_objective_check_certified': False})
    applicable = arm == 'pivot'
    return ({'status': 'not_applicable' if not applicable else 'recorded' if online or states else 'not_recorded',
             'semantics': 'Recorded DeepSWE online judgments and controller state. Four-level fields are included only when actually recorded by the visible-message judge; older records retain their legacy semantics.',
             'source_stage_state': references.get('stage-state.json'), 'requirements': judgments,
             'tb3_online': None},
            {'status': 'not_applicable' if not applicable else 'recorded' if checks is not None else 'not_recorded',
             'semantics': 'Recorded final DeepSWE checks; may include LLM judgment and programmatic checks',
             'source': references.get('checks.json'), 'requirements': final,
             'independent_objective_check_certified': False})


class Package:
    def __init__(self, root, output):
        self.root, self.output = root, output
        self.sources = {}

    def source_bytes(self, path):
        path = Path(path)
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            raise ValueError('Source outside module root')
        if any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(self.root)):
            raise ValueError('Symlink input is not accepted: ' + str(path))
        data = path.read_bytes()
        rel = path.relative_to(self.root).as_posix()
        h = sha(data)
        if rel in self.sources and self.sources[rel]['sha256'] != h:
            raise ValueError('Source changed while exporting: ' + rel)
        self.sources.setdefault(rel, {'sha256': h, 'bytes': len(data), 'copies': []})
        return data

    def read(self, path, default=None):
        if not path.exists():
            return default
        return json.loads(self.source_bytes(path))

    def write(self, rel, value):
        dest = self.output / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(dumps(value))

    def copy(self, source, rel):
        if not source.exists():
            return None
        data = self.source_bytes(source)
        dest = self.output / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.sources[source.relative_to(self.root).as_posix()]['copies'].append(Path(rel).as_posix())
        return {'path': Path(rel).as_posix(), 'sha256': sha(data), 'bytes': len(data)}

    def verify_sources(self):
        for rel, record in list(self.sources.items()):
            if sha(self.source_bytes(self.root / rel)) != record['sha256']:
                raise ValueError('Source changed while exporting: ' + rel)


def finite_seconds(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def timing_evidence(api_path, actions_path, result):
    """API + requested tool time, with missing components left unknown."""
    events = None
    if api_path.is_file():
        events = [json.loads(line) for line in api_path.read_text().splitlines() if line.strip()]
    actions = json.loads(actions_path.read_text()) if actions_path.is_file() else None
    agents = [e for e in events if e.get('role') == 'agent'] if events is not None else None
    api_seconds = sum(e['seconds'] for e in agents) if agents is not None and all(finite_seconds(e.get('seconds')) for e in agents) else None
    totals, missing = Counter(), Counter()
    if actions is not None:
        for a in actions:
            kind = (a.get('action') or {}).get('action', 'unknown')
            record = a.get('result')
            if isinstance(record, str):
                try:
                    record = json.loads(record)
                except ValueError:
                    record = None
            seconds = record.get('seconds') if isinstance(record, dict) else None
            if kind not in ('shell', 'validate') or not finite_seconds(seconds):
                missing[kind] += 1
            else:
                totals[kind] += seconds
    recorded = ((result or {}).get('role_costs') or {}).get('agent', {}).get('seconds')
    if recorded is not None and api_seconds is not None and abs(recorded - api_seconds) > .001:
        raise ValueError('Recorded agent time differs from API evidence')
    complete = api_seconds is not None and actions is not None and not missing
    return {'unit': 'seconds',
        **({'active_coding_budget':result['coding_budget']} if (result or {}).get('coding_budget') is not None else {}),
        'agent_api_seconds': api_seconds,
        'recorded_agent_api_seconds': recorded,
        'requested_shell_seconds': totals['shell'] if actions is not None and not missing['shell'] else None,
        'requested_validation_seconds': totals['validate'] if actions is not None and not missing['validate'] else None,
        'coding_accounted_seconds': api_seconds + sum(totals.values()) if complete else None,
        'action_count': len(actions) if actions is not None else None,
        'missing_or_unsupported_action_timings': dict(missing),
        'scope': 'Final selected attempt only: coding-model API attempts plus requested shell and public-validation durations. Excludes simulator, Persona checks, final grading, unmeasured overhead and archived interrupted attempts. Not complete wall-clock coding time.',
        'complete_for_this_definition': complete}


def export_trial(pkg, job, batch):
    identity = {k: identifier(job.get(k)) for k in ('task_id', 'persona_id', 'arm', 'run_id')}
    trial_name = identity['persona_id'] + '--' + identity['arm'] + '--' + sha(identity['run_id'].encode())[:12]
    trial = Path('trials') / identity['task_id'] / trial_name
    source = pkg.root / 'runs' / identity['run_id']
    result = pkg.read(source / 'result.json')
    config = pkg.read(source / 'config.json', {})
    for data in [result, config]:
        if data:
            for k, expected in identity.items():
                if (k in data or data is result) and data.get(k) != expected:
                    raise ValueError(f'Identity mismatch: {identity["run_id"]}: {k}')
    attempt = pkg.read(batch / 'attempts' / (identifier(job['id']) + '.json'), {})
    entry = {**identity, 'pair_id': identity['task_id'] + '--' + identity['persona_id'],
        'source_ordinal': job.get('ordinal'), 'trial_name': trial_name, 'trial_directory': trial.as_posix(),
        'benchmark_outcome': None, 'status_scope': 'DeepSWE staged evaluation',
        'source_attempt_status': attempt.get('status'), 'has_result': result is not None}
    references = {}
    # Only allowlisted artifacts are copied, never models.env or complete run trees.
    mapping = {
        'result.json': 'agent-private/audit/source-result.json',
        'config.json': 'agent-private/audit/source-config.json',
        'checks.json': 'agent-private/requirement-judge-audit/final-checks.json',
        'stage-state.json': 'agent-private/audit/stage-state.json',
        'turns.json': 'agent/conversation.json',
        'requirement-disclosures.json': 'agent-private/requirement-judge-audit/disclosures.json',
        'online-rubrics.json': 'agent-private/requirement-judge-audit/rubrics.json',
        'online-judge-records.json': 'agent-private/requirement-judge-audit/online-judge-records.json',
        'actions.json': 'agent/actions.json',
        'api.jsonl': 'agent-private/audit/api.jsonl',
        'final-review-api.jsonl': 'agent-private/audit/final-review-api.jsonl',
        'final-review-status.json': 'agent-private/audit/final-review-status.json',
        'simulator-dialogue.json': 'agent-private/audit/simulator-dialogue.json',
        'judge-error-audit.json': 'agent-private/requirement-judge-audit/judge-error-audit.json',
        'final-judge-error-audit.json': 'agent-private/requirement-judge-audit/final-judge-error-audit.json',
        'final-snapshot.json': 'agent-private/audit/final-snapshot.json',
        'early-evidence.json': 'agent-private/audit/early-evidence.json',
        'model.patch': 'agent/model.patch',
        'official/result.json': 'agent-private/audit/official/result.json',
        'official/integrity.json': 'agent-private/audit/official/integrity.json',
        'official/verifier/reward.json': 'agent-private/audit/official/reward.json',
        'official/verifier/ctrf.json': 'agent-private/audit/official/ctrf.json',
        'delivery/instruction.md': 'agent-private/audit/delivery/instruction.md',
        'delivery/task-blur.json': 'agent-private/audit/delivery/task-blur.json',
        'delivery/blur-receipt.json': 'agent-private/audit/delivery/blur-receipt.json',
        'delivery/opening-receipt.json': 'agent-private/audit/delivery/opening-receipt.json',
        'persona-experience.json': 'persona-experience.json',
        'experience-rubric.json': 'experience-rubric.json',
        'ux-audit/api.jsonl': 'ux-audit/api.jsonl',
        'ux-audit/judge.json': 'ux-audit/judge.json',
    }
    for pattern, dest in [('online-judge-attempt-*.json', 'agent-private/requirement-judge-audit'),
                          ('checks-turn-*.json', 'agent-private/requirement-judge-audit'),
                          ('controller-turn-*.json', 'agent-private/audit/controller'),
                          ('source-scope-turn-*.json', 'agent-private/audit/source-scope'),
                          ('simulator-turn-*.json', 'agent-private/audit/simulator')]:
        for path in sorted(source.glob(pattern)):
            mapping[path.name] = dest + '/' + path.name
    for name, dest in mapping.items():
        record = pkg.copy(source / name, trial / dest)
        if record:
            references[name] = record
    # The actual Pier bundle is a sibling of the staged run, not source-config.json.
    # Copy only known configuration evidence, never whole task/trial directories.
    runtime_files = {}
    for name in ('task.toml', 'adapter-overrides.json'):
        ref = pkg.copy(pkg.root / 'runs/pier-tasks' / identity['run_id'] / name,
                       trial / 'agent-private/audit/pier-runtime' / name)
        if ref:
            runtime_files[name] = ref
            references['pier-runtime/' + name] = ref
    runtime_configuration = {'status': 'recorded' if len(runtime_files) == 2 else 'partial' if runtime_files else 'not_recorded',
        'files': runtime_files, 'missing': [n for n in ('task.toml', 'adapter-overrides.json') if n not in runtime_files],
        'scope': 'Actual retained Pier task bundle, copied verbatim; never reconstructed from source task or current defaults',
        'paths_relative_to': 'package_root'}
    verifier_policy = pkg.copy(pkg.root / 'runs/pier-trials' / identity['run_id'] / 'verifier-policy.json',
                               trial / 'agent-private/audit/pier-runtime/verifier-policy.json')
    if verifier_policy:
        runtime_configuration['verifier_policy'] = verifier_policy
        references['pier-runtime/verifier-policy.json'] = verifier_policy
    # Failed/cancelled verification may never reach import_grade. Preserve the
    # native evidence directly rather than inventing official reward records.
    native_evidence = {}
    for name in ('result.json', 'exception.txt', 'native-verifier-executions.jsonl',
                 'verifier/test-stdout.txt', 'verifier/run.log'):
        ref = pkg.copy(pkg.root / 'runs/pier-trials' / identity['run_id'] / name,
                       trial / 'agent-private/audit/pier-native' / name)
        if ref:
            native_evidence[name] = ref
            references['pier-native/' + name] = ref
    runtime_configuration['native_trial_evidence'] = native_evidence
    entry['runtime_configuration'] = runtime_configuration
    pkg.write(trial / 'agent-private/audit/pier-runtime/index.json', runtime_configuration)
    if attempt:
        references['source-attempt'] = pkg.copy(batch / 'attempts' / (job['id'] + '.json'), trial / 'agent-private/audit/source-attempt.json')
    official = pkg.read(source / 'official/result.json', {})
    assessment = None
    legacy_status = legacy_reason = None
    if result is not None:
        legacy_status, legacy_reason = outcome(result, official)
        assessment = benchmark_assessment(result, official)
        entry.update(status=assessment['status'], status_reason=assessment['reason'],
                     benchmark_outcome=assessment['status'], benchmark_outcome_policy=OUTCOME_POLICY)
    elif attempt.get('status') in ('freeze_failed', 'execution_failed', 'interrupted', 'finished'):
        entry.update(status='infrastructure_error', status_reason='Recorded interrupted/failed execution has no final result; no later-stage result fabricated')
    else:
        entry.update(status='running' if attempt.get('status') in ('running', 'freezing') else 'pending',
                     status_reason='Source scheduler state only; exporter does not infer liveness')
    timings = timing_evidence(pkg.output / trial / mapping['api.jsonl'], pkg.output / trial / mapping['actions.json'], result)
    entry['coding_time'] = timings
    pkg.write(trial / 'agent/trace-index.json', {'schema_version': VERSION, **identity,
        'artifact_paths_relative_to': 'package_root', 'artifacts': references, 'coding_time': timings,
        'format': 'Original DeepSWE action/message records, not synthesized ATIF',
        'private_evidence': 'Simulator and judge sidecars stay in agent-private; do not deliver them to the coding agent.'})
    entry['trace_index'] = (trial / 'agent/trace-index.json').as_posix()
    checks = pkg.read(source / 'checks.json')
    plan = config.get('plan') or {}
    if checks is not None:
        if not isinstance(checks, dict):
            raise ValueError('checks.json must be a requirement mapping')
        states = (result or {}).get('evolved') or {}
        requirement_ids = sorted(set(checks) | set(states) | (set(plan.get('requirements', {})) if identity['arm'] == 'pivot' else set()))
        converted = []
        for key in requirement_ids:
            raw = checks.get(key)
            converted.append({'requirement_id': key,
                'legacy_status': raw.get('status') if raw is not None else states.get(key),
                'record_present': raw is not None,
                'tb3_verdict': None, 'objective_verdict': None,
                **assessment_fields(raw),
                'independent_objective_check_certified': False,
                'legacy_check': raw, 'lifecycle': plan.get('requirements', {}).get(key),
                'tb3_criteria': None,
                'citation_policy': 'Original evidence_id/reference_id citations retained; no event_id or TB3 visible-message evidence inferred.'})
        rel = trial / 'persona-check.json'
        pkg.write(rel, {'schema_version': VERSION, **identity, 'compatibility': BOUNDARY,
            'assessment_kind': 'per_requirement_recorded_source', 'requirements': converted,
            'source': references.get('checks.json'), 'objective_checker_executed_by_exporter': False})
        entry['persona_check'] = rel.as_posix()
    stage = pkg.read(source / 'stage-state.json')
    online = [(name, pkg.read(source / name)) for name in sorted(
        (k for k in references if re.fullmatch(r'checks-turn-\d+\.json', k)),
        key=lambda k: int(re.search(r'\d+', k).group()))]
    if any(not isinstance(record, dict) for _, record in online):
        raise ValueError('Online checks must be requirement mappings')
    # Keep both online saved stage state and the final result's post-review state.
    # No new four-level verdicts, criteria, timestamps or event IDs are invented.
    if any(k in references for k in ('stage-state.json', 'turns.json', 'simulator-dialogue.json')):
        episode = {'schema_version': VERSION, **identity, 'compatibility': BOUNDARY,
            'protocol_version': (result or {}).get('protocol_version') or config.get('protocol_version'),
            'budgets': config.get('budgets') or plan.get('budgets'),
            'recorded_stage_state': stage,
            'final_result_stage': (result or {}).get('stage'),
            'disclosure_and_progression_events': (stage or {}).get('events'),
            'visible_conversation': references.get('turns.json'),
            'simulator_decisions_and_repairs': references.get('simulator-dialogue.json'),
            'controller_records': [v for k, v in references.items() if k.startswith('controller-turn-')],
            'source_scope_checks': [v for k, v in references.items() if k.startswith('source-scope-turn-')],
            'requirement_checks': [v for k, v in references.items() if k.startswith('checks-turn-') or k == 'checks.json'],
            'timings': (result or {}).get('timings'), 'end_reason': (result or {}).get('end_reason'),
            'source_paths_relative_to': 'package_root', 'tb3_requirement_judgments': None,
            'requirement_disclosures': references.get('requirement-disclosures.json'),
            'online_judge_records': references.get('online-judge-records.json'),
            'online_rubrics': references.get('online-rubrics.json')}
        pkg.write(trial / 'agent-private/episode.json', episode)
        entry['episode'] = (trial / 'agent-private/episode.json').as_posix()
    audit_files = [v for k, v in references.items() if k.startswith(('checks-turn-', 'online-judge-attempt-')) or k in ('checks.json', 'judge-error-audit.json', 'final-judge-error-audit.json', 'online-judge-records.json', 'online-rubrics.json', 'requirement-disclosures.json')]
    if audit_files:
        pkg.write(trial / 'agent-private/requirement-judge-audit/index.json', {
            'schema_version': VERSION, 'artifacts': audit_files, 'paths_relative_to': 'package_root',
            'semantics': 'Source online judgments and separate final checker records. The protocol on each online record determines its semantics; historical results are not converted.'})
    if result is not None:
        rel = trial / 'persona-result.json'
        ux = pkg.read(source / 'persona-experience.json')
        experience = {'status': 'not_recorded', 'result': None, 'satisfaction': None,
                      'rubric': references.get('experience-rubric.json')}
        if ux is not None:
            if not isinstance(ux, dict) or ux.get('status') not in ('not_requested', 'scored', 'unsupported', 'infrastructure_error'):
                raise ValueError('Invalid recorded experience status')
            experience.update(status=ux['status'], result=references['persona-experience.json'],
                satisfaction=ux.get('satisfaction'), assessment_status=(ux.get('assessment') or {}).get('status'),
                model_calls=ux.get('model_calls'), affects_benchmark_outcome=False)
        entry['experience'] = experience
        old_fields = ('base_pass', 'persona_pass', 'joint_pass', 'evaluation_valid', 'original_evaluation_valid',
            'original_report_eligible', 'persona_evaluation_valid', 'persona_evaluation_exclusion',
            'original_exclusion_reason', 'end_reason', 'error', 'final_error', 'evolved', 'persona_measurement',
            'online_final_disagreement', 'integrity', 'task_workflow_pass')
        judgments, checker_results = requirement_records(
            identity['arm'], plan, stage, checks, result.get('evolved') or {}, online, references)
        pkg.write(rel, {'schema_version': VERSION, **identity, 'pair_id': entry['pair_id'],
            'source_ordinal': entry['source_ordinal'], 'compatibility': BOUNDARY,
            'source_protocol_version': result.get('protocol_version'),
            'benchmark_outcome': assessment['status'], 'benchmark_outcome_status': 'derived_from_recorded_deepswe_evaluation',
            'benchmark_assessment': assessment,
            'legacy_outcome': {'status': legacy_status, 'reason': legacy_reason,
                'policy': 'Pivot uses original joint_pass; other arms use base_pass, after validity/error checks'},
            'original_reward': result.get('reward'),
            'original_verifier': {'result': references.get('official/result.json'),
                'integrity': references.get('official/integrity.json'), 'reward': references.get('official/verifier/reward.json')},
            'legacy_assessment': {k: result.get(k) for k in old_fields},
            'requirement_judgments': judgments,
            'checker_results': {**checker_results, 'detail_file': entry.get('persona_check')},
            'objective_persona_checker': {'status': 'not_certified', 'result': None},
            'experience': experience,
            'runtime_configuration': runtime_configuration,
            'timings': result.get('timings'), 'role_costs': result.get('role_costs'),
            'coding_time': timings, 'evidence': references, 'paths_relative_to': 'package_root'})
        entry['persona_result'] = rel.as_posix()
    entry['execution_identity'] = {'models': config.get('models'),
        'coding_backend': config.get('coding_backend'),
        'runtime_backend': (config.get('execution_admission') or {}).get('runtime_backend'),
        'source_config': references.get('config.json'),
        'status': 'recorded' if config.get('models') else 'not_recorded'}
    return entry


def validate_package(output):
    """Offline checks for this adapter's schema and hashes, not TB3 certification."""
    output = Path(output).resolve()
    record = json.loads((output / 'export-manifest.json').read_text())
    for rel, expected in record['files'].items():
        path = output / rel
        if not path.resolve().is_relative_to(output) or path.is_symlink() or sha(path.read_bytes()) != expected:
            raise ValueError('Export checksum mismatch: ' + rel)
    results = json.loads((output / 'results.json').read_text())
    status = json.loads((output / 'status.json').read_text())
    entries = results['entries']
    if len({e['run_id'] for e in entries}) != len(entries):
        raise ValueError('Duplicate run IDs in export')
    if len({e['trial_directory'] for e in entries}) != len(entries):
        raise ValueError('Trial directory collision')
    if status['counts'] != dict(Counter(e['status'] for e in entries)):
        raise ValueError('Status counts disagree with result index')
    def artifact(ref):
        if not ref:
            return None
        path = output / ref['path']
        if (not path.resolve().is_relative_to(output) or path.is_symlink()
                or record['files'].get(ref['path']) != ref['sha256']):
            raise ValueError('Result evidence reference mismatch')
        return json.loads(path.read_text())
    for entry in entries:
        runtime = entry.get('runtime_configuration')
        if runtime is not None:
            artifact(runtime.get('verifier_policy'))
            files = runtime['files']
            names = {'task.toml', 'adapter-overrides.json'}
            expected_status = 'recorded' if set(files) == names else 'partial' if files else 'not_recorded'
            if (not set(files) <= names or runtime['status'] != expected_status
                    or set(runtime['missing']) != names - set(files)):
                raise ValueError('Runtime configuration completeness mismatch')
            prefix = entry['trial_directory'] + '/agent-private/audit/pier-runtime/'
            if json.loads((output / (prefix + 'index.json')).read_text()) != runtime:
                raise ValueError('Runtime configuration index mismatch')
            for name, ref in files.items():
                if ref['path'] != prefix + name or record['files'].get(ref['path']) != ref['sha256']:
                    raise ValueError('Runtime configuration evidence mismatch')
                content = (output / ref['path']).read_text()
                (tomllib.loads if name.endswith('.toml') else json.loads)(content)
        for key in ('persona_result', 'persona_check', 'episode', 'trace_index'):
            if key not in entry:
                continue
            value = json.loads((output / entry[key]).read_text())
            for name in ('task_id', 'persona_id', 'arm', 'run_id'):
                if value[name] != entry[name]:
                    raise ValueError('Export identity mismatch')
            if key == 'persona_check' and value.get('schema_version') == VERSION:
                raw_checks = artifact(value.get('source')) or {}
                for row in value['requirements']:
                    raw = raw_checks.get(row['requirement_id'])
                    if row.get('legacy_check') != raw or any(row.get(k) != v for k,v in assessment_fields(raw).items()):
                        raise ValueError('Persona checker assessment source differs from evidence')
            if key == 'persona_result':
                if runtime is not None and value.get('runtime_configuration') != runtime:
                    raise ValueError('Runtime configuration summary mismatch')
                if value['compatibility']['tb3_protocol_executed']:
                    raise ValueError('Historical data relabeled as TB3 evaluation')
                if value.get('schema_version') in (VERSION, 'deepswe-result-adapter-v2'):
                    refs = value['evidence']
                    source = artifact(refs['result.json'])
                    config = artifact(refs.get('config.json')) or {}
                    official = artifact(refs.get('official/result.json'))
                    expected = benchmark_assessment(source, official)
                    if (value['benchmark_assessment'] != expected
                            or value['benchmark_outcome'] != expected['status']
                            or entry['benchmark_outcome'] != expected['status']
                            or entry['status'] != expected['status']
                            or entry.get('benchmark_outcome_policy') != OUTCOME_POLICY):
                        raise ValueError('DeepSWE benchmark outcome differs from source evidence')
                    if value['original_reward'] != source.get('reward'):
                        raise ValueError('Original reward differs from source evidence')
                    online = [(name, artifact(refs[name])) for name in sorted(
                        (n for n in refs if re.fullmatch(r'checks-turn-\d+\.json', n)),
                        key=lambda n: int(re.search(r'\d+', n).group()))]
                    judgments, checker = requirement_records(entry['arm'], config.get('plan') or {},
                        artifact(refs.get('stage-state.json')), artifact(refs.get('checks.json')),
                        source.get('evolved') or {}, online, refs,
                        legacy_assessment=value.get('schema_version')=='deepswe-result-adapter-v2')
                    if (value['requirement_judgments'] != judgments or value['checker_results'] != {
                            **checker, 'detail_file': entry.get('persona_check')}):
                        raise ValueError('Requirement/checker summaries differ from source evidence')
                elif value.get('schema_version') == 'deepswe-tb3-result-adapter-v1':
                    if value['benchmark_outcome'] is not None:
                        raise ValueError('Historical data relabeled as TB3 evaluation')
                else:
                    raise ValueError('Unsupported result schema version')
            if key == 'persona_result' and value.get('experience', {}).get('result'):
                ux_ref = value['experience']['result']
                if record['files'].get(ux_ref['path']) != ux_ref['sha256']:
                    raise ValueError('Experience reference mismatch')
                ux = json.loads((output / ux_ref['path']).read_text())
                if value['experience']['status'] != ux['status'] or value['experience']['satisfaction'] != ux.get('satisfaction'):
                    raise ValueError('Experience summary differs from recorded UX result')
    captured = output / 'provenance/execution-records.json'
    if captured.exists():
        for rel, ref in json.loads(captured.read_text())['records'].items():
            if ref['path'] != rel or record['files'].get(rel) != ref['sha256']:
                raise ValueError('Execution sidecar reference mismatch')
    provenance = json.loads((output / 'provenance/source-files.json').read_text())
    for source in provenance.values():
        for rel in source['copies']:
            if sha((output / rel).read_bytes()) != source['sha256']:
                raise ValueError('Source-copy mismatch: ' + rel)
    return {'schema_version': record['schema_version'], 'trials': len(entries), 'files_verified': len(record['files']),
            'counts': status['counts'], 'adapter_validation': 'passed', 'tb3_protocol_compliance': False}


def export_batch(root, batch_dir, output, *, work_dir=None):
    root, batch_dir, output = Path(root).resolve(), Path(batch_dir).resolve(), Path(output).resolve()
    if not batch_dir.is_relative_to(root / 'runs'):
        raise ValueError('Batch must be under module runs/')
    if output.exists() or output.is_relative_to(root / 'runs') or root.is_relative_to(output):
        raise ValueError('Use a new output directory outside runs/; no overwrite of source or existing export')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=output.parent))
    try:
        pkg = Package(root, temporary)
        manifest = pkg.read(batch_dir / 'manifest.json')
        if not isinstance(manifest, dict) or not isinstance(manifest.get('jobs'), list) or not manifest['jobs']:
            raise ValueError('Expected manifest.json with a nonempty jobs[] list')
        jobs = manifest['jobs']
        for field in ('id', 'run_id'):
            if len({identifier(j.get(field)) for j in jobs}) != len(jobs):
                raise ValueError('Duplicate job/run identity: ' + field)
        manifest_ref = pkg.copy(batch_dir / 'manifest.json', 'provenance/source-batch-manifest.json')
        summary_ref = pkg.copy(root / 'log' / batch_dir.name / 'summary.json', 'provenance/source-summary.json')
        preflight = {}
        for name in ('preparation.json', 'environment-provenance.json'):
            value = pkg.copy(batch_dir / name, Path('provenance') / name)
            if value:
                preflight[name] = value
        for i, rel in enumerate(manifest.get('recovery_history', []), 1):
            pkg.copy(root / rel, Path('provenance') / f'recovery-{i}.json')
        entries = [export_trial(pkg, job, batch_dir) for job in jobs]
        from .delivery_records import collect
        execution = collect(work_dir, pkg, jobs)
        records = execution['records']
        counts = dict(Counter(e['status'] for e in entries))
        stamp = datetime.now(timezone.utc).isoformat()
        pkg.write('run-plan.json', {'schema_version': VERSION, 'batch_id': manifest.get('batch_id', batch_dir.name),
            'compatibility': BOUNDARY, 'models': manifest.get('models'), 'backend': manifest.get('backend'),
            'source_commit': manifest.get('source_commit'), 'source_manifest': manifest_ref,
            'execution_plan': records.get('provenance/execution/run-plan.json'),
            'execution_source_pins': records.get('provenance/execution/source-pins.json'),
            'trial_execution_identities': [{'run_id': e['run_id'], **e['execution_identity']} for e in entries],
            'source_summary': summary_ref, 'source_fingerprints': manifest.get('sources'),
            'fingerprint_note': 'Historical source hashes retained; no assertion that current code equals historical code',
            'trials': [{**j, 'trial_directory': e['trial_directory']} for j, e in zip(jobs, entries)]})
        pkg.write('preflight.json', {'schema_version': VERSION, 'export_only': True,
            'live_infrastructure_check_performed': False, 'model_calls_made': 0,
            'historical_preflight_artifacts': preflight,
            'execution_records_status': execution['status'],
            'execution_preflight': execution.get('categories', {}).get('preflight', []),
            'calibration': {'status': 'recorded' if 'calibration/calibration.json' in records else 'not_recorded',
                'record': records.get('calibration/calibration.json'),
                'audit': execution.get('categories', {}).get('calibration', []),
                'executed_by_adapter': False, 'tb3_six_case_gate_passed': None},
            'missing_execution_records': execution['missing']})
        pkg.write('results.json', {'schema_version': VERSION, 'compatibility': BOUNDARY,
            'entry_count': len(entries), 'status_scope': 'DeepSWE staged evaluation', 'entries': entries})
        pkg.write('status.json', {'schema_version': VERSION, 'exported_at_utc': stamp,
            'phase': 'historical_export', 'live_scheduler_checked': False, 'total': len(entries),
            'completed': sum(e['status'] in ('pass', 'fail', 'invalid', 'infrastructure_error') for e in entries),
            'final_result_count': sum(e['has_result'] for e in entries),
            'counts': counts, 'counts_scope': 'DeepSWE outcomes; completed is recorded terminal entries, not passed tasks',
            'tb3_evaluated': 0, 'tb3_passed': None})
        pkg.verify_sources()
        pkg.write('provenance/source-files.json', {**pkg.sources, **execution['source_files']})
        pkg.write('format-notes.json', {'schema_version': VERSION, 'compatibility': BOUNDARY,
            'benchmark_outcome_policy': {'id': OUTCOME_POLICY,
                'scope': 'DeepSWE staged bound requirements; not independent objective Persona acceptance or TB3',
                'precedence': ['recorded infrastructure errors', 'validity/reporting eligibility and exclusions',
                    'original reward/base_pass consistency', 'original and bound-Persona components'],
                'pivot': 'original base_pass AND recorded persona_pass; original failure short-circuits unknown Persona',
                'baseline_control': 'original base_pass; Persona does not apply',
                'unknown': 'Missing original reward, unknown required pass component or inconsistent recorded joint_pass is invalid',
                'legacy_validity': 'Older missing per-component validity flags defer to evaluation_valid; explicit false is invalid',
                'ux_affects_outcome': False},
            'status_mapping': {'pass': 'Valid original task and applicable bound Persona components pass',
                'fail': 'Valid evaluation with a failing required component',
                'invalid': 'Unusable or excluded evaluation; not a normal task failure',
                'infrastructure_error': 'Recorded runtime/protocol error, verifier missing reward/timeout, or interrupted/failed attempt without final result'},
            'missing_fields': 'Missing TB3 verdicts, criteria, experience scores and evidence event IDs stay null; absent stages do not receive fabricated result files',
            'privacy': 'Only allowlisted run artifacts are copied. No env files. Original runtime redactions are retained; review actual trace content before external publication.',
            'historical_retries': 'Manifest selects the final run ID; interrupted attempts are not double-counted. Recovery receipts retained when available.'})
        files = {p.relative_to(temporary).as_posix(): sha(p.read_bytes()) for p in sorted(temporary.rglob('*')) if p.is_file()}
        pkg.write('export-manifest.json', {'schema_version': VERSION, 'exported_at_utc': stamp,
            'files': files, 'exporter_sha256': sha(Path(__file__).read_bytes())})
        sums = dict(files, **{'export-manifest.json': sha((temporary / 'export-manifest.json').read_bytes())})
        (temporary / 'SHA256SUMS').write_text(''.join(f'{h}  {name}\n' for name, h in sorted(sums.items())))
        validation = validate_package(temporary)
        # Publish only after all checks succeed. Sources remain outside this directory.
        if output.exists():
            raise ValueError('Output appeared during export; refusing overwrite')
        temporary.rename(output)
        return dict(validation, output=str(output))
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('export', help='Create a new historical result export; no experiment execution')
    build.add_argument('--batch-dir', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    check = sub.add_parser('validate', help='Validate the adapter package offline, not TB3 protocol conformance')
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = export_batch(ROOT, args.batch_dir, args.output) if args.command == 'export' else validate_package(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
