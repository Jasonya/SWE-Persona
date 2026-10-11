"""One source fingerprint implementation for runtime gates and offline inventory."""
import hashlib
import json
from . import specs

RUNTIME = ('__init__ api api_config api_codec coding_budget specs task sandbox checks full_persona ledger protocol run '
           'launch matrix run_v3 checks_v3 calibrate_v3 calibrate negative_controls '
           'fixture_author interaction_policy integrity fingerprint workspace_evidence check_contracts source_scope').split()


def episode_manifest(episode):
    root=specs.ROOT
    paths=[root/'dspilot'/f'{name}.py' for name in RUNTIME]
    paths += [root/'data/personas.v3.json',root/'data/instruction.md',root/'sources.lock.json',
              episode.task.path,episode.binding.path,episode.task.contract_path]
    paths += [root/p for p in episode.binding.raw.get('calibration',{}).get('positive_artifacts',{}).values()]
    if episode.binding.raw.get('staged'):
        from .coding_backend import backend_sources
        paths += backend_sources(root)
        paths += [root/'dspilot'/f'{name}.py' for name in
                  ('task_workflow','resource_probe','runbook_probe','staged','staged_cli','checks_staged','run_staged','simulator_staged','recovery_staged','events_staged','probes_staged','fixture_repairs','calibrate_staged','report_policy')]
        selected=root/episode.binding.raw.get('staged_plan_path',
            f'data/staged_bindings/{episode.task.id}--{episode.persona["id"]}.json')
        paths += [selected]
        if selected.name=='staged-plan.json':
            paths += [root/'dspilot/planning_staged.py',root/'dspilot/disclosure_design.py',root/'dspilot/staged_pipeline.py',
                      selected.parent/'admission.json']
            receipt=json.loads((selected.parent/'admission.json').read_text())
            if receipt.get('admission_policy')=='frozen_configuration_basic_checks':
                paths += [root/'dspilot/frozen_staged.py',selected.parent/'basic-checks.json',selected.parent/'advisory.json']
            else:
                paths += [selected.parent/'review.json',selected.parent/'runtime-certificate.json',selected.parent/'design.json',
                          selected.parent/'schedule.json',selected.parent/'details.json',
                          selected.parent.parent/'screen.json',selected.parent.parent/'context.json']
                paths += sorted(selected.parent.glob('review-attempt-*.json'))
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in dict.fromkeys(paths)}
