"""Explicit delivery metadata derived from existing rules; no new grading policy."""
import json
import tomllib
from . import delivery_runtime as rt

VERSION = 'deepswe-delivery-metadata-v1'


def reference(path, pointer=None):
    return {'path': path, 'json_pointer': pointer, 'base': 'tests/persona'}


def write_task_readme(folder, pair):
    """Append pair-specific configuration instructions before release sealing."""
    ext = folder / 'tests/persona'
    plan = rt.read(ext / 'legacy-plan.json')
    original = tomllib.loads((ext / 'original/task.toml').read_text())
    from .delivery_timeouts import verifier_seconds
    filename = pair['task_id'] + '--' + pair['persona_id'] + '.json'
    budgets = json.dumps({'budgets': plan['budgets']}, ensure_ascii=False, indent=2)
    selection = json.dumps({'pairs': [{'task_id': pair['task_id'], 'persona_id': pair['persona_id'],
        'plan': 'data/staged_bindings/' + filename}]}, ensure_ascii=False, indent=2)
    section = f'''

---

## DeepSWE 时限配置与运行入口

Task：`{pair['task_id']}`；Persona：`{pair['persona_id']}`；运行 pair ID：`{pair['delivery_id']}`。

### 当前配置（秒）

| 配置 | 本 pair 当前值 | 作用与边界 |
|---|---|---|
| `agent.timeout_sec` | {original.get('agent', {}).get('timeout_sec', '未声明')} | 原题时限；未显式覆盖时 Pier-native 沿用此值，不是整场实验总墙钟上限。 |
| 实际 Pier agent 时限 | {plan.get('pier_agent_timeout_sec', original.get('agent', {}).get('timeout_sec', '未声明'))} | 默认沿用原题；显式覆盖存入冻结计划 `pier_agent_timeout_sec`，再写入实际执行的 Pier task.toml。 |
| `budgets.interaction_seconds` | {plan['budgets'].get('interaction_seconds', '未声明')} | 交互客户端 deadline，从 episode 开始计算，准备和开场耗时也会消耗这一窗口。 |
| `budgets.final_review_seconds` | {plan['budgets'].get('final_review_seconds', '未声明')} | 结束后的独立 Persona 终审客户端预算。 |
| 原题 `verifier.timeout_sec` | {original['verifier']['timeout_sec']} | 原始配置留档，实际覆盖值见下一行。 |
| 实际 Pier verifier 时限 | {verifier_seconds(plan, original['verifier']['timeout_sec'])} | 默认最多 900 秒；只尝试一次，超时不重试，取消及容器清理可能额外耗时。 |

轮次、动作、模型调用次数以及工具自身的 timeout 也会限制运行；只增加秒数不保证继续运行。
当前实现没有统一的整个实验墙钟上限。下述配置入口可设置四个时限；`run.py` 不接受通用 `--timeout`。

### 本 task 的设置接口

当前生效配置：[runtime-settings.json](tests/persona/runtime-settings.json)。从本 task 目录执行：

```sh
python -B tests/persona/configure_runtime.py --show
python -B tests/persona/configure_runtime.py --verifier-seconds 900 --output /path/to/PersonaCodeBench-DeepSWE-verifier15-vNEXT
python -B tests/persona/configure_runtime.py --pier-agent-seconds 7200 --interaction-seconds 3600 --final-review-seconds 900 --output /path/to/PersonaCodeBench-DeepSWE-timeouts-vNEXT
```

设置命令需要打包依赖（见数据集 requirements-harbor-packaging.lock），只重新打包，不调用模型或 Docker。
它为本 pair 设置秒数，保留其余 pair 的配置，生成完整的新输入包；旧包和旧结果保持不变。
新包需要重新运行并冻结完整开场。显式设置 Pier agent 或 verifier 时限后必须使用 `--runtime pier_native`，避免其他后端忽略设置。
原始 task.toml 留档保持不变；覆盖值在冻结计划、runtime-settings.json 和实际运行的 adapter-overrides.json 中明确记录。
也可从数据集根目录使用 `python -B configure_runtime.py --pair {pair['delivery_id']} ...`。

### 其他已有时限及设置范围

- Pier verifier：`--verifier-seconds` 设置一次完整验收（包括 verifier 环境准备）的总预算，默认最多 900 秒且不重试。原题测试与评分方式保留，超时不生成通过分数。collect 在验收计时前独立执行；取消和容器清理可能额外耗时。
- 公开探针：绑定中的 `public_probes[].timeout_seconds`；分阶段覆盖位于计划 `public_probe_overlays[].probe.timeout_seconds`，覆盖值须为 1–600 秒。需在源码工作区修改并重新打包，不在上述四个 CLI 参数内。
- 单次 API 请求：客户端最多等待 300 秒，同时受该客户端剩余 deadline 限制；当前是实现常量，没有角色环境变量配置入口。
- 最小在线 Judge 校准：300 秒、最多 32 次调用；可选 UX Judge：120 秒、最多 2 次调用。当前为实现常量，没有逐题配置参数。
- 工具执行有独立命令 timeout，具体值随调用点变化；增加交互预算不会自动增加工具超时。

上述 deadline 不等于强制终止全部同步操作的全程墙钟计时器；总耗时还包含准备、验收和清理。

### 查看与修改入口

- 本任务计划：[tests/persona/legacy-plan.json](tests/persona/legacy-plan.json)，查看 `budgets`。
- 交付协议副本：[protocol.json](tests/persona/protocol.json)；策略副本：[policy.json](tests/persona/closed_loop/policy.json)。这些由生成器同步，不能各自修改。
- 原题配置：[task.toml](task.toml)；原始留档：[original/task.toml](tests/persona/original/task.toml)。保留其原始限时；实际 Pier agent/verifier 覆盖通过上述专用入口设置。
- 在**未冻结的源码工作区** `deepswe_persona_pilot/` 中，编辑 `data/staged_bindings/{filename}` 的 `budgets`。若使用自定义 `--source-plan` 或 selection 的 `plan`，应编辑实际选中的那个计划。
- 包内运行器计划副本：[runtime plan](../../runtime/project/data/staged_bindings/{filename})，仅供核对，不要直接修改已冻结包。

当前完整 budgets 如下；编辑时保留其他字段，只替换需要调整的正整数值：

```json
{budgets}
```

例如将 `interaction_seconds` 改为 `3600` 表示 60 分钟；`final_review_seconds` 改为 `900` 表示 15 分钟。这只是设置示例，不是本包当前值。

### 修改后生成新版本

1. 在源码工作区修改上述计划；不要修改旧包、原题缓存或旧运行记录。
2. 在模块根目录新建 `selection-timeout.json`，单 pair 示例：

```json
{selection}
```

3. 使用已安装项目打包依赖的 Python，从 `deepswe_persona_pilot/` 生成**全新输入包**：

```sh
python -B -m dspilot.export_deepswe_release --selection selection-timeout.json --output /path/to/PersonaCodeBench-DeepSWE-timeout-vNEXT
```

打包会同步计划、协议、策略、README 和校验记录。批次可在 selection 中放多个 pair；目前没有覆盖全部 pair 的统一时限参数。
这条命令准备新输入，完整开场会在新运行中冻结；它不是给旧实验补写新预算。

### 运行本 pair

在新包的 `dataset/` 根目录执行（不要在此 task 目录执行）：

```sh
python -B run.py --pair {pair['delivery_id']} --dry-run
python -B run.py --pair {pair['delivery_id']} --runtime pier_native --calibrate --env-file /path/to/host.env --output /path/to/new-run
```

第二条命令会调用模型并启动 Docker。先安装包内锁定的 Pier 依赖，凭据仅放在宿主环境。
运行器在新的 work 目录冻结执行计划。修改预算后使用新的包和输出目录；`--resume` 只用于同一配置。
源配置及实际执行配置分别保存在 work 目录的 `project/runs/staged-pipelines/<pipeline-id>/frozen/staged-plan.json`
和 `project/runs/pier-tasks/<run-id>/task.toml`，时限保留策略记录在同目录的 `adapter-overrides.json`。
'''
    # The original problem text remains the first section, unmodified.
    path = folder / 'README.md'
    path.write_bytes(path.read_bytes() + section.encode('utf-8'))


