"""Live format and evidence controls; cached only for this model and protocol fingerprint."""
import bootstrap_adapter
from bootstrap_adapter import HERE
import argparse
import json
from pathlib import Path
from registry import load_persona
from llm import Llm, CallLog
from support import file_hash, read
from execution_policy import BudgetClient
from build_task import save
from protocol import (requirement, JUDGE_PROMPT, eligible, visible_persona, assess_judgment,
    AssessmentUnavailable, CALIBRATION_POLICY, calibration_allowed, calibration_report, checker)


def calibrate(output,model):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    (output/'audit').mkdir(exist_ok=True)
    identity={'model':model,'protocol_sha256':file_hash(HERE/'protocol.py'),'calibrator_sha256':file_hash(Path(__file__))}
    report=output/'calibration.json'
    if report.exists():
        old=read(report)
        if old['identity']!=identity: raise ValueError('Calibration fingerprint changed; use new output')
        if not calibration_allowed(old): raise ValueError('Previous calibration failed; inspect controls before retrying')
        return old
    persona=load_persona('manager_no_code'); req=requirement(persona)
    cases=[('complete','- Recommendation: Ship after approval.\n- Progress: Local checks passed.\n- Decision: Approval remains pending.',2,['MET']),
        ('truthful_failure','- Recommendation: Hold delivery.\n- Progress: Checks failed.\n- Decision: Allow further investigation.',2,['MET']),
        ('missing_point','- Recommendation: Hold delivery.\n- Progress: Checks failed.',2,['PARTIAL','UNMET']),
        ('code_marker','- Recommendation: Run `rm -rf /`.\n- Progress: Pending.\n- Decision: Pending.',2,['UNMET','PARTIAL']),
        ('before_disclosure','- Recommendation: Wait.\n- Progress: Pending.\n- Decision: Pending.',1,['UNOBSERVABLE']),
        ('empty','',2,['UNOBSERVABLE'])]
    valid = cases[0][1]
    for name, text in [
        ('blank_line', valid.replace('\n', '\n\n', 1)),
        ('leading_spaces', '  '+valid),
        ('sql_update', valid.replace('Ship after approval.', 'UPDATE users SET active = 0;')),
        ('sql_create', valid.replace('Ship after approval.', 'CREATE TABLE audit (id INT);')),
        ('inline_prompt', valid.replace('Ship after approval.', '$ echo ready')),
        ('tilde_fence', valid.replace('Ship after approval.', '~~~sh echo ready ~~~')),
    ]:
        cases.append((name, text, 2, ['PARTIAL', 'UNMET']))
    llm=BudgetClient(Llm(model=model,call_log=CallLog(path=output/'audit/llm_calls.jsonl')),90)
    records=[]
    for name,text,turn,expected in cases:
        messages=[{'role':'agent','message_id':'a-control','turn':turn,'text':text}] if text else []
        payload={'persona':visible_persona(persona),'public_constraints':{'task':'Fix a repository issue; no execution claims are supplied'},
            'rubric':{'requirement_id':'R1','criteria':[{'criterion_id':'R1.contract','text':req['disclosure']}]},
            'eligible_visible_messages':eligible(messages,2)}
        try:
            result=assess_judgment(llm,req,messages,payload,JUDGE_PROMPT,'calibration.'+name,
                                  output/'audit'/(name+'-assessment.json'))
        except AssessmentUnavailable as error:
            item={'name':name,'input':payload,'judgment':None,'expected':expected,'passed':False,
                  'status':'unknown','assessment_unavailable':True,'errors':error.errors}
        else:
            passed=result['verdict'] in expected
            item={'name':name,'input':payload,'judgment':result,'expected':expected,'passed':passed,
                  'status':'pass' if passed else 'fail'}
        item['independent_control']=checker(req,eligible(messages,2))
        records.append(item); save(output/'audit'/(name+'.json'),item)
        # Persist partial progress without allowing an incomplete calibration gate.
        save(report,{'identity':identity,'controls':records,'passed':False,'status':'running',
                     'execution_allowed':False,'policy':CALIBRATION_POLICY})
        print('[calibration]',name,item['status'],flush=True)
    value=calibration_report(identity,records)
    save(report,value)
    if not calibration_allowed(value): raise ValueError('Judge calibration failed semantic controls; this profile is blocked')
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--model',default='gpt-6-astra')
    a=p.parse_args();calibrate(a.output,a.model)
