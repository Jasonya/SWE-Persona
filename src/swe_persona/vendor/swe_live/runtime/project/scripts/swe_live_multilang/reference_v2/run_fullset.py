"""Full pinned dataset, empirical P20, immutable attempts and explicit coverage.

Preparation never calls a model. --run enables the selected paid stage. The
paper queue refuses unresolved persona bindings rather than silently running
only the two legacy personas and calling it a ten-persona experiment.
"""
import bootstrap
from bootstrap import ROOT
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time

from fullset_protocol import persona_pool, preliminary_inventory, p20, fastest_selection, metric_summary
from support import FULLSET_PROTOCOL, REVISION, Manifest, digest, file_hash, read, save, source_hashes, collection_source_hashes
from run_study import LANGUAGES, ensure_gold, ensure_gates
from whole_sample import supervise_sample, existing_result
from collection.admission import resolved_inventory, freeze_admission, import_qualification
from collection.timing import round_ceiling, next_reference_cap, can_extend
from collection.online import PEER_INITIAL_SECONDS, PEER_SOURCE, budget_after, advance_budget, ordered, censored_p20

HERE = Path(__file__).resolve().parent
DEFAULT = ROOT / ".local/multilang-study/fullset-p20-v1"
ARMS = ("persona", "no_evolution", "fixed_schedule")


def atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    save(temporary, value)
    os.replace(temporary, path)


def all_rows():
    from datasets import Dataset, load_dataset
    cache = ROOT / ".local/hf-cache/SWE-bench-Live___multi_lang/default/0.0.0" / REVISION
    for language in LANGUAGES:
        files = sorted(cache.glob("*-" + language + ".arrow"))
        data = (Dataset.from_file(str(files[0])) if len(files) == 1 else
                load_dataset("SWE-bench-Live/MultiLang", split=language, revision=REVISION,
                             cache_dir=str(ROOT / ".local/hf-cache")))
        # Source order is irrelevant. No ranking by private tests or outcomes.
        for row in sorted(data, key=lambda r: r["instance_id"]):
            yield language, row


def prepare(study, config, rows=None):
    study = Path(study)
    study.mkdir(parents=True, exist_ok=True)
    path = study / "manifest.json"
    if path.exists():
        manifest = read(path)
        if manifest["config"] != config or manifest["protocol"] != FULLSET_PROTOCOL:
            raise ValueError("Frozen full-set configuration differs; use a new directory")
        if manifest["sources"] != source_hashes():
            raise ValueError("Source changed after freezing; do not mix code versions in a final study")
        if manifest.get("collection_sources") != collection_source_hashes():
            raise ValueError("Collection code changed after freezing; use a new study directory")
        for task in manifest["tasks"]:
            if file_hash(study / task["path"]) != task["sha256"]:
                raise ValueError("Frozen task modified: " + task["key"])
        if file_hash(study / "inventory.json") != manifest["inventory_sha256"]:
            raise ValueError("Frozen persona inventory modified")
        return manifest
    tasks, inventory, seen = [], [], set()
    for language, row in rows if rows is not None else all_rows():
        if row["instance_id"] in seen:
            raise ValueError("Duplicate dataset instance: " + row["instance_id"])
        seen.add(row["instance_id"])
        key = f"{len(tasks) + 1:04d}-{language}"
        target = study / "tasks" / key / "task.private.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        Manifest(target)
        task = {"key": key, "language": language, "instance_id": row["instance_id"],
                "path": target.relative_to(study).as_posix(), "sha256": file_hash(target)}
        tasks.append(task)
        inventory.extend(preliminary_inventory(task))
    if len(tasks) != config["expected_tasks"]:
        raise ValueError(f"Expected {config['expected_tasks']} tasks, found {len(tasks)}; inspect the pinned dataset")
    atomic_save(study / "inventory.json", inventory)
    from run_qualification import SEEDS
    manifest = {"protocol": FULLSET_PROTOCOL, "dataset_revision": REVISION,
                "created_utc": datetime.now(timezone.utc).isoformat(), "config": config,
                "tasks": tasks, "personas": [p["id"] for p in persona_pool()], "arms": list(ARMS),
                "sources": source_hashes(), "collection_sources": collection_source_hashes(),
                "adapter_qualification_task_ids": sorted({task for task, _ in SEEDS.values()}),
                "inventory_sha256": file_hash(study / "inventory.json"),
                "selection": "All pinned tasks, deterministic language/instance-ID order, no replacements",
                "comparison": "Original baseline has the full issue; pair arms share persona, public opening and budgets"}
    atomic_save(path, manifest)
    return manifest


