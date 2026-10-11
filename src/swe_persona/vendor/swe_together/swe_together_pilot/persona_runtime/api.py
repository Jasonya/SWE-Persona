"""Two public API configurations; grading shares the simulator's API settings."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from jsonschema import Draft202012Validator, ValidationError
from openai import OpenAI

ROLES = ('agent', 'user_simulator', 'judge')
PUBLIC_ROLES = ('agent', 'user_simulator')

def load_config(path, require_keys=False):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict): raise ValueError('API configuration must be a JSON object')
    if 'judge' in config:
        raise ValueError('Remove the judge section: grading uses the user_simulator model, endpoint, options and API key')
    if set(config) - set(PUBLIC_ROLES): raise ValueError('Only agent and user_simulator sections are allowed')
    for role in PUBLIC_ROLES:
        if role not in config: raise ValueError('Missing API role: ' + role)
        c=config[role]
        if not isinstance(c, dict): raise ValueError(role + ' must be a JSON object')
        for key in ['model','base_url','api_key_env','api']:
            if not isinstance(c.get(key),str) or not c[key]: raise ValueError(f'{role}.{key} is required')
        if c['api'] not in {'chat_completions','responses'}: raise ValueError('Unsupported API transport')
        if not c['base_url'].startswith(('https://','http://localhost','http://127.0.0.1')):
            raise ValueError('Use HTTPS or a local API endpoint')
        chat_token_options(c)
        if 'api_key' in c: raise ValueError('Use api_key_env; do not save API keys in configuration')
        if require_keys and not os.environ.get(c['api_key_env']): raise ValueError('Unset key variable: '+c['api_key_env'])
    # Source profiles retain separate judge prompts and audit records. Copy the
    # connection settings so per-role state cannot mutate simulator settings.
    config['judge'] = deepcopy(config['user_simulator'])
    return config

def chat_token_options(config, max_tokens=None):
    """Select one provider-supported Chat token field, without silent duplicates."""
    options=dict(config.get('request_options',{}))
    legacy=options.pop('max_tokens_field',None)
    field=config.get('max_tokens_field',legacy or 'max_completion_tokens')
    if field not in {'max_tokens','max_completion_tokens'}:
        raise ValueError('max_tokens_field must be max_tokens or max_completion_tokens')
    if legacy is not None and legacy!=field:
        raise ValueError('Conflicting max_tokens_field settings')
    limits=[options.pop(k) for k in ('max_tokens','max_completion_tokens') if k in options]
    if len(limits)>1:
        raise ValueError('Specify only one Chat token limit in request_options')
    limit=limits[0] if limits else max_tokens
    if limit is not None:
        if isinstance(limit,bool) or not isinstance(limit,int) or limit<=0:
            raise ValueError('Chat token limit must be a positive integer')
        options[field]=limit
    return options

def fingerprint(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def role_name(role):
    if role in {'agent','coding_agent'} or role.startswith('agent.'): return 'agent'
    if role in {'simulator','user_simulator'} or role.startswith('simulator'): return 'user_simulator'
    return 'judge'

class RoleAPI:
    def __init__(self, config, role, log_dir=None):
        self.role=role;self.config=config[role];self.log_dir=Path(log_dir) if log_dir else None
        c=self.config
        key=os.environ.get(c['api_key_env'])
        if not key: raise ValueError('Unset key variable: '+c['api_key_env'])
        self.client=OpenAI(base_url=c['base_url'],api_key=key,max_retries=0,
                           timeout=float(c.get('timeout_sec',120)))

    def raw(self,messages,schema=None,name='payload',max_tokens=4096,timeout=None):
        c=self.config; messages=list(messages)
        converted=[]
        for message in messages:
            message=dict(message)
            if isinstance(message.get('content'),list):
                blocks=[]
                for block in message['content']:
                    block=dict(block)
                    if c['api']=='chat_completions' and block.get('type')=='input_text':block['type']='text'
                    elif c['api']=='chat_completions' and block.get('type')=='input_image':
                        block={'type':'image_url','image_url':{'url':block['image_url']}}
                    elif c['api']=='responses' and block.get('type')=='image_url':
                        block={'type':'input_image','image_url':block['image_url']['url']}
                    elif c['api']=='responses' and block.get('type')=='text':block['type']='input_text'
                    blocks.append(block)
                message['content']=blocks
            converted.append(message)
        messages=converted
        mode=c.get('structured_output_mode','json_schema' if c.get('strict_json_schema',True) else 'json_object')
        if mode not in {'json_schema','json_object','prompt'}: raise ValueError('Invalid structured_output_mode')
        strict=mode=='json_schema'
        if schema is not None and not strict:
            instruction='\nOutput transport: return ONLY one JSON object satisfying this schema, with no prose outside it. Natural-language role content belongs INSIDE the appropriate JSON string fields. Schema: '+json.dumps(schema)
            indices=[i for i,m in enumerate(messages) if m.get('role')=='system' and isinstance(m.get('content'),str)]
            if indices:
                index=indices[-1];messages[index]=dict(messages[index],content=messages[index]['content']+instruction)
            else:messages=[{'role':'system','content':instruction}]+messages
        client=self.client.with_options(timeout=float(timeout or c.get('timeout_sec',120)))
        extra=dict(c.get('request_options',{}))
        if set(extra)&{'model','messages','input','api_key','base_url','text','response_format'}:
            raise ValueError('request_options cannot override routing or output schemas')
        started=time.monotonic()
        if c['api']=='responses':
            kwargs={'model':c['model'],'input':messages,'max_output_tokens':max_tokens,'store':False,**extra}
            if schema is not None and strict: kwargs['text']={'format':{'type':'json_schema','name':name,'strict':True,'schema':schema}}
            response=client.responses.create(**kwargs)
            text=response.output_text or ''; usage=response.usage
            input_tokens=getattr(usage,'input_tokens',0);output_tokens=getattr(usage,'output_tokens',0)
        else:
            kwargs={'model':c['model'],'messages':messages,**chat_token_options(c,max_tokens)}
            if schema is not None and mode!='prompt':
                kwargs['response_format']={'type':'json_schema','json_schema':{'name':name,'strict':True,'schema':schema}} if strict else {'type':'json_object'}
            response=client.chat.completions.create(**kwargs)
            text=response.choices[0].message.content or '';usage=response.usage
            input_tokens=getattr(usage,'prompt_tokens',0);output_tokens=getattr(usage,'completion_tokens',0)
        if self.log_dir:
            self.log_dir.mkdir(parents=True,exist_ok=True)
            record={'role':self.role,'model':c['model'],'resolved_model':response.model,'api':c['api'],
                    'elapsed_sec':time.monotonic()-started,'input_tokens':input_tokens,'output_tokens':output_tokens,
                    'messages':messages,'output':text}
            with (self.log_dir/'api-calls.jsonl').open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
        return {'text':text,'input_tokens':input_tokens,'output_tokens':output_tokens}

    def json(self,messages,schema,name='payload',max_tokens=4096,timeout=None):
        # Format repair never changes the schema or supplies grading hints. All
        # attempts share the caller's timeout and are included in the API audit.
        deadline=time.monotonic()+float(timeout or self.config.get('timeout_sec',120))
        repair_messages=list(messages)
        validator=Draft202012Validator(schema)
        for attempt in range(3):
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError('Structured response deadline exhausted')
            text=self.raw(repair_messages,schema,name,max_tokens,remaining)['text'].strip()
            if text.startswith('```') and text.endswith('```') and '\n' in text:
                text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
            try:
                value=json.loads(text)
                validator.validate(value)
                return value
            except (json.JSONDecodeError,ValidationError) as error:
                detail=error.msg if isinstance(error,json.JSONDecodeError) else str(list(error.absolute_path))+': '+error.message
                if self.log_dir:
                    with (self.log_dir/'format-rejections.jsonl').open('a') as f:
                        f.write(json.dumps({'role':self.role,'schema':name,'attempt':attempt+1,'error':detail})+'\n')
                if attempt==2: raise
                repair_messages=list(messages)+[{'role':'assistant','content':text},
                    {'role':'user','content':'Your response was rejected as invalid JSON or a schema mismatch. '
                     'Return a corrected JSON object satisfying the same schema, with no additional keys. '
                     'Do not change the task or invent evidence. Validation error: '+detail[:2000]}]
