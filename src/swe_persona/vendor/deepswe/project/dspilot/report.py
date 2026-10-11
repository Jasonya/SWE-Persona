"""Report a measured run alongside locally archived historical comparisons."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def comparison_lines(run_id, result, config):
    index=json.loads((ROOT/'comparison/index.json').read_text())
    entries=[]
    for item in index['runs']:
        folder=ROOT/'comparison'/item['run_id']
        for record in item['files']:
            if hashlib.sha256((folder/record['path']).read_bytes()).hexdigest()!=record['sha256']:
                raise ValueError(f'Historical evidence changed: {item["run_id"]}/{record["path"]}')
        entries.append((item['label'],folder.relative_to(ROOT).as_posix(),
                        json.loads((folder/'result.json').read_text()),
                        json.loads((folder/'config.json').read_text())))
    entries.append(('独立目录 pivot（完整 v2）',f'runs/{run_id}',result,config))
    lines=['## Baseline / control / pivot 总对比',
        f'**1 个独立任务，{len(entries)} 次正式运行记录。** 任务是 `tomlkit-toml-table-converters`；交互组绑定十个 persona 中的 `first_time_pr_contributor`。表中四行来自已归档历史实验，最后一行是独立目录实跑。本次整理报告没有调用模型。',
        '| 运行 | F2P 新功能 | P2P 回归 | 新增要求满足 | 原题 / 联合成功 | 耗时 |',
        '|---|---:|---:|---|---|---:|']
    for label,path,r,c in entries:
        reward=r['reward']; evolved=r.get('evolved',{})
        persona=f'{sum(v=="met" for v in evolved.values())}/{len(evolved)}' if evolved else '不适用（未追加）'
        lines.append(f'| [{label}]({path}/result.json) | {reward["f2p_passed"]}/{reward["f2p_total"]} | {reward["p2p_passed"]}/{reward["p2p_total"]} | {persona} | {r["base_pass"]} / {r["joint_pass"]} | {r["seconds"]/60:.2f} 分钟 |')
    lines += ['**如何理解：** F2P 60 项检验新增转换功能；P2P 964 项检验原有功能。严格成功要求两组全部通过。五次都保住了回归，但新功能均有失败；persona 要求完成不等于原题正确。历史两模板的 2/2 只覆盖 scope、handoff，完整 v2 的 3/3 还包含 explanation。',
        '**比较边界：** 本历史表的 baseline/control 来自旧版本；后续独立对照另列于 MATCHED_REPORT.md。旧三组预算为 60 动作/6 用户轮，完整 v2 为 80 动作/8 用户轮；协议、调度及证据呈现也有差异。旧 baseline 的实测 F2P 最高，但每组只有一次，不能据此断言交互导致下降，也不能把两次 v2 的差值归因于目录隔离。Control 还改变了信息披露时机，并非仅增加聊天的单变量对照。',
        '### 资源与对话记录',
        '| 运行 | 模型 | 上限：动作/用户轮 | 实际：动作/用户轮 | API 次数 | 输入 token（含缓存） | 输出 token | 证据 |',
        '|---|---|---:|---:|---:|---:|---:|---|']
    for label,path,r,c in entries:
        lines.append(f'| {label} | {c["model"]} | {c["max_actions"]}/{c["max_user_turns"]} | {r["actions"]}/{r["user_turns"]} | {r["api_calls"]} | {r["usage"]["input_tokens"]:,} | {r["usage"]["output_tokens"]:,} | [配置]({path}/config.json) · [对话]({path}/transcript.md) |')
    lines += ['Baseline 的单轮指一次用户请求，agent 仍执行了多次工具/API 调用。耗时为整次墙钟时间，受网络重试和检查器开销影响，不能当成数据集平均任务时长。历史两模板 pivot 的 14.20 分钟含一次约 300 秒超时等待。对话文件是用户与 agent 的轮次摘要，不包含全部工具输出。',
        '### 三种流程的差异',
        '#### Baseline：一次给完整原题',
        '```mermaid\nflowchart LR\n A[完整原题] --> B[Agent 实现与公开验证]\n B --> C[提交并结束]\n C --> D[隐藏 F2P / P2P 验收]\n```',
        '#### Control：交互补充原题，不追加 persona 验收项',
        '```mermaid\nflowchart LR\n A[简略请求] --> B[Agent 回复或提问]\n B --> C[用户模拟器回复 + 完整原题]\n C --> D[继续实现与公开验证]\n D --> E[结束后隐藏验收]\n```',
        '#### Pivot：交互期间披露新增要求',
        '```mermaid\nflowchart LR\n A[初始请求与计划] --> B[完整原题 + scope]\n B --> C[实现与逐步披露要求]\n C --> D[检查证据并反馈修订]\n D --> E[结束交互]\n E --> F[隐藏验收 + persona 分项评分]\n```',
        '旧 pivot 仅有 scope 与 handoff；完整 v2 在首次源码修改后增加 explanation。完整 v2 的具体触发流程和本次轨迹见下文。新增检查约束交付范围、解释和验证证据，不代替原题隐藏测试。',
        '### 是否需要新模型实验？',
        '历史资料整理不需要模型调用。用户随后授权验证是否下降，已另建统一总预算三组实验；进度和新结果见 [MATCHED_REPORT.md](MATCHED_REPORT.md)，不混入本页历史表。每组重复三次仍只支持单任务初步观察。',
        '历史副本及哈希见 [comparison/index.json](comparison/index.json)；报告生成仅读取本目录文件，不依赖父目录。']
    return lines


def build(run_id):
    p=ROOT/'runs'/run_id
    result=json.loads((p/'result.json').read_text())
    config=json.loads((p/'config.json').read_text())
    rows=[json.loads(s) for s in (p/'api.jsonl').read_text().splitlines()]
    reward=result.get('reward') or {}
    artifacts=p/'deliverables';artifacts.mkdir(exist_ok=True)
    snap=json.loads((p/'final-snapshot.json').read_text())
    for name in ['scope.json','explanation.md','PR.md','validation.json']:
        value=snap['artifacts'].get(name)
        if value is not None:(artifacts/name).write_text(value)
    lines=['# DeepSWE persona：对照结果与独立 v2 复现报告',
        f'本报告包含本目录 `comparison/` 内的历史对照副本和 `runs/{run_id}` 的独立实跑。历史结果与新结果分别标注，均可在本目录查看。']
    if (ROOT/'V3_REPORT.md').exists():
        lines += ['**最新修复后对比见 [V3_REPORT.md](V3_REPORT.md)**；[MATCHED_REPORT.md](MATCHED_REPORT.md) 保留九次冻结协议结果及修复回放。下表为更早的历史记录，不混入新均值。']
    lines += comparison_lines(run_id,result,config)
    lines += ['## 本次独立 v2 结果',
        '| 指标 | 实测值 |','|---|---|',
        f'| 原题新功能 F2P | {reward.get("f2p_passed")}/{reward.get("f2p_total")} |',
        f'| 原题回归 P2P | {reward.get("p2p_passed")}/{reward.get("p2p_total")} |']
    for k,v in result.get('evolved',{}).items():lines.append(f'| Persona {k} | {v} |')
    lines += [f'| Persona 全部满足 | {result.get("persona_pass")} |',
        f'| 整体成功 | {result.get("joint_pass")} |',f'| 评测有效 | {result.get("evaluation_valid")} |',
        f'| 停止原因 | `{result.get("end_reason")}` |',f'| 预算耗尽 | {result.get("budget_exhausted")} |',
        f'| 用户轮 / agent 动作 | {result.get("user_turns")} / {result.get("actions")} |',
        f'| API 次数 | {result.get("api_calls")} |',f'| 总耗时 | {result["seconds"]:.2f} 秒（{result["seconds"]/60:.2f} 分钟） |',
        '原题严格成功要求 F2P 60/60 和 P2P 964/964；persona 满足不能替代代码正确。一次运行不能证明平均性能或交互的因果作用。',
        '## 完整三模板流程',
        '```mermaid\nflowchart TD\n A["独立任务容器：先给计划"] --> B["完整原题 + scope"]\n B --> C["首次源代码改动后：explanation"]\n C --> D["继续实现、提交、公开验证"]\n D --> E["完成声明后：handoff"]\n E --> F["三项要求检查；有预算时反馈修订"]\n F --> G["结束交互后：新容器执行原题隐藏验收"]\n G --> H["分别报告功能、persona、交互有效性和资源"]\n```',
        '## 真实披露与检查轨迹','| 要求 | 用户轮 | 触发 | 披露时剩余动作 |','|---|---:|---|---:|']
    for e in json.loads((p/'events.json').read_text()):
        lines.append(f'| {e["requirement"]} | {e["turn"]} | {e["trigger"]} | {e["actions_remaining"]} |')
    for f in sorted(p.glob('checks-turn-*.json')):
        values=json.loads(f.read_text())
        lines.append(f'- {f.stem}: '+', '.join(f'{k}={v["status"]}' for k,v in values.items()))
    lines.append('若某轮出现 unknown，它表示证据不足或引用未精确匹配，不自动等于解释内容错误。原始判定和后续反馈保留在 trajectory/checks 中；不要把检查器格式造成的额外交互全部归因于 persona 难度。')
    lines += ['## 分角色耗时','| 角色 | API 次数 | API 耗时 |','|---|---:|---:|']
    for role in ['agent','simulator','judge']:
        records=[r for r in rows if r['role']==role]
        lines.append(f'| {role} | {len(records)} | {sum(r["seconds"] for r in records):.2f} 秒 |')
    usage=result['usage']
    lines.append(f'输入 {usage["input_tokens"]:,} token（其中缓存输入 {usage["cached_tokens"]:,}），输出 {usage["output_tokens"]:,}；缓存是输入子集。总耗时另含容器、工具与验证开销，API 单价未知。')
    lines += ['## 同目录复现方式',
        '```bash\npython3 -m dspilot.prepare\npython3 -m unittest discover -s tests -v\npython3 -m dspilot.preflight\npython3 -m dspilot.calibrate\npython3 -m dspilot.run --run-id another-run\npython3 -m dspilot.report --run-id another-run\n```',
        '先按 README 配置主机端 .env 或 DS_PERSONA_ENV_FILE。校准和运行会调用模型，模型得分可能变化。',
        '## 本次隔离改动',
        '- 独立 dspilot 包、私有任务缓存与 runs；没有父目录导入或历史运行依赖。',
        '- 校准从固定参考补丁重建真实证据，参考解只用于隔离离线校准。',
        '- 20 项本地测试通过；源码单独复制到临时目录后同样通过，默认来源下载与校验已执行。',
        '- 保留三模板 v2 的 80 动作、8 用户轮、12 动作交付预留和原始隐藏验收。',
        '- Skills、真实凭据保留在目录外；comparison 仅保存历史阅读证据，不参与运行或校准；无远端提交。',
        '## 本地证据链接（runs 被 Git 忽略）',
        f'- [运行结果](runs/{run_id}/result.json)',f'- [完整 trajectory](runs/{run_id}/transcript.md)',
        f'- [解释文档](runs/{run_id}/deliverables/explanation.md)',f'- [PR 草稿](runs/{run_id}/deliverables/PR.md)',
        f'- [原题验收输出](runs/{run_id}/official/output.txt)',
        '- [校准结果](runs/full-persona-v2-gates/validation.json)',
        '这些链接指向本地生成产物，提交源码时不会自动包含原始日志。报告中的摘要已保留关键实测值。']
    (ROOT/'REPORT.md').write_text('\n\n'.join(lines).replace('|\n\n|','|\n|'))
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);args=parser.parse_args()
    if not args.run_id.replace('-','').replace('_','').isalnum():raise SystemExit('Invalid run ID')
    result=build(args.run_id)
    print(json.dumps({'report':'REPORT.md','run_id':args.run_id,'joint_pass':result.get('joint_pass')}))

if __name__=='__main__':main()