def readiness(study, manifest):
    inventory = resolved_inventory(study, manifest)
    counts = Counter(p["status"] for p in inventory)
    required = [p for p in inventory if p["status"] == "binding_required"]
    # A complete pool inventory is not evidence that all ten roles have run.
    # Require an independently audited, task-bound qualification sample for
    # every role; synthetic rubric controls alone do not satisfy this gate.
    validated, qualification_errors = [], {}
    from task_fixtures import SPECS
    from personas import PERSONAS
    from audit_sample import audit
    for persona in manifest["personas"]:
        receipt_path = study / "qualification" / persona / "qualification.json"
        if not receipt_path.exists():
            qualification_errors[persona] = "No validated task-bound qualification sample"
            continue
        receipt = read(receipt_path)
        sample = (study / receipt.get("sample_path", "")).resolve()
        if study.resolve() not in sample.parents:
            qualification_errors[persona] = "Qualification path is outside this study"
            continue
        result = audit(sample)
        if (not result["ready_for_review"] or receipt.get("persona") != persona
                or receipt.get("sources") != manifest["sources"]
                or receipt.get("fixture_controls_passed") is not True):
            qualification_errors[persona] = "Qualification evidence or fixture controls incomplete"
            continue
        protocol = read(sample / "protocol.json")
        if protocol.get("sources") != manifest["sources"]:
            qualification_errors[persona] = "Qualification sample used different source code"
            continue
        if any(model != manifest['config'].get('model') for model in protocol.get('models', {}).values()):
            qualification_errors[persona] = 'Qualification model differs from the frozen study model'
            continue
        episode = read(sample / "episode.json")
        if episode.get("persona") != persona or receipt.get("episode_sha256") != file_hash(sample / "episode.json"):
            qualification_errors[persona] = "Qualification identity or digest mismatch"
            continue
        validated.append(persona)
    report = {"tasks": len(manifest["tasks"]), "personas": len(manifest["personas"]),
        "candidate_pairs": len(inventory), "counts": dict(counts),
        "missing_binding_personas": sorted({p["persona"] for p in required}),
        "personas_with_registered_implementations": sorted(set(PERSONAS) | {s["persona"] for s in SPECS.values()}),
        "validated_personas": validated, "qualification_errors": qualification_errors,
        "unresolved_pairs": sum(p['status'] not in ('applicable', 'not_applicable') for p in inventory),
        "final_queue_ready": all(p['status'] in ('applicable', 'not_applicable') for p in inventory)
                             and any(p['status'] == 'applicable' for p in inventory) and len(validated) == 10,
        "publication_ready": False,
        "reason": "Missing task-specific bindings are not evidence of persona incompatibility. Finalization also requires live gates and transcript review."}
    atomic_save(study / "readiness.json", report)
    return report


def episode_command(study, task, model, arm="original", persona="manager_no_code", opening=None, binding=None):
    command = [sys.executable, "-u", str(HERE / "run_reference.py"), "--run",
        "--task", str(study / task["path"]), "--gold-dir", str(study / "gold" / task["key"]),
        "--agent-model", model, "--arm",
        "single_turn" if arm == "original" else "interactive_no_evolution" if arm == "no_evolution" else "interactive",
        "--persona", persona, "--interaction-seconds", "0", "--verification-seconds", "0",
        "--max-turns", "8", "--max-steps", "14", "--single-turn-steps", "40"]
    if arm == "fixed_schedule":
        command += ["--fixed-schedule"]
    if opening:
        command += ["--opening-file", str(opening)]
    if binding:
        command += ["--binding", str(binding)]
    return command


