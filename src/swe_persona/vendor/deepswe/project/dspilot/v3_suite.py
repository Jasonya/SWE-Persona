"""Three fixed new pivot episodes; retain six unchanged earlier controls."""
import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from .api import config
from .protocol import dump
from .prepare import verify_cache
from .run_v3 import episode, manifest
from .task import ROOT


def summarize(folder):
    plan=json.loads((folder/'plan.json').read_text())
    rows=[]
    for item in plan['controls']+plan['new_pivots']:
        p=ROOT/'runs'/item['run_id']/'result.json'
        if p.exists():rows.append(item|{'result':json.loads(p.read_text())})
    done=sum(x['arm']=='pivot' for x in rows)
    lines=['# 修复后对照结果：三次新 pivot 与已有对照',
           f'协议版本：`{plan.get("protocol_version","matched-total-budget-judge-v3")}`。新 pivot 已完成 {done}/3 次。Baseline/control 复用上一批各三次，没有把它们说成 v3 新跑的同期对照。旧 pivot 不混入修复后均值，全部原始记录仍保留。',
           '## 每次结果','| 来源 | 组别 | 次数 | F2P | P2P | Persona | 评测有效 | 分钟 | 停止原因 |',
           '|---|---|---:|---:|---:|---|---|---:|---|']
    for item in rows:
        r=item['result'];w=r.get('reward') or {};ev=r.get('evolved') or {}
        persona=('评测器错误，不计分' if not r.get('evaluation_valid') else (f'{sum(v=="met" for v in ev.values())}/{len(ev)}' if ev else '不适用'))
        lines.append(f'| {item["source"]} | [{item["arm"]}](runs/{item["run_id"]}/transcript.md) | {item["repeat"]} | {w.get("f2p_passed","缺失")}/{w.get("f2p_total","?")} | {w.get("p2p_passed","缺失")}/{w.get("p2p_total","?")} | {persona} | {r.get("evaluation_valid")} | {r.get("seconds",0)/60:.2f} | {r.get("end_reason")} |')
    lines+=['## 汇总','| 组别 | 有效次数 | F2P 各次 | 平均 ± 样本标准差（项/60） | 原题严格成功 | 平均分钟 |','|---|---:|---|---:|---:|---:|']
    means={}
    for arm in ['baseline','control','pivot']:
        group=[x['result'] for x in rows if x['arm']==arm and x['result'].get('evaluation_valid') and x['result'].get('reward')]
        if not group:continue
        scores=[r['reward']['f2p_passed'] for r in group];means[arm]=statistics.mean(scores)
        sd=f'{statistics.stdev(scores):.2f}' if len(scores)>1 else '暂无'
        lines.append(f'| {arm} | {len(group)} | {scores} | {means[arm]:.2f} ± {sd} | {sum(r["base_pass"] for r in group)}/{len(group)} | {statistics.mean(r["seconds"] for r in group)/60:.2f} |')
    if done==3 and all(x['result'].get('evaluation_valid') for x in rows):
        delta=means['pivot']-means['control']
        lines.append(f'新 pivot − 既有 control：**{delta:+.2f}/60 项（{delta/60*100:+.2f} 个百分点）**。每组仅三次、仅一个任务，且对照不是同期重新运行，这只是探索性观察，不能确定普遍的因果效果。')
    else:lines.append('新批次尚未完整有效结束，暂不下结论。')
    lines+=['## 为什么这样重跑',
            '- 引用故障影响的是 persona judge 及后续反馈。Baseline/control 不调用该 judge，原始代码与隐藏验收也未改；复用六次对照能节省资源。',
            '- 为避免只重跑有 unknown 的样本而产生选择偏差，预先固定新跑三次 pivot，所有结果保留，不挑最高分。',
            '- 新 judge 从 schema 枚举选择固定证据编号，由主机还原原文，保留一次有界修复和明确的旧版本验证解释。它改变了交互反馈，必须新跑 coding agent 才能观察真实耗时和行为；保存证据回放只验证评分器。',
            '- 本次不将旧 pivot 的低分替换或删除。新旧差异仍可能包含模型随机波动及不同运行时段的影响。',
            '- F2P/P2P 全过才算原题成功；persona 全过是独立指标。',
            f'- [运行前冻结计划](runs/{folder.name}/plan.json) · [旧九次结果与修复回放](MATCHED_REPORT.md) · [更早历史结果](REPORT.md)']
    (ROOT/'V3_REPORT.md').write_text('\n\n'.join(lines).replace('|\n\n|','|\n|')+'\n')
    dump(folder/'summary.json',{'new_pivots_completed':done,'new_pivots_planned':3,'rows':rows,'valid_f2p_means':means})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prefix',required=True);args=parser.parse_args()
    if not args.prefix.replace('-','').replace('_','').isalnum():raise SystemExit('Invalid prefix')
    verify_cache();gate=json.loads((ROOT/'runs/judge-v3-gates.json').read_text())
    fingerprint=manifest()
    if not gate.get('passed') or gate['harness_manifest']!=fingerprint:raise SystemExit('v3 checker gate missing or stale')
    old=json.loads((ROOT/'runs/matched-02/plan.json').read_text());cfg=config()
    roles={role:cfg.get(role) or cfg['AGENT_MODEL'] for role in ['AGENT_MODEL','SIMULATOR_MODEL','JUDGE_MODEL']}
    if roles!=old['model_roles']:raise SystemExit('Model roles changed; cannot reuse these controls')
    controls=[item|{'source':'已有对照'} for item in old['episodes'] if item['arm']!='pivot']
    hashes={}
    for item in controls:
        p=ROOT/'runs'/item['run_id']/'result.json'
        if not json.loads(p.read_text()).get('evaluation_valid'):raise SystemExit('Invalid old control')
        hashes[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    folder=ROOT/'runs'/args.prefix;folder.mkdir(exist_ok=False)
    plan={'protocol_version':'matched-total-budget-judge-v3.1','created_utc':datetime.now(timezone.utc).isoformat(),'controls':controls,'control_result_hashes':hashes,
          'new_pivots':[{'arm':'pivot','repeat':i,'run_id':f'{args.prefix}-r{i}-pivot','source':'v3.1 新运行'} for i in range(1,4)],
          'harness_manifest':fingerprint,'model_roles':roles,'suite_sha256':hashlib.sha256((ROOT/'dspilot/v3_suite.py').read_bytes()).hexdigest(),
          'policy':'Three predetermined new pivot episodes; no selective reruns; unchanged earlier controls, not contemporaneous reruns; stop on invalid evaluation.'}
    dump(folder/'plan.json',plan);summarize(folder)
    for item in plan['new_pivots']:
        if manifest()!=fingerprint:raise SystemExit('Runtime changed during batch')
        if any(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=sha for path,sha in hashes.items()):raise SystemExit('Old control changed')
        print(json.dumps({'starting':item}),flush=True)
        try:result=episode(item['run_id'],'pivot')
        finally:summarize(folder)
        if not result.get('evaluation_valid'):raise SystemExit('Evaluator failure retained; pause for inspection')
    print(json.dumps({'suite_complete':args.prefix,'new_pivots':3}),flush=True)

if __name__=='__main__':main()
