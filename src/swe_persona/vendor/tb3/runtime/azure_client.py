"""Audited Azure Responses requests shared by authoring and evaluated roles."""
from pathlib import Path
import hashlib, json, os, sys, time, uuid
from openai import OpenAI

# Host environment only; no machine-specific .env or endpoint in this package.
MODEL = os.environ.get('PCB_MODEL', 'gpt-6-astra')

def load_local_env():
    pass

def call(role, prompt, schema, audit_dir, timeout=600, images=None, messages=None):
    load_local_env()
    key=os.environ.get('AZURE_OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not key:raise RuntimeError('Azure API credential unavailable')
    directory=Path(audit_dir);directory.mkdir(parents=True,exist_ok=True)
    path=directory/(uuid.uuid4().hex+'.json')
    record={'role':role,'requested_model':MODEL,'started':time.time(),'request_timeout_seconds':timeout,
            'input':prompt,'input_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'image_count':len(images or []),'valid':False}
    path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    content=[{'type':'input_text','text':prompt}]
    content.extend({'type':'input_image','image_url':uri} for uri in images or [])
    try:
        endpoint=os.environ.get('AZURE_OPENAI_BASE_URL') or os.environ.get('OPENAI_BASE_URL')
        if not endpoint: raise RuntimeError('Set AZURE_OPENAI_BASE_URL or OPENAI_BASE_URL')
        client=OpenAI(base_url=endpoint,api_key=key,max_retries=0,timeout=max(.05,timeout))
        model_input=messages if messages is not None else [{'role':'user','content':content}]
        response=client.responses.create(model=MODEL,input=model_input,
            text={'format':{'type':'json_schema','name':'benchmark_response','strict':True,'schema':schema}},store=False)
        record.update(output=response.output_text,resolved_model=response.model,response_id=response.id,
                      usage=response.usage.model_dump() if response.usage else None)
        value=json.loads(response.output_text)
        import jsonschema
        jsonschema.validate(value,schema)
        if response.model!=MODEL:raise RuntimeError('Unexpected resolved model')
        record['valid']=True
        return value
    except Exception as exc:
        record.update(error_type=type(exc).__name__,error=str(exc))
        raise
    finally:
        record['finished']=time.time();path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')

def obj(properties):return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
TEXT={'type':'string'}