def get_image(client, task_manifest, expected=None):
    import docker
    pulled = False
    try:
        image = client.images.get(task_manifest.docker_image)
    except docker.errors.ImageNotFound:
        print("[image]", task_manifest.docker_image, flush=True)
        image = client.images.pull(task_manifest.docker_image)
        pulled = True
    if expected and image.id != expected:
        raise ValueError("Image changed since reference calibration")
    return image, pulled


def release_image(client, image, pulled, keep):
    # Only remove images acquired by this invocation. Never prune globally or force removal.
    if pulled and not keep:
        try:
            client.images.remove(image.id, force=False, noprune=True)
        except Exception as exc:
            print("[cache retained]", image.id, type(exc).__name__, flush=True)


def reference_round(study, manifest, client, keep_images, round_dir, ceiling):
    config = manifest["config"]
    references = []
    for index, task in enumerate(manifest["tasks"], 1):
        if source_hashes() != manifest["sources"]:
            raise RuntimeError("Source changed during calibration")
        directory = round_dir / task['key']
        existing = existing_result(directory)
        if existing:
            references.append(dict(existing, task_key=task["key"]))
            continue
        cap = next_reference_cap(references, len(manifest['tasks']), ceiling)
        print(f"[reference {index}/{len(manifest['tasks'])}] {task['instance_id']}; whole-sample cap={cap:.3f}s", flush=True)
        image, pulled = None, False
        try:
            task_manifest = Manifest(study / task["path"])
            image, pulled = get_image(client, task_manifest)
            gold_limit = min(ceiling, config['gold_safety_seconds'])
            gold_attempts = sorted((study / 'gold' / task['key']).glob('attempt-*/attempt.json'))
            previous_gold = read(gold_attempts[-1]) if gold_attempts else {}
            retry_timed_out_gold = previous_gold.get('exit_code') == 124 and gold_limit > previous_gold.get('seconds', 0)
            gold_seconds = ensure_gold(study, task, image.id, {'gold_seconds': gold_limit}, retry_timed_out_gold)
            if gold_seconds is None:
                gold_attempts = sorted((study / 'gold' / task['key']).glob('attempt-*/attempt.json'))
                latest_gold = read(gold_attempts[-1]) if gold_attempts else {}
                result = {'status': 'gold_preflight_timeout' if latest_gold.get('exit_code') == 124 else 'gold_blocked',
                          'seconds': None, 'image_id': image.id, 'gold_ceiling_seconds': gold_limit}
                directory.mkdir(parents=True)
                atomic_save(directory / "result.json", result)
            else:
                result = supervise_sample(episode_command(study, task, config["model"]), directory,
                                          cap)
                result["image_id"] = image.id
                atomic_save(directory / "result.json", result)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if directory.exists():
                # A supervisor/cleanup failure must stop; never downgrade it and
                # start a second 16 GiB job while the first may still be alive.
                raise
            result = {"status": "infrastructure_error", "seconds": None, "error_type": type(exc).__name__}
            atomic_save(directory / "result.json", result)
        finally:
            if image:
                release_image(client, image, pulled, keep_images)
        references.append(dict(result, task_key=task["key"]))
        result['calibration_round'] = round_dir.name
        atomic_save(round_dir / 'progress.json', {'processed': len(references), 'total': len(manifest['tasks']),
            'ceiling_seconds': ceiling, 'next_sample_cap_seconds': next_reference_cap(references, len(manifest['tasks']), ceiling),
            'counts': dict(Counter(r['status'] for r in references))})
        atomic_save(round_dir / 'references.json', references)
    atomic_save(round_dir / 'references.json', references)
    return references