def enrich_pair(folder, pair, rules, opening_status, binding_review=None):
    ext = folder / 'tests/persona'
    plan = rt.read(ext / 'legacy-plan.json')
    persona = rt.read(ext / 'persona.json')
    binding = rt.read(ext / 'binding.json')
    templates = {t['id']: (i, t) for i, t in enumerate(persona['attributes']['evolved_requirements'])}
    requirements = []
    for index, rule in enumerate(rules):
        req = rule['effective_requirement']; life = rule['lifecycle']
        template_index, template = templates[req['template']]
        params = req.get('checker', {}).get('parameters', {})
        paths = list(rule.get('artifact_paths', params.get('artifacts', [])))
        if params.get('artifact'): paths.append(params['artifact'])
        paths += [v[len('artifact:'):] for v in params.get('required_evidence', []) if v.startswith('artifact:')]
        paths = sorted(set(paths))
        requirements.append({'requirement_id': rule['requirement_id'], 'disclosure': life['statement'],
            'effective_statement': req['statement'], 'lifecycle': life,
            'persona_basis': {'persona_id': persona['id'], 'template_id': template['id'],
                'template': template, 'source': reference('persona.json', f'/attributes/evolved_requirements/{template_index}')},
            'trigger': {'declared': req.get('trigger'), 'runtime': req.get('runtime_trigger'),
                'stage': life['stage'], 'acceptance': life.get('acceptance')},
            'dependencies': {'task_units': life.get('task_dependencies', []),
                'source': reference('legacy-plan.json', '/requirements'),
                'scope': 'Recorded task dependencies; no inferred requirement dependency graph'},
            'artifacts': {'paths': paths, 'path_base': '/deliverables',
                'status': 'declared' if paths else 'no_explicit_artifact_path',
                'scope': 'Existing checker artifact names and registered defaults; no new deliverable obligation',
                'source': reference('closed_loop/rubric.json', f'/requirements/{index}/artifact_paths')},
            'checker': req.get('checker'), 'rule': reference('closed_loop/rubric.json', f'/requirements/{index}')})
    rt.dump(ext / 'evolved_requirements.json', {'schema_version': VERSION,
        'task_id': pair['task_id'], 'persona_id': pair['persona_id'], 'requirements': requirements})
    interaction = rt.read(ext / 'interaction.json')
    interaction.update(schema_version=VERSION, task_id=pair['task_id'], persona_id=pair['persona_id'],
        expectations={'status': 'recorded' if 'interaction_preferences' in persona['attributes'] else 'not_recorded',
            'interaction_preferences': persona['attributes'].get('interaction_preferences'),
            'source': reference('persona.json', '/attributes/interaction_preferences')},
        frozen_opening={'status': opening_status,
            'instruction': '../../instruction.md' if opening_status == 'frozen_before_first_agent_action' else None,
            'receipt': 'receipts/opening.json' if opening_status == 'frozen_before_first_agent_action' else None,
            'base': 'tests/persona'})
    rt.dump(ext / 'interaction.json', interaction)
    blur = rt.read(ext / 'blurred_task.json')
    source_blur = ext / 'receipts/blur/task-blur.json'
    source_summary = rt.read(source_blur).get('stage_summary') if source_blur.exists() else None
    blur['stage_summary'] = source_summary or {
        'status': 'not_recorded_before_persona_binding', 'value': None,
        'reason': 'Task-only construction did not freeze a stage summary; bound stages must not be backdated.'}
    blur['bound_stage_summary'] = {'construction_phase': 'derived_after_persona_binding',
        'source': reference('legacy-plan.json', '/stages'), 'stages': plan['stages']}
    rt.dump(ext / 'blurred_task.json', blur)
    review = {'schema_version': VERSION, 'task_id': pair['task_id'], 'persona_id': pair['persona_id'],
        'status': 'not_certified', 'semantic_approval_claimed': False,
        'source_binding_sha256': rt.sha(ext / 'binding.json'),
        'binding': reference('binding.json'),
        'source_review_fields': {k: binding[k] for k in ('review', 'review_status', 'audit', 'validation_status') if k in binding},
        'reason': 'Source fields are preserved as evidence only; export does not perform or upgrade semantic review.'}
    from .delivery_binding_review import attach
    review = attach(review, binding_review, binding, plan, persona)
    rt.dump(ext / 'receipts/binding/review.json', review)
    identity = rt.read(ext / 'receipts/binding/source-identity.json')
    identity['review_record'] = 'review.json'
    rt.dump(ext / 'receipts/binding/source-identity.json', identity)
    provenance = rt.read(ext / 'provenance.json')
    provenance['construction_review'] = reference('receipts/binding/review.json')
    rt.dump(ext / 'provenance.json', provenance)
    rt.dump(ext / 'closed-loop-provenance.json', {'schema_version': VERSION,
        'dataset_family': 'deepswe', 'task_id': pair['task_id'], 'persona_id': pair['persona_id'],
        'source_protocol': plan['protocol'], 'tb3_v5_lineage': 'not_applicable',
        'source_files': {name: rt.sha(ext / name) for name in ('binding.json', 'persona.json', 'legacy-plan.json')},
        'source_files_base': 'tests/persona', 'source_receipt': 'receipts/source-protocol-deepswe.json',
        'review': reference('receipts/binding/review.json'),
        'execution_status': 'recorded_trials' if pair['trials'] else 'not_run',
        'trials': pair['trials'], 'trial_paths_base': 'outer_package_root',
        'opening_status': opening_status, 'validation': {'semantic_review': 'not_certified',
            'live_execution_performed_by_exporter': False}})


