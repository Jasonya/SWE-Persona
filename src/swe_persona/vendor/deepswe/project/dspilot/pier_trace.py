"""Lossless host event sidecar plus a non-fabricating ATIF coding-agent view."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

def export(events, session_id, model):
    steps=[];pending={};errors=[];seen=set()
    for event in events:
        kind=event['kind'];value=event['value']
        if kind=='user':
            steps.append({'step_id':len(steps)+1,'source':'user','message':value,'timestamp':event['at']})
        elif kind=='response' and event.get('role')=='agent':
            text=[];reasoning=[];calls=[];malformed=[]
            for item in value.get('output',[]):
                if item.get('type')=='message':
                    text.extend(c['text'] for c in item.get('content',[]) if c.get('type')=='output_text')
                elif item.get('type')=='reasoning':
                    reasoning.extend(c['text'] for c in item.get('summary',[]) if c.get('text'))
                elif item.get('type')=='function_call':
                    identity=item.get('call_id')
                    if not identity or not item.get('name'):
                        malformed.append(copy.deepcopy(item))
                        continue
                    args=item.get('arguments','')
                    try:args=json.loads(args)
                    except (ValueError,TypeError):args={'raw_arguments':args}
                    if not isinstance(args,dict):args={'raw_arguments':args}
                    calls.append({'tool_call_id':identity,
                                  'function_name':item['name'],'arguments':args})
            step={'step_id':len(steps)+1,'source':'agent','message':'\n'.join(text),'llm_call_count':1,
                  'timestamp':event['at'],'extra':{'provider_response_id':value.get('id'),
                    'response_status':value.get('status'),'raw_event':event['event_id'],
                    'recorded_at_not_provider_start':True}}
            if reasoning:step['reasoning_content']='\n'.join(reasoning)
            if malformed:step['extra']['malformed_tool_items']=malformed
            if calls:
                step['tool_calls']=calls
                for call in calls:
                    if call['tool_call_id'] in seen:raise ValueError('Duplicate tool identity in trajectory')
                    seen.add(call['tool_call_id'])
                    pending[call['tool_call_id']]=step
            usage=value.get('usage')
            if isinstance(usage,dict):
                metrics={}
                for source,target in [('input_tokens','prompt_tokens'),('output_tokens','completion_tokens')]:
                    if usage.get(source) is not None:metrics[target]=usage[source]
                if metrics:step['metrics']=metrics
            steps.append(step)
        elif kind=='observation':
            identity=value['call_id']
            if identity not in pending:raise ValueError('Observation lacks actual tool call')
            step=pending.pop(identity)
            # This is exactly the observation delivered back to the coding agent.
            step.setdefault('observation',{'results':[]})['results'].append({'source_call_id':identity,'content':value['output']})
        elif kind=='transport_error':errors.append({'event_id':event['event_id'],'role':event.get('role')})
    if not steps:raise ValueError('No delivered dialogue or model response; cannot fabricate ATIF step')
    return {'schema_version':'ATIF-v1.7','session_id':session_id,
            'agent':{'name':'dspilot-native-act','version':'native-act-v1','model_name':model},
            'steps':steps,'extra':{'canonical_evidence':'benchmark turns/actions/API and sidecar events',
                'unobserved_calls':list(pending),'transport_errors':errors,
                'scope':'Coding agent view only; simulator/judge events retained separately; missing cost stays unknown'}}

class Trace:
    def __init__(self, path):self.path=Path(path);self.events=[]
    def add(self, kind, value, role=None):
        row={'event_id':len(self.events)+1,'at':datetime.now(timezone.utc).isoformat(),
             'kind':kind,'role':role,'value':copy.deepcopy(value)}
        self.events.append(row)
        with self.path.open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')

class RecordingClient:
    def __init__(self, client, trace):self.inner=client;self.trace=trace
    def __getattr__(self,name):return getattr(self.inner,name)
    def create(self,messages,**kwargs):
        role=kwargs.get('role','agent')
        self.trace.add('request',{'messages':messages,'options':kwargs},role)
        try:r=self.inner.create(messages,**kwargs)
        except Exception as exc:
            self.trace.add('transport_error',{'type':type(exc).__name__},role);raise
        self.trace.add('response',r,role);return r

class RecordingBackend:
    def __init__(self,backend,trace):self.inner=backend;self.trace=trace
    def __getattr__(self,name):return getattr(self.inner,name)
    def deliver_user(self,message):
        self.inner.deliver_user(message);self.trace.add('user',message)
    def observe(self,identity,value):
        self.inner.observe(identity,value)
        self.trace.add('observation',{'call_id':identity,'output':self.inner.history[-1]['output']})