def reference_stage(study, manifest, client, keep_images):
    """Start at ten minutes; expand only if the percentile is unidentified.

    A restarted reference is a fresh stochastic trajectory, not a continuation.
    Therefore an expanded round reruns the entire frozen population and uses
    only that round for its quantile. All earlier attempts remain auditable.
    """
    config = manifest['config']
    initial, maximum = config['reference_safety_seconds'], config['reference_max_seconds']
    index = 1
    while True:
        ceiling = round_ceiling(initial, maximum, index)
        directory = study / 'reference' / f'round-{index:03d}'
        directory.mkdir(parents=True, exist_ok=True)
        print(f'[calibration round {index}] initial ceiling={ceiling:.1f}s; this is not the final P20', flush=True)
        refs = reference_round(study, manifest, client, keep_images, directory, ceiling)
        try:
            quantile = p20(refs)
        except ValueError as error:
            extending = ceiling < maximum and can_extend(refs)
            atomic_save(directory / 'round.json', {'status': 'unidentified', 'reason': str(error),
                'ceiling_seconds': ceiling, 'extended': extending, 'used_for_final_threshold': False})
            if not extending:
                raise ValueError('P20 remains unidentified; no arbitrary fallback threshold was substituted') from error
            print('[calibration] Too few complete references; preserving this round and doubling the ceiling for a fresh full round.', flush=True)
            index += 1
            continue
        atomic_save(directory / 'round.json', {'status': 'identified', 'ceiling_seconds': ceiling,
                    'used_for_final_threshold': True, 'p20': quantile})
        atomic_save(study / 'calibration.json', {'selected_round': directory.relative_to(study).as_posix(),
            'round_index': index, 'ceiling_seconds': ceiling, 'p20_seconds': quantile['seconds'],
            'policy': 'Fresh full-population rounds on extension; do not mix first attempts and rerun completions'})
        atomic_save(study / 'references.json', refs)
        return refs


def freeze_threshold(study, manifest, references):
    expected = {task['key'] for task in manifest['tasks']}
    actual = [record['task_key'] for record in references]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError('Cannot freeze P20 before every frozen task has a reference record')
    threshold = p20(references)
    threshold.update(manifest_sha256=file_hash(study / "manifest.json"),
                     selection_count=threshold['rank'] if manifest['config']['prefilter'] else threshold['population'],
                     tie_break='task_key_ascending',
                     policy="prefilter_tasks" if manifest["config"]["prefilter"] else "attempt_all_evaluable_tasks")
    path = study / "threshold.json"
    if path.exists() and read(path) != threshold:
        raise ValueError("Reference population changed after threshold freeze")
    atomic_save(path, threshold)
    selection = fastest_selection(references, threshold, manifest['config']['prefilter'])
    atomic_save(study / "time-selection.json", selection)
    selected = sum(s["status"] == "selected" for s in selection)
    print(f"[P20 frozen] {threshold['seconds']:.3f} seconds; selected {selected}/{len(references)} tasks", flush=True)
    return threshold, selection


def load_references(study, manifest):
    records = []
    state = read(study / 'calibration.json') if (study / 'calibration.json').exists() else {}
    reference_root = study / state.get('selected_round', 'reference')
    for task in manifest["tasks"]:
        path = reference_root / task['key'] / 'result.json'
        if not path.exists():
            raise ValueError("Reference stage incomplete: " + task["key"])
        records.append(dict(read(path), task_key=task["key"]))
    return records


