"""Freeze existing plans and check executable structure; never call a model.

Design/screen/review documents are provenance, not execution admission. Historical
attempts remain immutable. A new runtime version requires a new freeze, not a new
model review. Environment checks run separately and before runtime model calls.
"""
import copy
import json
import shutil
from pathlib import Path
import subprocess
import uuid

from .coding_backend import backend_metadata, verify_backend, backend_sources
from .runtime_backend import runtime_metadata, verify_runtime
from . import specs
from .api import config, public_routes
from .planning_staged import digest, dump, seed_path, source_manifest
from .staged import CALIBRATION_POLICY, adapt_episode, validate_plan

VERSION = 'frozen-staged-v1'
ADMISSION_POLICY = 'frozen_configuration_basic_checks'
REVIEW_POLICY = 'advisory_only'


def runtime_sources(ep):
    sources = source_manifest(ep)
    for path in sorted((specs.ROOT/'dspilot').glob('*.py')) + backend_sources(specs.ROOT):
        sources[str(path.relative_to(specs.ROOT))] = digest(path)
    return sources


def basic_checks(plan, ep):
    """Registered schema, exact task coverage, stage order and reference checks."""
    validate_plan(plan, ep)
    adapt_episode(ep, plan=plan)
    if plan.get('blocked_reasons'):
        raise ValueError('Configuration has unresolved fixture blockers: '+ '; '.join(plan['blocked_reasons']))
    return {'passed': True, 'checks': ['configuration_complete', 'registered_checkers_and_probes',
        'complete_original_task', 'ordered_stages', 'staged_persona',
        'disclosed_requirement_references', 'authorization_boundaries'],
        'semantic_review_required': False, 'calibration_required': False,
        'note': 'Structural validity does not establish semantic correctness or task/persona success.'}


def persona_exclusion(advisory):
    """Retain a prior applicability caveat without preventing execution."""
    screen=advisory.get('screen',{}).get('result',{})
    if (advisory.get('status')=='not_applicable' or
            advisory.get('source_state',{}).get('status')=='not_applicable' or
            screen.get('decision')=='not_applicable'):
        return 'Prior screening did not establish persona applicability; exploratory execution only.'
    return None


def environment_check(ep):
    """Check cache, local image and baseline tools in an isolated throwaway container.

    No dependency installation, tests, reference code, model requests or source
    modifications. The running episode still performs its normal setup/checks.
    """
    from .prepare import verify_cache
    verify_cache(ep.task.id)
    from .coding_backend import selected_backend
    if selected_backend()=='terminus2_native':
        from .terminus_dependency import check_image
        check_image(ep.task.image)
    config()  # Check host credentials exist; never serialize their values.
    name = 'persona-preflight-'+uuid.uuid4().hex[:12]
    def call(args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError('Environment unavailable: Docker '+args[0]+' failed')
        return result.stdout.strip()
    image_id = call(['image','inspect','--format','{{.Id}}',ep.task.image])
    resources = ep.task.raw.get('environment', {})
    try:
        call(['run','--rm','--runtime=runc','-e','NVIDIA_VISIBLE_DEVICES=void','-e','CUDA_VISIBLE_DEVICES=','--name',name,'--platform','linux/amd64','--network','none',
              '--cpus',str(resources.get('cpus',2)), '--memory',str(resources.get('memory','4g')),
              '--pids-limit',str(resources.get('pids_limit',256)),
              '--entrypoint','bash',image_id,'-lc',
              'command -v git >/dev/null && command -v python3 >/dev/null && command -v timeout >/dev/null && '
              'git -c safe.directory=/app -C /app cat-file -e "$1^{commit}"',
              'preflight',ep.task.base_commit])
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
    return {'passed': True, 'task_id':ep.task.id, 'image':ep.task.image, 'image_id':image_id,
            'checks':['pinned_cache','host_api_configuration','container_start','baseline_commit','runtime_tools'],
            'model_endpoint_tested':False, 'task_tests_run':False}


def freeze_plan(ep, directory, source, *, advisory=None, delivery_blur=None, delivery_opening=None):
    from .delivery_capture import prepare_blur, verify_blur, verify_opening, OPENING_FILES
    delivery_blur = delivery_blur or prepare_blur(specs.ROOT, ep.task.id)
    verify_blur(delivery_blur, ep.task.id)
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=False)
    source=Path(source).resolve()
    plan=json.loads(source.read_text())
    out=directory/'frozen'; out.mkdir()
    opening_hashes = {}
    if delivery_opening is not None:
        delivery_opening = Path(delivery_opening)
        verify_opening(delivery_opening, task_id=ep.task.id, persona_id=ep.persona['id'], arm='pivot', plan=plan)
        if (delivery_blur['blur'] != json.loads((delivery_opening/'task-blur.json').read_text())
                or delivery_blur['receipt'] != json.loads((delivery_opening/'blur-receipt.json').read_text())):
            raise ValueError('Replayed opening and frozen blur differ')
        dest = out/'delivery-opening'; dest.mkdir()
        for name in OPENING_FILES:
            shutil.copy2(delivery_opening/name, dest/name)
            opening_hashes[name] = digest(dest/name)
    dump(out/'delivery-blur.json', delivery_blur)
    path=out/'staged-plan.json'; dump(path,plan)
    notes=copy.deepcopy(advisory or {})
    notes.update(source_plan=str(source.relative_to(specs.ROOT)),source_plan_sha256=digest(source))
    dump(out/'advisory.json',notes)
    state={'version':VERSION,'task_id':ep.task.id,'persona_id':ep.persona['id'],
           'plan':str(path.relative_to(specs.ROOT)), 'admission_policy':ADMISSION_POLICY,
           'review_policy':REVIEW_POLICY, 'preparation_warnings':notes,
           'calibration_policy':CALIBRATION_POLICY,'calibration_status':'not_run','model_calibrated':False}
    try:
        checks=basic_checks(plan,ep)
        dump(out/'basic-checks.json',checks)
        cfg=config()
        receipt={'version':VERSION,'approved':True,'admission_policy':ADMISSION_POLICY,
                 'review_policy':REVIEW_POLICY,'requires_calibration':False,'calibration_policy':CALIBRATION_POLICY,
                 'plan_sha256':digest(path),'basic_checks_sha256':digest(out/'basic-checks.json'),
                 'delivery_blur_sha256':digest(out/'delivery-blur.json'),
                 'delivery_opening_files':opening_hashes,
                 'advisory_sha256':digest(out/'advisory.json'), 'sources':runtime_sources(ep),
                 'models':{k:v for k,v in cfg.items() if k.endswith('_MODEL')},
                 'api_routes':public_routes(cfg),
                 'coding_backend':backend_metadata(), 'runtime_backend':runtime_metadata()}
        dump(out/'admission.json',receipt)
        state.update(status='prepared',reasons=[])
    except (ValueError,KeyError,TypeError,OSError) as exc:
        dump(out/'basic-checks.json',{'passed':False,'error':str(exc)})
        state.update(status='basic_check_failed',reasons=[str(exc)])
    dump(directory/'state.json',state)
    return state


