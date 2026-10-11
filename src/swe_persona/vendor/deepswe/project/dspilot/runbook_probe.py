"""Execute a delivered, registered reproduction command in a clean task box.

Only public fixture files and the exact registered command are accepted. Artifact
prose never executes on the host. This verifies the documented bounded workload,
not every original task behavior or the surrounding prose's completeness.
"""
import hashlib
import json
import re
import shlex


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def contract(probe):
    return {'artifact':'runbook.md', 'probe_id':probe['id'],
            'command':probe['command'],
            'files':[{'filename':p['filename'],'sha256':sha(p['script'])}
                     for p in [probe]+probe.get('extra_files',[])]}


def key(bound):
    return 'runbook-'+bound['probe_id']


def shell_spacing(text):
    # Normalize only the shell continuation whose semantics are fixed outside
    # single quotes. Do not collapse arbitrary newlines into shell arguments.
    unfolded=[];quote=None;i=0
    while i<len(text):
        char=text[i]
        if char=='\\' and quote!="'" and i+1<len(text):
            if text[i+1]=='\n':i+=2;continue
            unfolded.extend(text[i:i+2]);i+=2;continue
        if char in ('\"',"'"):
            if quote==char:quote=None
            elif quote is None:quote=char
        if char in ' \t' and quote is None:
            if not unfolded or unfolded[-1]!=' ':unfolded.append(' ')
        else:unfolded.append(char)
        i+=1
    return ''.join(unfolded).strip()


def command_from(text, bound):
    # The Markdown representation is not a persona obligation. Accept plain
    # command lines, inline code and any fence label; repetition is harmless.
    command=bound['command'].strip()
    candidates=text.splitlines()+re.findall(r'`([^`\n]+)`',text)
    candidates+=re.findall(r'^```[^\n]*\n(.*?)^```[ \t]*$',text,re.M|re.S)
    logical=''
    for line in text.splitlines():
        logical+=line+'\n'
        if not line.endswith('\\'):
            candidates.append(logical);logical=''
    # Literal single invocations may use different harmless spaces or quoting.
    # Compound commands/expansions retain their exact registered shell meaning.
    words=shlex.split(command)
    literal=all(re.fullmatch(r'[A-Za-z0-9_./:=+-]+',word) for word in words)
    for candidate in candidates:
        candidate=shell_spacing(candidate.strip().removeprefix('$ '))
        if candidate==shell_spacing(command):return command
        if literal and '\n' not in candidate:
            try:actual=shlex.split(candidate)
            except ValueError:continue
            if actual==words:return command
    raise ValueError('The documented entry point does not identify the supplied registered workload command; its reproduction cannot be verified.')


def execute(snapshot, directory, probe, runner=None):
    from .probes_staged import public_probe
    bound=contract(probe);text=snapshot.get('artifacts',{}).get(bound['artifact']) or ''
    result={'version':'delivered-runbook-v1','artifact_sha256':sha(text),
            'patch_sha256':snapshot['patch_sha256'],'contract':bound}
    try:command=command_from(text,bound)
    except ValueError as exc:
        directory.mkdir(parents=True,exist_ok=False)
        result.update(status='not_met',reason=str(exc))
    else:
        # public_probe installs precisely these fixture bytes in a NEW box and
        # executes the command read from the artifact, with its registered bound.
        result['command']=command
        result['execution_record']=(runner or public_probe)(snapshot['patch'],directory,probe|{'command':command})
        result['status']='executed'
    export=directory/'public-fixtures';export.mkdir()
    for item in [probe]+probe.get('extra_files',[]):
        path=export/item['filename'];path.parent.mkdir(parents=True,exist_ok=True);path.write_text(item['script'])
    (export/'manifest.json').write_text(json.dumps(bound,indent=2))
    (directory/'runbook-record.json').write_text(json.dumps(result,indent=2))
    return result


def errors(req, snapshot, trusted):
    bound=req.get('staged_runbook_probe')
    if not bound:return []
    text=snapshot.get('artifacts',{}).get(bound['artifact']) or ''
    try:command=command_from(text,bound)
    except ValueError as exc:return [str(exc)]
    record=(trusted or {}).get(key(bound))
    if not record:return ['Independent execution of the delivered runbook is missing.']
    if (record.get('version')!='delivered-runbook-v1' or record.get('contract')!=bound or
            record.get('artifact_sha256')!=sha(text) or record.get('patch_sha256')!=snapshot.get('patch_sha256')):
        return ['Runbook execution is stale or does not match the delivered artifact, patch and public inputs.']
    run=record.get('execution_record') or {}
    if record.get('status')!='executed' or record.get('command')!=command:
        return ['The delivered runbook command was not executed.']
    if (run.get('patch_sha256')!=snapshot.get('patch_sha256') or
            any(run.get(k)!=v for k,v in req['staged_baseline_probe'].items())):
        return ['Runbook execution has a different candidate or public case identity.']
    measured=req.get('staged_resource_probe')
    if measured:
        from .resource_probe import verdict
        status, reasons=verdict(run,measured['protocol'],measured['probe_sha256'],measure_budget=measured['measure_budget'])
        if status!='met':return ['Clean runbook reproduction failed: '+'; '.join(reasons)]
    elif not run.get('passed') or run.get('execution',{}).get('code')!=0:
        return ['Clean runbook reproduction did not complete successfully.']
    return []