def final_stage(study, manifest, client, references, threshold, selection, keep_images):
    ready = readiness(study, manifest)
    if not ready["final_queue_ready"]:
        raise RuntimeError(f"Final queue is not ready: {ready['unresolved_pairs']} unresolved pairs; "
                           f"{len(ready['validated_personas'])}/10 validated personas")
    # This guard deliberately precedes all checker calibration / paid agent work.
    # A future registered binding must have an offline fit receipt, fixture
    # hashes, live checker-gate result and supported runner mapping before admission.
    inventory = resolved_inventory(study, manifest)
    pending = [p for p in inventory if p["status"] not in ("applicable", "not_applicable")]
    if pending:
        raise RuntimeError("Public task-persona fit receipts are still pending; see inventory.json")
    final_plan = {'manifest_sha256': file_hash(study / 'manifest.json'),
                  'admission_sha256': file_hash(study / 'admission' / 'frozen.json'),
                  'threshold_sha256': file_hash(study / 'threshold.json'),
                  'selection_digest': digest(selection),
                  'qualification_sha256': {p: file_hash(study / 'qualification' / p / 'qualification.json')
                                           for p in manifest['personas']}}
    plan_path = study / 'final-plan.json'
    if plan_path.exists() and read(plan_path) != final_plan:
        raise ValueError('Final collection inputs changed after scoring began')
    if not plan_path.exists():
        atomic_save(plan_path, final_plan)
    from run_qualification import ensure_gate
    by_reference = {r["task_key"]: r for r in references}
    selected = {s["task_key"] for s in selection if s["status"] == "selected"}
    for task in manifest["tasks"]:
        if task["key"] not in selected:
            continue
        if source_hashes() != manifest["sources"]:
            raise RuntimeError("Source changed during the frozen experiment")
        image, pulled = get_image(client, Manifest(study / task["path"]), by_reference[task["key"]]["image_id"])
        try:
            for pair in inventory:
                if pair["task_key"] != task["key"] or pair["status"] != "applicable":
                    continue
                ensure_gate(study, pair, manifest['config']['model'])
                for arm in ARMS:
                    directory = study / "episodes" / task["key"] / pair["persona"] / arm
                    if existing_result(directory):
                        continue
                    opening = study / pair["opening_path"]
                    if file_hash(opening) != pair["opening_sha256"]:
                        raise ValueError("Frozen opening changed")
                    binding = None
                    if pair.get("binding_path"):
                        binding = study / pair["binding_path"]
                        if file_hash(binding) != pair["binding_sha256"]:
                            raise ValueError("Frozen task binding changed")
                    command = episode_command(study, task, manifest["config"]["model"], arm, pair["persona"], opening, binding)
                    supervise_sample(command, directory, threshold["seconds"])
                    report(study, manifest)
        finally:
            release_image(client, image, pulled, keep_images)