def verify(path, ep):
    path=Path(path).resolve()
    path.relative_to((specs.ROOT/'runs/staged-pipelines').resolve())
    if path.name!='staged-plan.json':raise ValueError('Expected frozen staged-plan.json')
    out=path.parent
    receipt=json.loads((out/'admission.json').read_text())
    if (receipt.get('version')!=VERSION or receipt.get('approved') is not True or
            receipt.get('admission_policy')!=ADMISSION_POLICY or receipt.get('review_policy')!=REVIEW_POLICY or
            receipt.get('requires_calibration') is not False or receipt.get('calibration_policy')!=CALIBRATION_POLICY):
        raise ValueError('Invalid frozen configuration receipt')
    for key,file in [('plan_sha256',path),('basic_checks_sha256',out/'basic-checks.json'),('advisory_sha256',out/'advisory.json')]:
        if receipt.get(key)!=digest(file):raise ValueError('Frozen configuration/evidence changed')
    if receipt.get('delivery_blur_sha256') and receipt['delivery_blur_sha256'] != digest(out/'delivery-blur.json'):
        raise ValueError('Frozen delivery blur changed')
    opening_hashes = receipt.get('delivery_opening_files', {})
    if bool(opening_hashes) != (out/'delivery-opening').exists():
        raise ValueError('Frozen opening presence changed')
    if opening_hashes:
        from .delivery_capture import OPENING_FILES
        if set(opening_hashes) != set(OPENING_FILES): raise ValueError('Incomplete frozen opening')
        for name, expected in opening_hashes.items():
            if digest(out/'delivery-opening'/name) != expected: raise ValueError('Frozen opening changed')
    if receipt.get('sources')!=runtime_sources(ep):
        raise ValueError('Runtime sources changed; freeze a new version (no model review needed)')
    verify_backend(receipt)
    verify_runtime(receipt)
    basic_checks(json.loads(path.read_text()),ep)
    return receipt


def select_source(row):
    """Use the selected compiled proposal, even if review rejected it.

    A screening-only attempt may have no compiled proposal. Its registered plan
    is then selected explicitly and this fallback is recorded, never hidden.
    """
    from .staged_pipeline import directory_for
    ep=specs.compile_episode(row['task_id'],row['persona_id'])
    notes={k:copy.deepcopy(row[k]) for k in ('status','reasons','missing_information') if k in row}
    if row.get('pipeline_id'):
        directory=directory_for(row['pipeline_id'])
        state=json.loads((directory/'state.json').read_text())
        if (state['task_id'],state['persona_id'])!=(ep.task.id,ep.persona['id']):
            raise ValueError('Source pipeline identity mismatch')
        notes['source_pipeline_id']=row['pipeline_id']
        notes['source_state']={k:state[k] for k in ('status','reasons','missing_information','feedback') if k in state}
        if row.get('plan') or state.get('plan'):
            path=specs.ROOT/(row.get('plan') or state['plan'])
            if not path.is_file():raise ValueError('Selected source plan missing; no silent fallback')
        else:
            proposals=sorted(directory.glob('revision-*/staged-plan.json'))
            path=proposals[-1] if proposals else seed_path(ep)
            notes['selection']='latest_compiled_proposal' if proposals else 'registered_plan_no_compiled_proposal'
        for name,where in [('screen',directory/'screen.json'),('review',path.parent/'review.json')]:
            if where.is_file():notes[name]=json.loads(where.read_text())
    else:
        path=specs.ROOT/row['plan'] if row.get('plan') else seed_path(ep)
        notes['selection']='explicit_plan' if row.get('plan') else 'registered_plan'
    path=path.resolve()
    path.relative_to(specs.ROOT)
    if not path.is_file():raise FileNotFoundError('Selected source plan missing; no silent fallback')
    return ep,path,notes
