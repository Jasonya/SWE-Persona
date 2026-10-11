"""Prove native tool continuation against a real disposable task container."""
import json
import uuid
from .api import Client
from .task import ROOT, BASE
from .sandbox import Box
from .protocol import ACTION_TOOL, tool_action, dump

def main():
    out=ROOT/'runs'/('preflight-'+uuid.uuid4().hex[:8]);out.mkdir(parents=True)
    client=Client(out/'api.jsonl',max_calls=4)
    history=[{'role':'system','content':'Use act. First inspect /app using pwd and git rev-parse HEAD. Wait for its actual result, then use reply to report the exact observed commit. Do not simulate tool outputs.'},
             {'role':'user','content':'Inspect the existing repository and report its commit.'}]
    with Box() as box:
        response=client.create(history,tools=[ACTION_TOOL],tool_choice='required',effort='medium',max_output_tokens=1200)
        action,call_id=tool_action(response)
        if action['action']!='shell':raise RuntimeError('Preflight did not request real inspection')
        result=box.shell(action['content'])
        if result['code']:raise RuntimeError('Preflight inspection command failed')
        history.extend(response['output'])
        history.append({'type':'function_call_output','call_id':call_id,'output':json.dumps(result)})
        response=client.create(history,tools=[ACTION_TOOL],tool_choice='required',effort='medium',max_output_tokens=1200)
        reply,_=tool_action(response)
        passed=reply['action']=='reply' and BASE in reply['content'] and BASE in result['output']
        dump(out/'result.json',{'passed':passed,'inspection':action,'execution':result,'reply':reply,'calls':client.calls,'usage':client.usage})
        print(json.dumps({'passed':passed,'result':str(out/'result.json'),'calls':client.calls}))
        if not passed:raise SystemExit(1)

if __name__=='__main__':main()
