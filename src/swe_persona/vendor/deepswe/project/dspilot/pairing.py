"""Candidate generation, isolated calibration, review policy, and publication.

No candidate is imported by normal runs. Publishing never overwrites a binding
and never promotes a candidate gate into a production gate.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from . import specs
from .negative_controls import validate_controls

VERSION = 'pair-candidate-v1'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def folder(candidate):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', candidate):
        raise ValueError('Candidate must be an ID containing letters, numbers, hyphens or underscores')
    path = specs.ROOT / 'data/candidates' / candidate
    # macOS /var and /tmp are system aliases. Canonicalize the trusted root,
    # but continue rejecting symlinks anywhere beneath that root.
    expected = specs.ROOT.resolve() / 'data/candidates' / candidate
    if path.resolve() != expected or path.is_symlink():
        raise ValueError('Candidate paths must not use symlinks')
    return path


def safe_source(path):
    path = Path(path)
    resolved = path.resolve(strict=True)
    resolved.relative_to(specs.ROOT.resolve())
    relative = path.absolute().relative_to(specs.ROOT.absolute())
    expected = specs.ROOT.resolve() / relative
    if not resolved.is_file() or path.is_symlink() or resolved != expected:
        raise ValueError(f'Expected a regular project file: {path}')
    return path


def candidate_data(candidate):
    base = folder(candidate)
    meta = specs._load(base / 'candidate.json')
    if meta.get('candidate_id') != candidate or meta.get('version') != VERSION:
        raise ValueError('Candidate metadata identity/version mismatch')
    return base, meta


def public_context(task_id, persona_id):
    task = specs.load_task(task_id)
    persona = specs.persona_catalog()[persona_id]
    allowed = ('id', 'base_commit', 'source_roots', 'environment', 'protected_files',
               'artifact_files', 'public_validation', 'public_examples')
    prior = next((cell for cell in specs.pairing_plan()
                  if (cell['task_id'], cell['persona_id']) == (task_id, persona_id)), None)
    return {'task': {key: task.raw.get(key) for key in allowed},
            'public_contract': task.contract_path.read_text(), 'persona': persona,
            'prior_pairing_audit': prior,
            'checker_manifest_contracts': {
                'scope.json': {'base_commit': task.raw['base_commit'], 'new_files': ['all changed committed task paths'],
                               'protected_files': [item['path'] for item in task.raw.get('protected_files', [])]},
                'validation.json': {'head_commit': 'actual final commit',
                                    task.raw.get('public_validation', {}).get('id', 'public_validation'):
                                        {'record_id': 'actual validate record ID', 'status': 'passed or failed'},
                                    'hidden_acceptance': {'status': 'not_run', 'reason': 'Unavailable during interaction'}}},
            'workspace_protocol': {
                'evidence_type': 'harness design facts, not a measured task snapshot',
                'isolation': 'Each episode uses a fresh Docker container; the host working tree is not mounted.',
                'setup': 'Box.setup checks out the registered base commit and writes the declared protected fixtures.',
                'ownership': 'Declared protected_files are synthetic user-owned benchmark fixtures; preserve their exact contents.',
                'initial_evidence': 'After setup, before agent actions, run_v3 saves initial-snapshot.json. Calibration also captures and saves an initial snapshot.',
                'snapshot_fields': ['head', 'branch', 'dirty', 'untracked', 'protected_files', 'working_tree_patch'],
                'checker': 'isolated_patch receives the initial snapshot and verifies the base commit and declared protected contents.',
                'limitations': 'Setup does not establish that every image is clean. Agent must inspect git status/diff and preserve pre-existing work. No actual initial snapshot or execution result is asserted at generation time.',
                'sources': ['dspilot/sandbox.py:Box.setup/Box.snapshot', 'dspilot/run_v3.py',
                            'dspilot/calibrate_v3.py:build_fixture', 'dspilot/checks.py:check']}}


GENERATION_INSTRUCTIONS = '''Draft a task/persona binding for human review, not an approved benchmark.
Use ONLY the supplied public task facts and persona templates. Never invent a real
race, production incident, customer tool, filesystem path, permission, or measured result.
Respect prior_pairing_audit; do not reverse not_applicable without concrete new evidence.
Do not use hidden tests, reference solutions, or agent success rates to choose requirements.
If evidence is insufficient, return decision=needs_information or not_applicable and explain.
workspace_protocol describes the real harness lifecycle. Initial snapshots are
collected during calibration and episodes, not during proposal generation. Require
the agent to inspect the actual workspace and protect existing changes; do not
demand a human certify a future container's snapshot merely to draft that check.
Do not claim the workspace is clean without runtime evidence. Genuine missing
task-specific facts must still produce needs_information, never automatic approval.
IMPORTANT: decision=applicable means the public task admits a testable persona
binding, NOT that a future agent has already satisfied it. Patch anchors, final
snapshots and execution records are FUTURE evidence, supplied at their registered
runtime triggers and during calibration. Their absence at generation time alone
is NOT a reason for needs_information. That decision is for missing DESIGN inputs
such as an unspecified allowed scope, unknowable expected behavior, or unavailable
public error scenario. For new-feature tasks, the old missing API is an observable
error path; a public example exercising the missing API is a valid reproduction.
Return binding_json and omitted_templates_json as JSON-encoded objects.
binding_json schema: {schema_version:2, task_id, persona_id, opening, requirements:[],
public_probes:[], calibration:{reference_patch:"solution/solution.patch",
expected_empty:{reward:0}, expected_reference_reward:1, commit_paths:[source roots],
positive_artifact_contents:{artifact_name:content}, positive_turns:[], negative_controls:[]}}.
Each requirement: {id, template, trigger, runtime_trigger, statement, bindings:{},
checker:{kind,parameters:{}}}. template must be an exact selected persona template ID;
trigger is a STRING equal to the template's trigger.event (e.g. "first_patch"),
never the entire trigger object. runtime_trigger is one of
first_plan, first_patch, first_artifact, completion_claim, at most one requirement
per runtime trigger. first_artifact also needs trigger_artifact. Opening must ask
for a plan-only reply before the full issue, consistent with the controller.
Choose only supported checkers: isolated_patch (allowed_paths, protected_paths,
manifest), reviewable_handoff (draft,manifest,public_validation_id,hidden_validation_id),
grounded_rubric (artifacts, required_evidence such as artifact:NAME/diff/transcript),
trusted_probe, grounded_explanation. Last two require public_probe pointing to a
public_probes entry {id,filename,script,command,timeout_seconds}; executable scripts
must run only in the task container and use public APIs. Use require_base_failure=true
where base/reference discrimination is needed. No arbitrary host-side code.
Artifacts must be in task.artifact_files. Derive allowed_paths only from source_roots
(directory roots need root/** glob patterns; an explicit root:*.<extension>
selector must stay exactly as given and matches files only at repository root);
copy protected file paths exactly.
Do not broaden permissions. Positive artifacts are calibration fixtures,
not observed results: label unverified claims and avoid fabricating execution IDs.
The calibrator creates the isolated_patch scope manifest and reviewable_handoff
validation manifest from actual snapshots/validation records. Use DIFFERENT files
for these manifests (scope.json and validation.json). Do not overwrite these
generated manifests with placeholder positive_artifact_contents. The hidden
validation ID is a string such as hidden_acceptance, whose status is not_run;
it is not a claim that hidden tests ran. A public_probe script is the CONTENT
of a runnable script, not a shell command mislabeled as a Python test file.
Use supplied public_examples as disclosed contract-derived cases when present.
For grounded_explanation, put public_probe at REQUIREMENT TOP LEVEL, and use
checker.parameters.artifact="explanation.md". Provide its actual runnable probe.
public_probe is a STRING ID, e.g. "public_probe":"case-v1"; the object containing
id, filename, script, command belongs in the binding's public_probes ARRAY. Never
put that object directly into the requirement. Probe filename is a basename such
as public_case.py; command is python3 /tmp/public_case.py.
Calibration commit_paths must copy source_roots exactly (NO added /** suffixes).
Registered root:*.<extension> selectors are converted to bounded Git pathspecs
by the host calibrator; do not replace them with a whole-repository path.
Calibration needs genuine positive deliverables for each required prose artifact:
PR.md, explanation.md, etc. Do not leave these empty or claim nonexistent runs.
For NEW generated bindings, declare calibration.positive_artifact_authoring as
{"artifacts":["explanation.md","PR.md"]} (adjust to the actual required PROSE
artifact names). Leave their positive_artifact_contents empty. The calibration-only
author receives the ACTUAL reference diff, initial snapshot and public runs and
writes these fixtures before independent checking. Do not include scope.json or
validation.json: those trusted manifests are created by the harness. This solves
the lack of future patch evidence WITHOUT placeholder text or fabricated results.
Use the EXACT checker_manifest_contracts supplied in context when disclosing
scope.json / validation.json. Do not invent alternative manifest keys or layouts.
You may supply positive_artifact_commands:[{filename,script,command,timeout_seconds}]
to create fixtures from ACTUAL reference code inside the calibration container,
e.g. inspect current source and run the public example. These commands execute
AFTER the reference patch is applied, not during scored episodes. A fixture should
explain the public case and current code causally, not merely repeat requirements.
Every requirement statement must disclose the artifact filenames and exact
machine-readable manifest schema used by its checker; do not keep those hidden.
For EVERY requirement add at least one negative_control:
{id,requirement_id,kind,target,content?}. Allowed kinds: remove_artifact,
replace_artifact (content required), change_protected_file (content required),
drop_trusted_probe. Targets must name existing task artifacts/protected paths or
registered probes. Mutation must make that requirement not_met without changing
the positive fixture. These bounded negatives do not prove semantic completeness.
omitted_templates_json must map EVERY unused persona template ID to a concrete
reason, and include no used templates. Do not omit requirements to lower difficulty.
Do not output credentials or instructions to modify the host registry.'''


def parse_generation(result, context):
    """Validate BOTH JSON layers before installing any candidate draft files."""
    if not isinstance(result, dict):
        raise ValueError('Generation envelope must be an object')
    for key in ('decision', 'rationale', 'binding_json', 'omitted_templates_json'):
        if not isinstance(result.get(key), str):
            raise ValueError(f'Generation field {key} must be a string')
    if result['decision'] not in ('applicable', 'needs_information', 'not_applicable'):
        raise ValueError('Unknown applicability decision')
    if not result['rationale'].strip():
        raise ValueError('Applicability rationale must not be empty')
    decoded = {}
    for key in ('binding_json', 'omitted_templates_json'):
        try:
            decoded[key] = json.loads(result[key])
        except json.JSONDecodeError as exc:
            raise ValueError(f'{key}: {exc.msg} at line {exc.lineno}, column {exc.colno}') from exc
        if not isinstance(decoded[key], dict):
            raise ValueError(f'{key} must encode an object')
    raw = decoded['binding_json']
    if (raw.get('task_id'), raw.get('persona_id')) != (context['task']['id'], context['persona']['id']):
        raise ValueError('Generated binding task/persona IDs do not match selection')
    omissions = decoded['omitted_templates_json']
    if any(not isinstance(value, str) or not value.strip() for value in omissions.values()):
        raise ValueError('Omitted-template reasons must be nonempty strings')
    return raw, {'decision': result['decision'], 'rationale': result['rationale'],
                 'omitted_templates': omissions}


def complete_negative_controls(result, context):
    """Preregister missing mechanical controls from declared checker contracts.

    No verdicts are observed here; these controls still have to fail calibration.
    Existing model controls and semantic requirements are never changed.
    """
    raw, _ = parse_generation(result, context)
    if result['decision'] != 'applicable':
        return result
    calibration = raw.get('calibration')
    if not isinstance(calibration, dict):
        return result
    controls = calibration.setdefault('negative_controls', [])
    covered = {item.get('requirement_id') for item in controls}
    used_ids = {item.get('id') for item in controls}
    added = []
    for req in raw.get('requirements', []):
        if req['id'] in covered:
            continue
        params = req['checker'].get('parameters', {})
        kind = req['checker']['kind']
        protected = context['task'].get('protected_files', [])
        if kind == 'isolated_patch' and protected:
            change = {'kind': 'change_protected_file', 'target': protected[0]['path'],
                      'content': protected[0]['content'] + '\nSynthetic negative-control mutation.\n'}
        elif kind in ('isolated_patch', 'reviewable_handoff', 'grounded_explanation'):
            target = (params.get('manifest', 'scope.json') if kind == 'isolated_patch' else
                      params.get('draft', 'PR.md') if kind == 'reviewable_handoff' else
                      params.get('artifact', 'explanation.md'))
            change = {'kind': 'remove_artifact', 'target': target}
        elif kind == 'grounded_rubric' and params.get('artifacts'):
            change = {'kind': 'remove_artifact', 'target': params['artifacts'][0]}
        elif req.get('public_probe') or params.get('public_probe'):
            change = {'kind': 'drop_trusted_probe', 'target': req.get('public_probe') or params['public_probe']}
        else:
            continue  # No safe default; the regular validator must reject missing coverage.
        control_id = f"harness-negative-{req['id']}"
        while control_id in used_ids:
            control_id += '-extra'
        control = {'id': control_id, 'requirement_id': req['id'], **change}
        controls.append(control)
        used_ids.add(control_id)
        added.append(control)
    if not added:
        return result
    validate_controls(raw, context['task'])
    return result | {'binding_json': json.dumps(raw),
                     'mechanical_control_completion': {'version': 'declared-checker-controls-v1', 'added': added}}


def generate(context, log):
    from .api import Client
    # Two output attempts, each allowing the client's one transport retry.
    client = Client(log, max_calls=4)
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': {key: {'type': 'string'} for key in
                             ('decision', 'rationale', 'binding_json', 'omitted_templates_json')},
              'required': ['decision', 'rationale', 'binding_json', 'omitted_templates_json']}
    messages = [
        {'role': 'system', 'content': GENERATION_INSTRUCTIONS},
        {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
    log.parent.mkdir(parents=True, exist_ok=True)
    audit = Path(tempfile.mkdtemp(prefix='generation-', dir=log.parent))
    dump(audit / 'context.json', context)
    decision = None
    for attempt in (1, 2):
        response = client.create(messages, schema=schema, max_output_tokens=16000, effort='medium')
        text = client.text(response)
        record = {'attempt': attempt, 'status': response.get('status'),
                  'incomplete_details': response.get('incomplete_details'),
                  'model': client.cfg['AGENT_MODEL'], 'text': text, 'valid': False}
        dump(audit / f'attempt-{attempt}.json', record)  # Preserve output even on an unexpected validator error.
        try:
            if response.get('status') != 'completed':
                raise ValueError(f"Response not completed: {response.get('status')}")
            result = json.loads(text)
            if isinstance(result, dict) and result.get('decision') in ('applicable', 'needs_information', 'not_applicable'):
                if decision is not None and result['decision'] != decision:
                    raise ValueError('Format retry changed the applicability decision')
                decision = result['decision']
            parse_generation(result, context)
            if result['decision'] == 'applicable' and 'source_roots' in context['task']:
                result = complete_negative_controls(result, context)
                validate_generated_binding(result, context)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            record['error'] = str(exc)
            dump(audit / f'attempt-{attempt}.json', record)
            if attempt == 2:
                raise ValueError(f'Generation failed format validation after 2 attempts; evidence: {audit}. '
                                 'Resume the pipeline to retry this incomplete candidate.') from exc
            print(json.dumps({'generation_retry': 2, 'reason': str(exc), 'evidence': str(audit)}), flush=True)
            messages += [{'role': 'assistant', 'content': text}, {'role': 'user', 'content':
                f'Output validation failed: {exc}. Return a complete corrected response. '
                'Both JSON-encoded fields must parse as objects; escape all nested quotes. '
                'Also fix any reported trigger, probe or calibration schema error. '
                'This is a format repair, not permission to change needs_information/not_applicable '
                'to applicable or invent facts. Keep the selected IDs and semantic decision.'}]
            continue
        record['valid'] = True
        if 'mechanical_control_completion' in result:
            record['mechanical_control_completion'] = result['mechanical_control_completion']
        dump(audit / f'attempt-{attempt}.json', record)
        return result, client.cfg['AGENT_MODEL']


def validate_generated_binding(result, context):
    """Check runtime schema before accepting a supposedly usable generated draft."""
    raw, applicability = parse_generation(result, context)
    with tempfile.TemporaryDirectory(prefix='dspilot-draft-') as temp:
        path = Path(temp) / 'binding.json'
        dump(path, raw)
        episode = specs.compile_episode(context['task']['id'], context['persona']['id'], binding_path=path)
    calibration = raw.get('calibration', {})
    from .fixture_author import artifact_names
    authored = artifact_names(calibration, episode.requirements, episode.task.raw['artifact_files'])
    if calibration.get('commit_paths') != episode.task.raw['source_roots']:
        raise ValueError('calibration.commit_paths must equal literal task.source_roots, without glob suffixes')
    uncovered = validate_controls(raw, episode.task.raw)
    if uncovered:
        raise ValueError(f'Negative controls missing for {sorted(uncovered)}')
    templates = {t['id'] for t in context['persona']['attributes']['evolved_requirements']}
    used = {req['template'] for req in episode.requirements}
    if set(applicability['omitted_templates']) != templates - used:
        raise ValueError('Document every omitted template, and no used template')
    contents = set(calibration.get('positive_artifact_contents', {})) | set(calibration.get('positive_artifacts', {})) | set(authored)
    commands = calibration.get('positive_artifact_commands', [])
    for req in episode.requirements:
        params = req['checker'].get('parameters', {})
        kind = req['checker']['kind']
        required = ([params.get('artifact', 'explanation.md')] if kind == 'grounded_explanation' else
                    [params.get('draft', 'PR.md')] if kind == 'reviewable_handoff' else
                    params.get('artifacts', []) if kind == 'grounded_rubric' else [])
        if not commands and set(required) - contents:
            raise ValueError(f"Missing positive calibration artifacts for {req['id']}: {sorted(set(required) - contents)}")


def recover_last_output(candidate, context):
    """Reuse the last paid output only if unchanged public facts and current validation allow it."""
    from .api import config
    parent = specs.ROOT / 'runs/pair-proposals' / candidate
    outputs = sorted(parent.glob('generation-*/attempt-*.json'), key=lambda path: path.stat().st_mtime_ns)
    if not outputs:
        return None
    path = outputs[-1]
    try:
        record = specs._load(safe_source(path))
        if (specs._load(safe_source(path.parent / 'context.json')) != context or
                record.get('status') != 'completed' or record.get('model') != config()['AGENT_MODEL']):
            return None
        result = json.loads(record['text'])
        if result.get('decision') != 'applicable':
            return None
        result = complete_negative_controls(result, context)
        validate_generated_binding(result, context)
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        return None
    result = result | {'recovered_from': str(path.relative_to(specs.ROOT)), 'recovered_sha256': digest(path)}
    print(json.dumps({'generation_recovered_without_api_call': candidate, 'evidence': result['recovered_from']}), flush=True)
    return result, record['model']


def propose(candidate, task_id, persona_id, *, use_model=False, from_binding=None, retry_generation=False):
    base = folder(candidate)
    context = public_context(task_id, persona_id)
    recovering = retry_generation and base.exists()
    if retry_generation and (not use_model or from_binding is not None or not base.exists()):
        raise ValueError('--retry-generation requires --generate and an existing incomplete model candidate')
    if base.exists() and not recovering:
        raise ValueError('Candidate already exists; use a new candidate ID')
    if recovering:
        _, meta = candidate_data(candidate)
        if (meta['task_id'], meta['persona_id'], meta.get('origin')) != (task_id, persona_id, 'model'):
            raise ValueError('Recovery candidate identity/origin mismatch')
        if (all((base / name).exists() for name in ('binding.json', 'applicability.json')) or
                any((base / name).exists() for name in ('check.json', 'review.json', 'publication.json')) or
                (task_id, persona_id) in specs.binding_paths()):
            raise ValueError('Only incomplete, unchecked, unreviewed, unpublished model candidates may be retried')
        archive_parent = specs.ROOT / 'runs/pair-proposals' / candidate
        archive_parent.mkdir(parents=True, exist_ok=True)
        archive = Path(tempfile.mkdtemp(prefix='recovery-', dir=archive_parent))
        for path in base.iterdir():
            shutil.copy2(safe_source(path), archive / path.name)
        dump(archive / 'recovery.json', {'candidate': candidate, 'utc': now(),
                                       'reason': 'Explicit regeneration of incomplete draft with current public facts'})
    if not use_model and from_binding is None:
        from_binding = specs.binding_paths().get((task_id, persona_id))
        if from_binding is None:
            raise ValueError('For a new pairing use --generate (model call) or --from-binding FILE')
    # Validate imports before creating the candidate directory.
    if from_binding is not None:
        raw = specs._load(Path(from_binding))
        if (raw.get('task_id'), raw.get('persona_id')) != (task_id, persona_id):
            raise ValueError('Imported binding IDs must match the selected pair')
    if not recovering:
        base.mkdir(parents=True, exist_ok=False)
        meta = {'version': VERSION, 'candidate_id': candidate, 'task_id': task_id,
                'persona_id': persona_id, 'created_utc': now(), 'origin': 'model' if use_model else 'import'}
        dump(base / 'candidate.json', meta)
    dump(base / 'public-context.json', context)
    if use_model:
        cached = recover_last_output(candidate, context) if recovering else None
        result, model = cached or generate(context, specs.ROOT / 'runs/pair-proposals' / candidate / 'api.jsonl')
        dump(base / 'generation.json', result | {'model': model})
        raw, applicability = parse_generation(result, context)
    else:
        templates = context['persona']['attributes']['evolved_requirements']
        used = {req['template'] for req in raw['requirements']}
        applicability = {'decision': 'needs_information',
                         'rationale': 'Imported draft: document applicability evidence before checking.',
                         'omitted_templates': {t['id']: 'Document why this template is not applicable.'
                                               for t in templates if t['id'] not in used}}
    dump(base / 'binding.json', raw)
    dump(base / 'applicability.json', applicability)
    return {'candidate': candidate, 'path': str(base), 'status': 'draft',
            'next': 'Inspect/edit binding.json and applicability.json, then pair check.'}


def inputs(candidate):
    base, meta = candidate_data(candidate)
    task = specs.load_task(meta['task_id'])
    raw = specs._load(base / 'binding.json')
    paths = [base / name for name in ('candidate.json', 'binding.json', 'applicability.json')]
    paths += list((specs.ROOT / 'dspilot').glob('*.py'))
    paths += [task.path, task.contract_path, specs.ROOT / 'data/personas.v3.json',
              specs.ROOT / 'data/instruction.md', specs.ROOT / 'sources.lock.json']
    if task.raw.get('source', {}).get('lock_file'):
        paths.append(specs.ROOT / specs._relative(task.raw['source']['lock_file'], 'source lock'))
    for value in raw.get('calibration', {}).get('positive_artifacts', {}).values():
        relative = Path(specs._relative(value, 'calibration artifact'))
        if relative.parts[:2] != ('data', 'calibration'):
            raise ValueError('Candidate positive_artifacts may reference only reviewed files under data/calibration/')
        paths.append(specs.ROOT / relative)
    return {str(path.relative_to(specs.ROOT)): digest(safe_source(path)) for path in sorted(set(paths))}


def static_check(candidate):
    base, meta = candidate_data(candidate)
    applicability = specs._load(base / 'applicability.json')
    if applicability.get('decision') != 'applicable' or not applicability.get('rationale', '').strip():
        raise ValueError('Candidate needs applicability evidence and decision=applicable before calibration. '
                         f"Recorded decision: {applicability.get('decision')}; "
                         f"rationale: {applicability.get('rationale', '')[:1000]}. "
                         'Skipping human review does not bypass this requirement.')
    episode = specs.compile_episode(meta['task_id'], meta['persona_id'], binding_path=base / 'binding.json')
    raw = episode.binding.raw
    from .fixture_author import artifact_names
    artifact_names(raw.get('calibration', {}), episode.requirements, episode.task.raw['artifact_files'])
    if not isinstance(raw.get('calibration'), dict) or not raw['calibration']:
        raise ValueError('Candidate requires calibration fixtures')
    templates = {t['id'] for t in episode.persona['attributes']['evolved_requirements']}
    used = {req['template'] for req in episode.requirements}
    omissions = applicability.get('omitted_templates', {})
    if not isinstance(omissions, dict) or set(omissions) != templates - used:
        raise ValueError('Document every omitted persona template, and no used templates')
    if any(not isinstance(reason, str) or not reason.strip() for reason in omissions.values()):
        raise ValueError('Empty omitted-template rationale')
    for req in episode.requirements:
        if not isinstance(req.get('bindings'), dict):
            raise ValueError('Every candidate requirement needs a bindings object')
    uncovered = validate_controls(raw, episode.task.raw)
    if uncovered:
        raise ValueError(f'Negative controls missing for requirements: {sorted(uncovered)}')
    inputs(candidate)  # Also enforce safe, pinned local input paths.
    return episode


def calibrate_candidate(candidate, fingerprint):
    """Run the existing calibrator in a private source/cache copy, with no gates reused."""
    from .prepare import lock, verify_cache
    base, meta = candidate_data(candidate)
    verify_cache(meta['task_id'])
    parent = specs.ROOT / 'runs/pair-checks' / candidate
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='attempt-', dir=parent))
    for relative in fingerprint:
        if relative.startswith('data/candidates/'):
            continue
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(specs.ROOT / relative, destination)
    target = stage / 'data/bindings' / f"{meta['task_id']}--{meta['persona_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base / 'binding.json', target)
    info = lock(meta['task_id'])
    for relative in info['task_files']:
        source = specs.ROOT / '.cache/tasks' / meta['task_id'] / relative
        destination = stage / '.cache/tasks' / meta['task_id'] / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_source(source), destination)
    # Credentials are referenced on the host, never copied into candidate/source archives.
    env = os.environ.copy()
    env['DS_PERSONA_ENV_FILE'] = str(Path(env.get('DS_PERSONA_ENV_FILE', specs.ROOT / '.env')).resolve())
    env['DS_TASK_ID'], env['DS_PERSONA_ID'] = meta['task_id'], meta['persona_id']
    env.pop('PYTHONPATH', None)
    print(json.dumps({'candidate_calibration_started': candidate,
                      'log': str(stage / 'calibration-output.txt')}), flush=True)
    with (stage / 'calibration-output.txt').open('w') as output:
        result = subprocess.run([sys.executable, '-m', 'dspilot.launch', 'calibrate',
                                 '--task-id', meta['task_id'], '--persona-id', meta['persona_id']],
                                cwd=stage, env=env, stdout=output, stderr=subprocess.STDOUT)
    gate_relative = ('runs/judge-v3-gates.json' if (meta['task_id'], meta['persona_id']) ==
                     ('tomlkit-toml-table-converters', 'first_time_pr_contributor') else
                     f"runs/gates/{meta['task_id']}--{meta['persona_id']}.json")
    gate_path = stage / gate_relative
    gate = specs._load(gate_path) if gate_path.exists() else {}
    passed = result.returncode == 0 and gate.get('passed') is True
    return {'passed': passed, 'exit_code': result.returncode,
            'evidence_directory': str(stage.relative_to(specs.ROOT)),
            'gate_path': str(gate_path.relative_to(specs.ROOT)) if gate_path.exists() else None,
            'gate_sha256': digest(gate_path) if gate_path.exists() else None,
            'model_roles': gate.get('model_roles'), 'negative_controls': gate.get('negative_controls', [])}


def check_candidate(candidate, *, calibrate=False):
    base, _ = candidate_data(candidate)
    record = {'version': VERSION, 'checked_utc': now(), 'static_passed': False, 'passed': False}
    dump(base / 'check.json', record)  # Invalidate a previous pass before any work.
    try:
        static_check(candidate)
        fingerprint = inputs(candidate)
        record.update(static_passed=True, inputs=fingerprint)
        dump(base / 'check.json', record)
        if calibrate:
            record['calibration'] = calibrate_candidate(candidate, fingerprint)
            if inputs(candidate) != fingerprint:
                raise ValueError('Candidate or source changed during calibration')
            record['passed'] = record['calibration']['passed']
    except Exception as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        dump(base / 'check.json', record)
    return record


def require_checked(candidate):
    base, _ = candidate_data(candidate)
    record = specs._load(base / 'check.json')
    if record.get('passed') is not True or record.get('inputs') != inputs(candidate):
        raise ValueError('Missing, failed, or stale candidate calibration; rerun pair check --calibrate')
    evidence = record.get('calibration', {})
    if not evidence.get('gate_path') or digest(safe_source(specs.ROOT / evidence['gate_path'])) != evidence.get('gate_sha256'):
        raise ValueError('Candidate calibration evidence is missing or changed')
    gate = specs._load(specs.ROOT / evidence['gate_path'])
    from .negative_controls import controls_for
    expected = {item['id'] for item in controls_for(specs._load(base / 'binding.json'))}
    observed = gate.get('negative_controls', [])
    if (gate.get('passed') is not True or {item['id'] for item in observed} != expected
            or any(item['status'] != 'not_met' for item in observed)):
        raise ValueError('Calibration gate does not confirm every negative control')
    return record


def review(candidate, decision, reviewer, note):
    base, _ = candidate_data(candidate)
    if not reviewer.strip() or not note.strip():
        raise ValueError('Reviewer identity and substantive review note are required')
    if decision == 'approve':
        require_checked(candidate)
    record = {'version': VERSION, 'decision': decision, 'reviewer': reviewer, 'note': note,
              'reviewed_utc': now(), 'inputs': inputs(candidate),
              'check_sha256': digest(base / 'check.json') if (base / 'check.json').exists() else None}
    dump(base / 'review.json', record)
    return record


def updated_registry(plan, task_id, persona_id, rationale):
    plan = copy.deepcopy(plan)
    catalog = specs.persona_catalog()
    if set(plan['personas']) != set(catalog):
        raise ValueError('Persona axis differs from catalog; audit it before publication')
    if task_id not in plan['tasks']:
        plan['tasks'].append(task_id)
        bindings = specs.binding_paths()
        for persona in plan['personas']:
            registered = (task_id, persona) in bindings
            plan['cells'].append({'task_id': task_id, 'persona_id': persona,
                                  'status': 'runnable' if registered else 'pending_review',
                                  'applicability': ('Existing registered binding; preserve its audit.' if registered else
                                                    'Not yet assessed; pending applicability and binding review.')})
    matches = [cell for cell in plan['cells'] if (cell['task_id'], cell['persona_id']) == (task_id, persona_id)]
    if len(matches) != 1:
        raise ValueError('Expected exactly one candidate registry cell')
    matches[0].update(status='runnable', applicability=rationale)
    return plan


def publish(candidate, *, skip_human_review=False):
    base, meta = candidate_data(candidate)
    require_checked(candidate)
    review_path = base / 'review.json'
    approval_sha256 = digest(review_path) if review_path.exists() else None
    if skip_human_review:
        if review_path.exists() and specs._load(review_path).get('decision') == 'reject':
            raise ValueError('Candidate was explicitly rejected; create a revised candidate instead')
        approval = {'decision': 'skip_human_review', 'reviewer': None,
                    'review_status': 'automated_validation_only',
                    'authorized_utc': now(), 'inputs': inputs(candidate),
                    'check_sha256': digest(base / 'check.json'),
                    'note': 'Explicit --skip-human-review; no human approval is asserted.'}
    else:
        approval = specs._load(review_path)
        if (approval.get('decision') != 'approve' or approval.get('inputs') != inputs(candidate)
                or approval.get('check_sha256') != digest(base / 'check.json')):
            raise ValueError('Explicit human approval of the current candidate/check is required')
        approval = approval | {'review_status': 'human_approved'}
    static_check(candidate)
    registry_path = specs.ROOT / 'data/pairings.v1.json'
    destination = specs.ROOT / 'data/bindings' / f"{meta['task_id']}--{meta['persona_id']}.json"
    lock_path = specs.ROOT / 'runs/pair-publication.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        require_checked(candidate)
        specs.pairing_plan()  # Refuse publication on an inconsistent current registry.
        if destination.exists() or (meta['task_id'], meta['persona_id']) in specs.binding_paths():
            raise ValueError('Binding already exists; publication never overwrites registered pairs')
        if (approval['inputs'] != inputs(candidate) or approval['check_sha256'] != digest(base / 'check.json')
                or approval_sha256 != (digest(review_path) if review_path.exists() else None)):
            raise ValueError('Candidate changed while waiting for publication lock')
        binding_bytes = (base / 'binding.json').read_bytes()
        binding_key = str((base / 'binding.json').relative_to(specs.ROOT))
        if hashlib.sha256(binding_bytes).hexdigest() != approval['inputs'][binding_key]:
            raise ValueError('Candidate changed before publication')
        original = registry_path.read_bytes()
        plan = updated_registry(json.loads(original), meta['task_id'], meta['persona_id'],
                                specs._load(base / 'applicability.json')['rationale'])
        publication_provenance = {'candidate_id': candidate, 'review_status': approval['review_status'],
                                  'check_sha256': approval['check_sha256'],
                                  'binding_sha256': hashlib.sha256(binding_bytes).hexdigest()}
        for cell in plan['cells']:
            if (cell['task_id'], cell['persona_id']) == (meta['task_id'], meta['persona_id']):
                cell['publication'] = publication_provenance
        audit_parent = specs.ROOT / 'runs/pair-publications' / candidate
        audit_parent.mkdir(parents=True, exist_ok=True)
        audit = Path(tempfile.mkdtemp(prefix='attempt-', dir=audit_parent))
        (audit / 'pairings.before.json').write_bytes(original)
        dump(audit / 'authorization.json', approval)
        if not skip_human_review:
            dump(audit / 'approval.json', approval)
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with destination.open('xb') as output:
                created = True
                output.write(binding_bytes)
            dump(registry_path, plan)
            specs.pairing_plan()
        except BaseException:
            registry_path.write_bytes(original)
            if created:
                destination.unlink()
            raise
        record = {'version': VERSION, 'published_utc': now(), 'candidate_id': candidate,
                  'binding_path': str(destination.relative_to(specs.ROOT)),
                  'binding_sha256': digest(destination),
                  'review_status': approval['review_status'],
                  'review_sha256': None if skip_human_review else approval_sha256,
                  'check_sha256': approval['check_sha256'],
                  'registry_sha256': digest(registry_path),
                  'note': 'Production gate is NOT copied; run matrix with --calibrate and a new prefix.'}
        dump(audit / 'publication.json', record)
        dump(base / 'publication.json', record)
        return record


def configure_parser(parser):
    commands = parser.add_subparsers(dest='pair_command', required=True)
    propose_parser = commands.add_parser('propose', help='Create an isolated draft; --generate calls the model.')
    propose_parser.add_argument('--task-id', required=True)
    propose_parser.add_argument('--persona-id', required=True)
    propose_parser.add_argument('--candidate', required=True)
    propose_parser.add_argument('--retry-generation', action='store_true',
                                help='Archive and regenerate only an incomplete, unchecked model candidate.')
    source = propose_parser.add_mutually_exclusive_group()
    source.add_argument('--generate', action='store_true')
    source.add_argument('--from-binding', type=Path)
    check_parser = commands.add_parser('check', help='Static checks; add --calibrate for Docker/model calibration.')
    check_parser.add_argument('--candidate', required=True)
    check_parser.add_argument('--calibrate', action='store_true')
    review_parser = commands.add_parser('review', help='Record a human decision; never invoked automatically.')
    review_parser.add_argument('--candidate', required=True)
    review_parser.add_argument('--decision', choices=['approve', 'reject'], required=True)
    review_parser.add_argument('--reviewer', required=True)
    review_parser.add_argument('--note', required=True)
    publish_parser = commands.add_parser('publish', help='Publish a calibrated NEW pair; no overwrites.')
    publish_parser.add_argument('--candidate', required=True)
    publish_parser.add_argument('--skip-human-review', action='store_true',
                                help='Explicitly publish with automated validation only; still requires current calibration.')


def dispatch(args):
    if args.pair_command == 'propose':
        result = propose(args.candidate, args.task_id, args.persona_id,
                         use_model=args.generate, from_binding=args.from_binding,
                         retry_generation=args.retry_generation)
    elif args.pair_command == 'check':
        result = check_candidate(args.candidate, calibrate=args.calibrate)
    elif args.pair_command == 'review':
        result = review(args.candidate, args.decision, args.reviewer, args.note)
    else:
        result = publish(args.candidate, skip_human_review=args.skip_human_review)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.pair_command == 'check' and args.calibrate and not result['passed']:
        raise SystemExit('Candidate calibration failed; inspect retained evidence')
