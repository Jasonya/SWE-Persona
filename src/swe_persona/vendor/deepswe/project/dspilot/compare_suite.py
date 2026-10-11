"""Run the preregistered nine episodes sequentially and report every result."""
import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from . import compare_run
from .api import config
from .prepare import verify_cache
from .protocol import dump
from .task import ROOT

ORDER=[['baseline','control','pivot'],['control','pivot','baseline'],['pivot','baseline','control']]

def summarize(folder):
    plan=json.loads((folder/'plan.json').read_text())
    records=[]
    for item in plan['episodes']:
        p=ROOT/'runs'/item['run_id']/'result.json'
        if p.exists():records.append(item|{'result':json.loads(p.read_text())})
    lines=['# 新版三组重复实验：统一总预算',
           f'计划 1 个任务 × 3 组 × 3 次 = 9 次；目前有 {len(records)} 次结果。历史记录不混入这张表。',
           '模型 gpt-5.6-luna；80 动作、8 用户轮、150 次总 API 尝试、45 分钟请求期限。Baseline 只接收一次用户请求，但可用全部 80 动作。三组均无分阶段动作预留。',
           '## 每次结果',
           '| 重复 | 组别 | F2P | P2P | Persona | 原题通过 | 评测有效 | 分钟 | 动作 / 用户轮 | API 次数 | 结束原因 |',
           '|---:|---|---:|---:|---|---|---|---:|---:|---:|---|']
    for item in records:
        r=item['result']; w=r.get('reward') or {}; ev=r.get('evolved',{})
        persona=f'{sum(v=="met" for v in ev.values())}/{len(ev)}' if ev else '不适用'
        lines.append(f'| {item["repeat"]} | [{item["arm"]}](runs/{item["run_id"]}/transcript.md) | {w.get("f2p_passed","缺失")}/{w.get("f2p_total","?")} | {w.get("p2p_passed","缺失")}/{w.get("p2p_total","?")} | {persona} | {r.get("base_pass")} | {r.get("evaluation_valid")} | {r.get("seconds",0)/60:.2f} | {r.get("actions")} / {r.get("user_turns")} | {r.get("api_calls")} | {r.get("end_reason")} |')
    lines += ['## 按组汇总','| 组别 | 已结束 / 有效 | F2P 平均 ± 样本标准差（项/60） | 范围 | 原题严格通过 | 平均分钟 |',
              '|---|---:|---:|---:|---:|---:|']
    means={}
    for arm in ['baseline','control','pivot']:
        allrows=[x['result'] for x in records if x['arm']==arm]
        valid=[r for r in allrows if r.get('evaluation_valid') and r.get('reward')]
        if not valid:continue
        scores=[r['reward']['f2p_passed'] for r in valid];means[arm]=statistics.mean(scores)
        std=f'{statistics.stdev(scores):.2f}' if len(scores)>1 else '暂无'
        lines.append(f'| {arm} | {len(allrows)} / {len(valid)} | {means[arm]:.2f} ± {std} | {min(scores)}–{max(scores)} | {sum(r["base_pass"] for r in valid)}/{len(valid)} | {statistics.mean(r["seconds"] for r in valid)/60:.2f} |')
    if len(records)<9:lines.append('**实验尚未完成，暂不判断是否下降。**')
    elif all(r['result'].get('evaluation_valid') for r in records):
        delta=means['pivot']-means['control']; secondary=means['pivot']-means['baseline']
        lines.append(f'主要比较：pivot − control 的平均 F2P 差值为 **{delta:+.2f}/60 项（{delta/60*100:+.2f} 个百分点）**。辅助比较：pivot − baseline 为 **{secondary:+.2f}/60 项**。这只是本任务各 3 次的观测差异，不等于统计上确定的因果结论，也不能推广到其他任务或 persona。')
    else:lines.append('有无效运行；上表保留全部结果，均值仅描述有效运行，不能把缺失或协议失败当成普通代码失败后直接比较。')
    lines += ['## 解释边界',
              '- 主要比较 control 与 pivot：同样先计划、再获得完整原题。Pivot 额外披露 scope、explanation、handoff，并接受证据检查和修订反馈。',
              '- 这测试的是完整 persona pivot 流程的影响，包括额外公开例子、交互、检查和反馈，不是单独测 persona 文风；三组总 API 上限相同，pivot 的 simulator/judge 也占其中额度。',
              '- Baseline 一开始拿到完整原题，因而 baseline 与 control 还混合了披露时机的变化。',
              '- 各重复轮采用不同组别顺序以减轻时间顺序影响；没有相同模型随机种子的配对保证，不进行挑选最佳结果或中途改规则。',
              '- 原题隐藏测试仅在作答结束后运行；公开检查通过、persona 3/3、整体严格成功是三个不同概念。',
              f'- [冻结计划](runs/{folder.name}/plan.json) · [实验说明](comparison/EXPERIMENT_PLAN.md) · [历史对比与旧 v2 报告](REPORT.md)']
    (ROOT/'MATCHED_REPORT.md').write_text('\n\n'.join(lines).replace('|\n\n|','|\n|')+'\n')
    dump(folder/'summary.json',{'completed':len(records),'planned':9,'records':records,'valid_f2p_means':means})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prefix',required=True);args=parser.parse_args()
    if not args.prefix.replace('-','').replace('_','').isalnum():raise SystemExit('Invalid prefix')
    verify_cache()
    gate=json.loads((ROOT/'runs/full-persona-v2-gates/validation.json').read_text())
    if not gate.get('passed') or gate['harness_manifest']!=compare_run.original_manifest():
        raise SystemExit('Frozen v2 scoring gate does not match source')
    folder=ROOT/'runs'/args.prefix;folder.mkdir(exist_ok=False)
    cfg=config()
    episodes=[{'repeat':i,'arm':arm,'run_id':f'{args.prefix}-r{i}-{arm}'} for i,arms in enumerate(ORDER,1) for arm in arms]
    fingerprint=compare_run.manifest()
    plan={'created_utc':datetime.now(timezone.utc).isoformat(),'episodes':episodes,'harness_manifest':fingerprint,
          'suite_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'model_roles':{role:cfg.get(role) or cfg['AGENT_MODEL'] for role in ['AGENT_MODEL','SIMULATOR_MODEL','JUDGE_MODEL']},
          'primary_metric':'mean F2P passed count; pivot minus control','secondary':'strict base pass, pivot minus baseline, persona, validity, runtime, actions, API calls',
          'policy':'All nine predetermined results retained. Stop suite on infrastructure/protocol-invalid run. No score-driven reruns.'}
    dump(folder/'plan.json',plan);summarize(folder)
    for item in episodes:
        if compare_run.manifest()!=fingerprint:raise SystemExit('Frozen runtime changed during suite')
        print(json.dumps({'starting':item}),flush=True)
        try:result=compare_run.episode(item['run_id'],item['arm'])
        finally:summarize(folder)
        if not result.get('evaluation_valid'):raise SystemExit('Invalid episode retained; pause suite for inspection')
    print(json.dumps({'suite_complete':args.prefix,'episodes':9}),flush=True)

if __name__=='__main__':main()
