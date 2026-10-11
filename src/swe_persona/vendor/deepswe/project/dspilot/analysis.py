"""Read-only analysis for a completed suite and optional separately versioned replay."""
import argparse
import json
import statistics
from pathlib import Path
from .compare_suite import summarize
from .task import ROOT

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',required=True);args=parser.parse_args()
    if not args.suite.replace('-','').replace('_','').isalnum():raise SystemExit('Invalid suite')
    if args.suite!='matched-02':raise SystemExit('This detailed audit is specific to matched-02; use compare_suite.summarize for other suites.')
    folder=ROOT/'runs'/args.suite
    summarize(folder)
    plan=json.loads((folder/'plan.json').read_text())
    records=[]
    for item in plan['episodes']:
        p=ROOT/'runs'/item['run_id'];f=p/'result.json'
        if f.exists():records.append((item,p,json.loads(f.read_text())))
    lines=['## 模型资源与交互成本',
           '| 组别 | Agent / simulator / judge 调用总数 | 平均动作 | 平均用户轮 | 平均输入 token（含缓存） | 平均缓存 token | 平均输出 token | Persona 全部通过 | 轮数耗尽 |',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    total_seconds=0;total_calls=0
    for arm in ['baseline','control','pivot']:
        group=[(p,r) for item,p,r in records if item['arm']==arm]
        if not group:continue
        n=len(group);calls={role:0 for role in ['agent','simulator','judge']}
        for p,r in group:
            total_seconds+=r['seconds'];total_calls+=r['api_calls']
            for line in (p/'api.jsonl').read_text().splitlines():
                row=json.loads(line);calls[row['role']]+=1
        mean=lambda key:statistics.mean(r[key] for p,r in group)
        tokens=lambda key:statistics.mean(r['usage'][key] for p,r in group)
        persona=f'{sum(r.get("persona_pass") is True for p,r in group)}/{n}' if arm=='pivot' else '不适用'
        exhausted=sum(r.get('end_reason')=='user_turn_budget_exhausted' for p,r in group)
        lines.append(f'| {arm} | {calls["agent"]} / {calls["simulator"]} / {calls["judge"]} | {mean("actions"):.2f} | {mean("user_turns"):.2f} | {tokens("input_tokens"):,.0f} | {tokens("cached_tokens"):,.0f} | {tokens("output_tokens"):,.0f} | {persona} | {exhausted}/{n} |')
    lines += [f'九次作答的累计墙钟时间为 {total_seconds/60:.2f} 分钟，共 {total_calls} 次 API 尝试。缓存 token 是输入子集，不能重复相加。API 单价未知，因此不估算美元成本。下方 judge 修复回放单独计费、单独记录，不计作 coding episode。',
              '## Unknown 与预算耗尽的具体含义',
              '本批第一轮 pivot 的 explanation 最终三个原始判票均为 met，其中两票因引用不精确降为 unknown；handoff 还出现把旧版本验证误解为失败验证的判票。原评分严格保留，不能将 unknown 自动改为 met，也不能将这些问题全部归因于 agent 能力。',
              '第一轮 pivot 用完的是 8 个用户轮，而非 API 余额：33/80 动作、85/150 次调用。控制器停止继续对话后仍执行原题隐藏验收，得到 F2P 40/60。第 3–8 轮代码补丁相同，后续主要是交付材料修订。',
              '第二轮 pivot 59/60，首轮交付检查就全部满足；第三轮 pivot 46/60，先修正 scope 清单的真实不一致，最终三项全过。因此正常交付、评测器不确定、原题功能失败应分别报告。',
              '## 原题失败例子与日志',
              '| 运行 | 第一个失败项示例 | 完整原题输出 |','|---|---|---|']
    for item,p,r in records:
        failures=[line for line in (p/'official/output.txt').read_text().splitlines() if line.startswith('FAILED ')]
        example=failures[0].split(' - ')[0].removeprefix('FAILED ') if failures else '无失败项或见原始输出'
        lines.append(f'| {item["run_id"]} | `{example}` | [日志](runs/{item["run_id"]}/official/output.txt) |')
    replay=ROOT/'runs'/f'{args.suite}-r1-pivot-judge-v3/result.json'
    lines += ['## 检查器 v3 修复与独立回放',
              '修复使用证据行号，由主机还原精确原文；无效引用最多在 judge 内部修正一次，仍失败则作为评测器错误停止，不占用下一轮 agent 修订机会。明确区分命令退出状态和代码版本匹配，保存原始判票与引用诊断。保留真实缺失/虚假材料的拒绝检查。',
              '新入口 `dspilot.run_v3` 是单独版本，当前九次全部仍是冻结的旧评分器。不能用保存证据回放去声称“修复后重新跑过三组”。']
    if replay.exists():
        value=json.loads(replay.read_text())
        lines.append(f'回放验证状态：passed={value.get("passed")}；judge 调用 {value.get("calls")} 次；coding agent 重新运行={value.get("coding_agent_rerun")}。')
        if value.get('replays'):
            lines += ['| 保存快照 | 原判定 | v3 回放判定 |','|---|---|---|']
            for turn,row in value['replays'].items():lines.append(f'| 第一轮 pivot，第 {turn} 轮 | {row["original"]} | {row["revised"]} |')
        lines.append(f'[完整回放与正反例结果](runs/{args.suite}-r1-pivot-judge-v3/result.json)。原始 F2P、代码补丁和原判定保持原样；回放不能证明修复后的完整交互会产生相同代码或耗时。')
    else:lines.append('真实回放正在进行，尚未宣称修复版通过验证。')
    lines += ['后续 v3.0 实跑有一次评测器越界引用失败，见 [V3_0_REPORT.md](V3_0_REPORT.md)。固定证据编号的 v3.1 回归验证见 [结果](runs/judge-v31-regression/result.json)，最新同版本三次实跑见 [V3_REPORT.md](V3_REPORT.md)。']
    path=ROOT/'MATCHED_REPORT.md'
    path.write_text(path.read_text()+'\n\n'+'\n\n'.join(lines).replace('|\n\n|','|\n|')+'\n')
    print(json.dumps({'report':'MATCHED_REPORT.md','completed':len(records),'calls':total_calls,'minutes':round(total_seconds/60,2)}))

if __name__=='__main__':main()
