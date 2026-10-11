"""Source-bound local Git delivery evidence, separate from persona scoring.

Reflogs are repository observations, not an adversarially tamper-proof audit.
Missing history is unknown, never inferred from the current branch name alone.
"""
import copy
import hashlib
import re
import subprocess
from pathlib import Path

CLAUSE = 'IMPORTANT: Please work on this in a new branch from main and commit everything when you are done.'
CONTRACT = {'kind':'new_branch_committed_v1','source_text':CLAUSE,
            'source_branch':'main','acceptance_stage':'S4'}


def validate(plan, contract):
    rule=plan.get('task_workflow')
    if rule is not None and (rule!=CONTRACT or CLAUSE not in contract.splitlines()):
        raise ValueError('Task workflow must match an exact original task instruction')


def capture(root, base):
    def git(*args):
        result=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True)
        if result.returncode:raise ValueError('Git workflow observation failed: '+args[0])
        return result.stdout.strip()
    refs={line.split(' ',1)[0]:line.split(' ',1)[1] for line in
          git('for-each-ref','--format=%(refname) %(objectname)','refs/heads').splitlines()}
    creations={}
    for ref in refs:
        path=Path(git('rev-parse','--git-path','logs/'+ref))
        if not path.is_absolute():path=Path(root)/path
        lines=path.read_text().splitlines() if path.is_file() else []
        # The oldest retained record must actually be a creation; a truncated
        # reflog cannot substantiate origin from a later update.
        if lines:
            record=lines[0];parts=record.split('\t',1)
            ids=parts[0].split(' ',2)
            if len(ids)==3 and len(parts)==2 and re.fullmatch(r'0{40,64}',ids[0]):
                creations[ref]={'commit':ids[1],'message':parts[1],
                               'record_sha256':hashlib.sha256(record.encode()).hexdigest()}
    head=git('rev-parse','HEAD')
    ancestry=subprocess.run(['git','-C',str(root),'merge-base','--is-ancestor',base,head],capture_output=True)
    if ancestry.returncode not in (0,1):raise ValueError('Cannot establish Git base ancestry')
    return {'version':1,'refs':refs,'creations':creations,'head':head,
            'branch':git('branch','--show-current'),'base':base,'base_is_ancestor':ancestry.returncode==0}


def check(initial, current, base):
    from .workspace_evidence import preservation_errors
    a=(initial or {}).get('task_workflow');b=(current or {}).get('task_workflow')
    if not isinstance(a,dict) or not isinstance(b,dict):
        return {'status':'unknown','errors':['Missing host Git workflow evidence.']}
    required={'version','refs','creations','head','branch','base','base_is_ancestor'}
    if not required<=a.keys() or not required<=b.keys() or a['base']!=base or b['base']!=base:
        return {'status':'unknown','errors':['Incomplete or mismatched Git workflow evidence.']}
    if a['branch']!='main' or a['refs'].get('refs/heads/main')!=base:
        return {'status':'unknown','errors':['Initial main/base identity is not established.']}
    errors=[];ref='refs/heads/'+b['branch']
    if not b['branch'] or b['branch']=='main' or ref in a['refs']:
        errors.append('Deliver on a new local branch, not main or an initially existing branch.')
    origin=b['creations'].get(ref)
    if not origin:
        return {'status':'not_met' if errors else 'unknown',
                'errors':errors+['The branch creation record is unavailable; its origin cannot be inferred.']}
    if (origin['commit']!=base or not origin['message'].startswith('branch: Created from ') or
            origin['record_sha256'] in {v['record_sha256'] for v in a['creations'].values()}):
        errors.append('The delivered branch was not observed as newly created at the initial main/base commit.')
    if b['head']!=current.get('head') or b['branch']!=current.get('branch'):
        return {'status':'unknown','errors':['Git workflow and candidate snapshot identities disagree.']}
    if b['head']==base or not b['base_is_ancestor']:
        errors.append('The delivered head must contain a local task commit descending from the initial main/base.')
    if not initial.get('workspace_identity') or not current.get('workspace_identity'):
        return {'status':'unknown','errors':['Missing workspace identity for committed-delivery verification.']}
    errors.extend(preservation_errors(initial['workspace_identity'],current['workspace_identity'],
                                      committed_paths=current.get('files',[]),final=True))
    return {'status':'not_met' if errors else 'met','errors':errors,
            'evidence_scope':'Local branch creation, base ancestry and committed delivery; no claim about remote operations or functional correctness.'}


def calibrate(initial, current, base):
    """Fixed negative mutations of the actual reference fixture's host evidence."""
    positive=check(initial,current,base);negative=[]
    for name in ('main_branch','wrong_origin','missing_origin','uncommitted_task','no_task_commit'):
        changed=copy.deepcopy(current);state=changed['task_workflow']
        ref='refs/heads/'+state['branch']
        expected='not_met'
        if name=='main_branch':state['branch']=changed['branch']='main'
        elif name=='wrong_origin':state['creations'][ref]['commit']='f'*40
        elif name=='missing_origin':state['creations'].pop(ref);expected='unknown'
        elif name=='no_task_commit':state['head']=changed['head']=base
        else:
            changed['workspace_identity']['files']['uncommitted-task.txt']={}
            changed['workspace_identity']['untracked'].append('uncommitted-task.txt')
        negative.append({'id':name,'expected_status':expected,'verdict':check(initial,changed,base)})
    return {'positive':positive,'negative':negative,
            'passed':positive['status']=='met' and all(n['verdict']['status']==n['expected_status'] for n in negative)}