def online_stage(study, manifest, client, keep_images):
    """Visit the complete admitted matrix once, without a reference prepass."""
    ready = readiness(study, manifest)
    if not ready['final_queue_ready']:
        raise RuntimeError(f"Full ten-persona queue is not ready: {ready['unresolved_pairs']} unresolved pairs; "
                           f"{len(ready['validated_personas'])}/10 validated personas. No partial fallback.")
    config = manifest['config']
    inventory = resolved_inventory(study, manifest)
    plan = {'mode': 'online', 'manifest_sha256': file_hash(study / 'manifest.json'),
            'admission_sha256': file_hash(study / 'admission' / 'frozen.json'),
            'qualification_sha256': {p: file_hash(study / 'qualification' / p / 'qualification.json') for p in manifest['personas']},
            'initial_threshold_seconds': config['initial_threshold_seconds'],
            'initialization_source': PEER_SOURCE if config['initial_threshold_seconds'] == PEER_INITIAL_SECONDS else {'kind': 'explicit CLI override'},
            'order': 'SHA256(seed:public task ID), then SHA256(seed:task ID:persona ID)',
            'quantile_scope': 'Completed/timeout persona-arm whole samples; invalid and N/A retained separately',
            'selection_claim': 'Online estimated P20 budgets, not advance identification of the global fastest fifth'}
    plan_file = study / 'final-plan.json'
    if plan_file.exists() and read(plan_file) != plan:
        raise ValueError('Online collection inputs changed after scoring began')
    if not plan_file.exists():
        atomic_save(plan_file, plan)
    from run_qualification import ensure_gate
    from run_reference import gold_proof
    history = []
    state = budget_after([], config['initial_threshold_seconds'], config['online_max_seconds'], config['update_every'])
    tasks = ordered(manifest['tasks'], lambda task: task['instance_id'], config['order_seed'])
    for task_index, task in enumerate(tasks, 1):
        if source_hashes() != manifest['sources']:
            raise RuntimeError('Runtime changed during online collection')
        pairs = ordered([p for p in inventory if p['task_key'] == task['key'] and p['status'] == 'applicable'],
                        lambda p: p['instance_id'] + ':' + p['persona'], config['order_seed'])
        image, pulled, gold_status = None, False, None
        try:
            for pair in pairs:
                pair_dir = study / 'episodes' / task['key'] / pair['persona']
                budget = {'cap_seconds': state['next_cap_seconds'], 'history_digest': digest(history),
                          'update_index': len(state['updates']), 'scope': 'entire sample through final evaluation; shared by matched arms'}
                budget_file = pair_dir / 'budget.json'
                if budget_file.exists() and read(budget_file) != budget:
                    raise ValueError('Recorded pair budget/history changed on resume')
                if not budget_file.exists():
                    atomic_save(budget_file, budget)
                complete = all((pair_dir / arm / 'result.json').exists() for arm in ARMS)
                if not complete:
                    public = Manifest(study / task['path'])
                    if image is None:
                        print(f'[task {task_index}/{len(tasks)}] {task["instance_id"]}', flush=True)
                        image, pulled = get_image(client, public)
                        expected_file = study / 'image-identities' / (task['key'] + '.json')
                        identity = {'image_id': image.id, 'task_sha256': task['sha256']}
                        if expected_file.exists() and read(expected_file) != identity:
                            raise ValueError('Online task image identity changed')
                        if not expected_file.exists():
                            atomic_save(expected_file, identity)
                        seconds = ensure_gold(study, task, image.id,
                            {'gold_seconds': min(config['gold_safety_seconds'], budget['cap_seconds'])}, False)
                        gold_status = 'passed' if seconds is not None else 'gold_blocked'
                    if gold_status == 'passed':
                        gold_proof(study / 'gold' / task['key'], public, image.id)
                        ensure_gate(study, pair, config['model'])
                    for arm in ARMS:
                        directory = pair_dir / arm
                        if existing_result(directory):
                            continue
                        if gold_status != 'passed':
                            atomic_save(directory / 'result.json', {'status': 'gold_blocked', 'seconds': None,
                                'joint_success': None, 'reason': 'Official gold preflight did not validate; not a fast completion'})
                            continue
                        opening = study / pair['opening_path']
                        if file_hash(opening) != pair['opening_sha256']:
                            raise ValueError('Frozen opening changed')
                        binding = study / pair['binding_path'] if pair.get('binding_path') else None
                        if binding and file_hash(binding) != pair['binding_sha256']:
                            raise ValueError('Frozen task binding changed')
                        print(f'[sample] {task["key"]}/{pair["persona"]}/{arm}; total cap={budget["cap_seconds"]:.3f}s', flush=True)
                        sample_result = supervise_sample(episode_command(study, task, config['model'], arm, pair['persona'], opening, binding),
                                                         directory, budget['cap_seconds'])
                        atomic_save(study / 'progress.json', {'updated_utc': datetime.now(timezone.utc).isoformat(),
                            'pid': os.getpid(), 'task_index': task_index, 'total_tasks': len(tasks),
                            'pair': task['key'] + '/' + pair['persona'], 'arm': arm,
                            'last_result_status': sample_result['status'], 'cap_seconds': budget['cap_seconds']})
                # Only the main arm updates P20. A pair's controls use its same
                # predeclared cap, including when resuming an interrupted batch.
                main = read(pair_dir / 'persona' / 'result.json')
                history.append({key: main.get(key) for key in ('status', 'seconds', 'cap_seconds')} |
                               {'pair_id': task['key'] + '/' + pair['persona'],
                                'result_sha256': file_hash(pair_dir / 'persona' / 'result.json')})
                state = advance_budget(state, history, config['online_max_seconds'])
                atomic_save(study / 'online-threshold.json', state)
            report(study, manifest)
        finally:
            if image:
                release_image(client, image, pulled, keep_images)