def enrich_release(dataset, rows, release_name):
    manifest = rt.read(dataset / 'release-manifest.json')
    manifest.update(metadata_version=VERSION, release_name=release_name,
        source={'path': 'source-deepswe.json', 'sha256': rt.sha(dataset / 'source-deepswe.json')},
        paths_relative_to='dataset_root',
        validation={'offline_packaging': 'passed_when_release_is_committed',
            'official_harbor_report': 'provenance/harbor-validation.json',
            'semantic_review': 'not_certified', 'live_execution_performed_by_exporter': False},
        task_entries=[])
    for pair in rows:
        folder = rt.task_path(dataset, pair); ext = folder / 'tests/persona'
        names = ['README.md', 'instruction.md', 'task.toml'] + ['tests/persona/' + name for name in (
            'persona.json', 'binding.json', 'legacy-plan.json', 'evolved_requirements.json', 'interaction.json',
            'protocol.json', 'closed-loop-provenance.json', 'closed_loop/rubric.json', 'closed_loop/manifest.json')]
        manifest['task_entries'].append({'pair_id': pair['delivery_id'], 'task_id': pair['task_id'],
            'persona_id': pair['persona_id'], 'task_directory': 'tasks/' + pair['delivery_id'],
            'source': rt.read(ext / 'provenance.json')['source'],
            'files_base': 'task_directory', 'files': {name: rt.sha(folder / name) for name in names},
            'opening_status': rt.read(ext / 'interaction.json')['opening_status'],
            'execution_status': 'recorded_trials' if pair['trials'] else 'not_run',
            'review_status': 'not_certified'})
    rt.dump(dataset / 'release-manifest.json', manifest)
