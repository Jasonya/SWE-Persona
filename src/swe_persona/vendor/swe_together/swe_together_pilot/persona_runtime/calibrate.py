"""Independent semantic controls; failures never silently approve evaluation."""
import argparse
import asyncio
import json
from pathlib import Path
from .api import load_config,fingerprint
from .binding import PILOT,validate
from .evaluation import judge_requirement,CheckerRegistry
from .public_run import Calls,runtime_hash,write

async def calibrate(config,out):
    out.mkdir(parents=True,exist_ok=True)
    cases=json.loads((Path(__file__).parent/'calibration-cases.json').read_text())
    sem=asyncio.Semaphore(3);results=[];binding_hashes=set()
    async def one(case):
        b=json.loads((PILOT/case['binding_file']).read_text());validate(b);binding_hashes.add(b['binding_sha256'])
        async with sem:
            calls=Calls(config,out/case['id'].replace('/','__'))
            result=await judge_requirement(calls,b['persona']['bound_evolved_requirements'][0],case['evidence'],case['activation_turn'],CheckerRegistry(),b['evaluation_policy'],{'original_instruction':b['original_instruction'],'original_user_context':b['original_user_context'],'initial_pool_persona':b['initial_pool_persona']})
        item={'id':case['id'],'expected':case['expected'],'actual':result['status'],'passed':case['expected']==result['status'],'judgment':result}
        results.append(item);write(out/'progress.json',results);print(json.dumps({k:v for k,v in item.items() if k!='judgment'}),flush=True)
    await asyncio.gather(*(one(c) for c in cases))
    report={'profile':'swe-together','passed':all(r['passed'] for r in results),
        'runtime_sha256':runtime_hash(),'api_config_sha256':fingerprint(config),'binding_sha256s':sorted(binding_hashes),
        'controls':len(results),'passed_controls':sum(r['passed'] for r in results),
        'scope':'Synthetic semantic positive/negative controls for every retained binding; not proof of native task success or broad judge accuracy.',
        'results':sorted(results,key=lambda r:r['id'])}
    write(out/'calibration.json',report);return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    r=asyncio.run(calibrate(load_config(a.config,True),a.output));return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