def report(study, manifest):
    inventory = resolved_inventory(study, manifest)
    time_file = study / "time-selection.json"
    selection = {r["task_key"]: r["status"] for r in read(time_file)} if time_file.exists() else {}
    cells = []
    for pair in inventory:
        for arm in ARMS:
            path = study / "episodes" / pair["task_key"] / pair["persona"] / arm
            if (path / "result.json").exists():
                result = read(path / "result.json")
            elif pair["status"] != "applicable":
                result = {"status": pair["status"], "reason": pair["reason"]}
            elif pair["task_key"] in selection and selection[pair["task_key"]] != "selected":
                result = {"status": selection[pair["task_key"]]}
                if result["status"] == "excluded_time_screen":
                    result.update(joint_success=0, score_basis="preregistered_reference_time_screen",
                                  measured_persona_timeout=False)
            else:
                result = {"status": "pending"}
            budget_file = path.parent / 'budget.json'
            budget = read(budget_file) if budget_file.exists() else {}
            cells.append({"task_key": pair["task_key"], "instance_id": pair["instance_id"],
                          "persona": pair["persona"], "arm": arm,
                          'threshold_seconds': budget.get('cap_seconds'), 'threshold_update': budget.get('update_index'),
                          "adapter_qualification_overlap": pair['instance_id'] in manifest.get('adapter_qualification_task_ids', []),
                          "record": path.relative_to(study).as_posix(), **result})
    summary = {"protocol": FULLSET_PROTOCOL, "tasks": len(manifest["tasks"]),
               'timing_mode': manifest['config'].get('timing_mode', 'reference'),
               "candidate_pairs": len(inventory), "planned_pair_arm_cells": len(cells),
               "by_arm": {arm: metric_summary([c for c in cells if c["arm"] == arm]) for arm in ARMS},
               "adapter_qualification_task_ids": manifest.get('adapter_qualification_task_ids', []),
               "by_development_overlap": {label: {
                   arm: metric_summary([c for c in cells if c['arm'] == arm and c['adapter_qualification_overlap'] == overlap])
                   for arm in ARMS} for label, overlap in (('qualification_seed_tasks', True), ('remaining_tasks', False))},
               "publication_ready": False,
               "publication_note": "Data collection is not finalization. Resolve coverage/readiness gaps and review validity, disclosures and checker evidence."}
    if manifest['config'].get('timing_mode') == 'online':
        main = [c for c in cells if c['arm'] == 'persona']
        summary['online_p20'] = censored_p20(main)
        summary['exact_fastest_twenty_percent_guaranteed'] = False
        summary['by_threshold_update'] = {str(epoch): metric_summary([c for c in main if c['threshold_update'] == epoch])
                                        for epoch in sorted({c['threshold_update'] for c in main if c['threshold_update'] is not None})}
    atomic_save(study / "results.json", cells)
    atomic_save(study / "summary.json", summary)
    import csv
    with (study / "review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        keys = ["task_key", "instance_id", "persona", "arm", "adapter_qualification_overlap", "status", "seconds", 'threshold_seconds', 'threshold_update', "base_task_success", "joint_success", "record"]
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cells)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--stage", choices=("all", "calibrate", "final", "report"), default="all")
    parser.add_argument("--study-dir", type=Path, default=DEFAULT)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument('--timing-mode', choices=('online', 'reference'), default='online')
    parser.add_argument('--initial-threshold-seconds', type=float, default=PEER_INITIAL_SECONDS)
    parser.add_argument('--online-max-seconds', type=float, default=3600)
    parser.add_argument('--update-every', type=int, default=20)
    parser.add_argument('--order-seed', default='multilang-online-p20-20260930')
    parser.add_argument("--reference-safety-seconds", type=float, default=600,
                        help="Initial adaptive calibration ceiling, NOT the final P20")
    parser.add_argument('--reference-max-seconds', type=float, default=7200,
                        help='Maximum calibration round ceiling; stop if P20 is still unidentified')
    parser.add_argument("--gold-safety-seconds", type=float, default=5400)
    parser.add_argument("--no-time-prefilter", action="store_true", help="Run all evaluable tasks with the frozen P20 cap")
    parser.add_argument("--keep-downloaded-images", action="store_true")
    parser.add_argument("--qualification-dir", type=Path,
                        help="Import all ten validated qualification histories before the final run")
    parser.add_argument("--admission-dir", type=Path,
                        help="Freeze reviewed public-fit decisions, bindings and matched openings")
    args = parser.parse_args(argv)
    if (not all(math.isfinite(x) for x in (args.initial_threshold_seconds, args.online_max_seconds))
            or not 0 < args.initial_threshold_seconds <= args.online_max_seconds or args.update_every < 1):
        parser.error('Invalid online budget bounds or update frequency')
    if args.timing_mode == 'online' and args.stage == 'calibrate':
        parser.error('Online mode has no all-task calibration stage; use --stage all')
    if (not all(math.isfinite(x) for x in (args.reference_safety_seconds, args.reference_max_seconds, args.gold_safety_seconds))
            or min(args.reference_safety_seconds, args.gold_safety_seconds) <= 0
            or args.reference_max_seconds < args.reference_safety_seconds):
        parser.error("Safety ceilings must be positive")
    config = {"expected_tasks": 1077, "model": args.model, "reference_safety_seconds": args.reference_safety_seconds,
              'reference_max_seconds': args.reference_max_seconds,
              'adaptive_timing': 'online_censored_p20' if args.timing_mode == 'online' else 'whole_population_rounds_then_monotone_top_k_upper_bound',
              'timing_mode': args.timing_mode, 'initial_threshold_seconds': args.initial_threshold_seconds,
              'online_max_seconds': args.online_max_seconds, 'update_every': args.update_every, 'order_seed': args.order_seed,
              "gold_safety_seconds": args.gold_safety_seconds, "quantile": .2,
              "prefilter": args.timing_mode == 'reference' and not args.no_time_prefilter, "workers": 1, "max_turns": 8, "max_steps": 14,
              "single_turn_steps": 40, "cpus": 4, "memory": "16g"}
    os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    os.environ["PATH"] = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin") + os.pathsep + os.environ["PATH"]
    study = args.study_dir.resolve()
    study.mkdir(parents=True, exist_ok=True)
    lock = study / "fullset.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit("Another run or stale interrupted lock exists; inspect " + str(lock))
    client, manifest = None, None
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "started_utc": datetime.now(timezone.utc).isoformat()}, handle)
        manifest = prepare(study, config)
        if args.qualification_dir and not (study / 'qualification').exists():
            import_qualification(study, manifest, args.qualification_dir)
        if args.admission_dir and not (study / 'admission' / 'frozen.json').exists():
            freeze_admission(study, manifest, args.admission_dir)
        ready = readiness(study, manifest)
        print(json.dumps(ready, indent=2), flush=True)
        report(study, manifest)
        if not args.run or args.stage == "report":
            print("Prepared only. No API calls or Docker jobs:", study)
            return 0
        if args.stage == "all" and not ready["final_queue_ready"]:
            raise RuntimeError("Refusing a final run with unresolved persona bindings. Read readiness.json; no paid calls started.")
        import docker
        client = docker.from_env(timeout=60)
        info = client.info()
        if info["OSType"] != "linux" or info["MemTotal"] < 18 * 1024**3:
            raise RuntimeError("Linux Docker with at least 18 GiB required")
        if client.containers.list(filters={'label': 'personacode.sample'}):
            raise RuntimeError('Another PersonaCode sample is still using Docker; finish or stop that owned run first')
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        if args.timing_mode == 'online':
            online_stage(study, manifest, client, args.keep_downloaded_images)
            report(study, manifest)
            return 0
        if args.stage in ("all", "calibrate"):
            refs = reference_stage(study, manifest, client, args.keep_downloaded_images)
        else:
            refs = load_references(study, manifest)
        threshold, selection = freeze_threshold(study, manifest, refs)
        if args.stage in ("all", "final"):
            final_stage(study, manifest, client, refs, threshold, selection, args.keep_downloaded_images)
        report(study, manifest)
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        atomic_save(study / "batch-error.json", {"error_type": type(exc).__name__, "message": str(exc)})
        print("[stopped]", str(exc), flush=True)
        return 2
    finally:
        if manifest:
            report(study, manifest)
        if client:
            client.close()
        lock.unlink(missing_ok=True)
        if args.run and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__ == "__main__":
    raise SystemExit(main())
