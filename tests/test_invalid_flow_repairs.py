"""Integration regressions for the seven invalid episode diagnoses."""
import asyncio
import contextlib
from copy import deepcopy
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from jsonschema import Draft202012Validator
from swe_persona.api import DEFAULT_USER_SIMULATOR_CONFIG

ROOT=Path(__file__).parents[1]
VENDOR=ROOT/'src/swe_persona/vendor'
TOGETHER=VENDOR/'swe_together/swe_together_pilot'
package=ModuleType('fixture_invalid_together');package.__path__=[str(TOGETHER/'persona_runtime')]
sys.modules[package.__name__]=package
simulator=importlib.import_module(package.__name__+'.simulator')
public=importlib.import_module(package.__name__+'.public_run')
evaluation=importlib.import_module(package.__name__+'.evaluation')

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module

shell=load('fixture_invalid_docker',VENDOR/'swe_pro/shell/portable/runtime-v3/runner/dockerx.py')
checker=load('fixture_invalid_checker',VENDOR/'swe_pro/shell/portable/runtime-v3/runner/fixtures/checker_driver.py')
live=load('fixture_invalid_live',VENDOR/'swe_live/runtime/project/scripts/swe_live_multilang/protected_patch.py')


def strict_objects(schema):
    if schema.get('type')=='object':
        assert schema.get('additionalProperties') is False
        assert set(schema['required'])==set(schema['properties'])
    for value in schema.values():
        if isinstance(value,dict):strict_objects(value)
        elif isinstance(value,list):
            for item in value:
                if isinstance(item,dict):strict_objects(item)


class SimulatorWireTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        case=json.loads((TOGETHER/'persona_runtime/calibration-cases.json').read_text())[0]
        binding=json.loads((TOGETHER/case['binding_file']).read_text())
        return binding,case

    async def test_actual_calls_responses_wire_for_all_roles_and_repair(self):
        binding,case=self.fixture()
        req=binding['persona']['bound_evolved_requirements'][0]
        decision={'action':'message','kind':'persona','content':req['statement'],
            'requirement_id':req['id'],'base_task_complete':None,'completion_reason':None}
        payload=evaluation._compact_overall_packet(evaluation.packet(req,case['evidence'],case['activation_turn']))
        rubric={'packet_id':payload['packet_id'],'status':'unknown','evidence_refs':[],'reason':'No conclusion.'}
        audit_payload={'items':['persona_fidelity'],'transcript':[]}
        audit={'items':[{'id':'persona_fidelity','score':0,'evidence_refs':[],'reason':'No transcript.'}]}
        clause_payload=evaluation.packet(req,case['evidence'],case['activation_turn'])
        clause={'packet_id':clause_payload['packet_id'],'clauses':[]}
        config={role:deepcopy(DEFAULT_USER_SIMULATOR_CONFIG) for role in ('user_simulator','judge')}
        config['user_simulator']['api_key_env']=config['judge']['api_key_env']='FIXTURE_KEY'
        create=Mock()
        client=SimpleNamespace(responses=SimpleNamespace(create=create),close=Mock())
        client.with_options=lambda **kwargs:client
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'FIXTURE_KEY':'dummy'}),patch('swe_persona.api.OpenAI',return_value=client):
            calls=public.Calls(config,Path(directory))
            for role,body,answer in [('user_simulator',{},decision),('requirement_rubric_judge',payload,rubric),
                                     ('requirement_rubric_judge',clause_payload,clause),('interaction_judge',audit_payload,audit)]:
                create.side_effect=[SimpleNamespace(output_text='{"wrong":true}',model='fixture',status='completed',usage=None),
                                    SimpleNamespace(output_text=json.dumps(answer),model='fixture',status='completed',usage=None)]
                self.assertEqual(await calls(role,'Follow the exact output protocol.',body),answer)
                self.assertEqual(create.call_args.kwargs['text']['format']['type'],'json_schema')
                wire=create.call_args.kwargs['text']['format']['schema']
                strict_objects(wire);Draft202012Validator(wire).validate(answer)
            self.assertEqual(create.call_count,8)

    async def test_due_fallback_recovers_without_permanent_invalid_then_accepts(self):
        binding,case=self.fixture();req=binding['persona']['bound_evolved_requirements'][0]
        failed=True;count=0
        async def call(role,system,payload):
            nonlocal count
            if role=='user_simulator':
                count+=1
                if failed:raise ValueError('malformed simulator output')
                return {'action':'accept','kind':'wait','content':'','requirement_id':None,
                    'base_task_complete':True,'completion_reason':'Current task is complete.'}
            if role=='interaction_judge':raise ValueError('optional audit unavailable')
            return {'packet_id':payload['packet_id'],'status':'met',
                    'evidence_refs':[payload['evidence']['artifacts'][0]['ref']],'reason':'Grounded work.'}
        sim=simulator.UserSimulator(binding,call)
        sim.native_disclosed={r['id']:0 for r in binding.get('native_phase_contract',{}).get('required_followups',[])}
        obs={'turn':req['earliest_turn'],'events':[req['trigger']['event']],'agent_message':'Current progress.',
             'completion_attempt':False,'remaining_agent_turns':10,'evidence':case['evidence']}
        decision=await sim.step(obs)
        self.assertEqual(count,3);self.assertEqual(decision['content'],req['statement'])
        self.assertEqual(sim.disclosed[req['id']],obs['turn']);self.assertFalse(sim.invalid)
        failed=False
        # All requests are explicitly disclosed; use host-graded current evidence.
        sim.disclosed={r['id']:obs['turn'] for r in binding['persona']['bound_evolved_requirements']}
        result=await sim.step({**obs,'turn':obs['turn']+1,'completion_attempt':True})
        self.assertEqual(result['action'],'accept');self.assertFalse(sim.invalid)
        summary=await sim.finish(case['evidence'])
        self.assertTrue(summary['evaluation_valid']);self.assertEqual(summary['persona_status'],'met')
        self.assertTrue(summary['simulator_errors']);self.assertFalse(summary['base_verifier_modified'])

    async def test_unrecoverable_reply_stops_without_agent_correction_loop(self):
        binding,case=self.fixture()
        async def call(*args):raise ValueError('bad simulator reply')
        sim=simulator.UserSimulator(binding,call)
        sim.native_disclosed={r['id']:0 for r in binding.get('native_phase_contract',{}).get('required_followups',[])}
        result=await sim.step({'turn':1,'events':[],'agent_message':'Question.',
            'completion_attempt':False,'remaining_agent_turns':100,'evidence':case['evidence']})
        self.assertEqual(result['action'],'assessment_unavailable')
        self.assertEqual(result['content'],'');self.assertFalse(sim.invalid)


class NativeControlWiringTests(unittest.TestCase):
    def test_all_packaged_controls_match_frozen_rows_without_hf(self):
        from swe_persona.dataset import rows
        from swe_persona.worker import install_native_reference_control
        fixtures=VENDOR/'swe_pro/shell/portable/runtime-v3/runner/fixtures'
        verified=0
        with tempfile.TemporaryDirectory() as directory:
            for row in rows(ROOT/'swe-persona-lite.parquet'):
                if row['interaction']['profile_id']!='swe-bench-pro-shell':continue
                assets=Path(directory)/str(verified);assets.mkdir()
                for a in row['assets']:
                    if a['path'] in ('tests/test.patch','tests/run_script.sh','tests/parser.py'):
                        (assets/Path(a['path']).name).write_bytes(a['inline_bytes'])
                install_native_reference_control(row,assets,fixtures)
                self.assertTrue((assets/'gold.patch').read_text().startswith('diff --git '))
                bad=deepcopy(row);bad['environment']['expected_base_commit']='0'*40
                with self.assertRaisesRegex(ValueError,'base mismatch'):
                    install_native_reference_control(bad,assets,fixtures)
                (assets/'test.patch').write_text('changed official tests')
                with self.assertRaisesRegex(ValueError,'asset mismatch'):
                    install_native_reference_control(row,assets,fixtures)
                verified+=1
        self.assertEqual(verified,17)

    def test_actual_shell_materialization_uses_current_driver_and_private_reference(self):
        # A real isolated worker import prevents profile monkeypatches leaking
        # into the unit test process. No provider or container call occurs.
        code="""
from pathlib import Path
import tempfile
from swe_persona.dataset import load_task,materialize,VENDOR
from swe_persona.worker import install_v7
row=load_task('swe-persona-lite.parquet','swepro/swepro-r0542-0a2d724118-first-time-pr-contributor')
with tempfile.TemporaryDirectory() as temp:
 root=materialize(row,Path(temp)/'input');out=Path(temp)/'output';out.mkdir()
 install_v7(root,row,None,out)
 import task_adapter
 manifest=task_adapter.TaskManifest.from_dataset(instance_id=row['source']['instance_id'])
 fixtures=root/'portable/runtime-v3/runner/fixtures'
 assets=manifest.materialize_checker_assets(Path(temp)/'private',fixtures)
 assert (assets/'checker_driver.py').read_bytes()==(VENDOR/'swe_pro/shell/portable/runtime-v3/runner/fixtures/checker_driver.py').read_bytes()
 assert (assets/'gold.patch').is_file()
 assert 'official_candidate_compilation_failed' in (assets/'checker_driver.py').read_text()
 assert (assets/'test.patch').read_bytes()==next(a['inline_bytes'] for a in row['assets'] if a['path']=='tests/test.patch')
"""
        env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONDONTWRITEBYTECODE='1')
        proc=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)


class ShellAndPatchTests(unittest.TestCase):
    def test_binary_output_decodes_success_and_timeout_without_host_crash(self):
        cmd=[sys.executable,'-c','import os,time;os.write(1,b"ok\\x82");os.write(2,b"err\\xff");time.sleep(.2)']
        rc,out,err=shell._run(cmd,timeout=2)
        self.assertEqual(rc,0);self.assertEqual(out,'ok\ufffd');self.assertEqual(err,'err\ufffd')
        rc,out,err=shell._run(cmd,timeout=.1)
        self.assertEqual(rc,124);self.assertEqual(out,'ok\ufffd');self.assertIn('err\ufffd',err)

    def test_official_patch_inventory_works_inside_nested_git_directory(self):
        def diff(path):
            return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);subprocess.run(['git','init','-q',str(root)],check=True)
            sub=root/'nested/evaluation';sub.mkdir(parents=True)
            previous=Path.cwd()
            try:
                os.chdir(sub)
                official=live.paths(diff('src/test/Test.java'))
                self.assertEqual(official,{'src/test/Test.java'})
                candidate=diff('src/main/Main.java')+diff('src/test/Test.java')
                projected,excluded=live.project(candidate,official)
                self.assertEqual(projected,diff('src/main/Main.java'))
                self.assertEqual(excluded,['src/test/Test.java'])
            finally:os.chdir(previous)

    def test_compiler_signals_exclude_dependencies_and_test_runner_failures(self):
        for message in ['scanner/walk_dir_tree_test.go:21:20: undefined: walkResults',
                        'src/Main.java:12: error: cannot find symbol','error[E0308]: mismatched types']:
            self.assertTrue(checker.compilation_diagnostics(message))
        for message in ['scanner/a.go:2:3: cannot find module X',
                        'scanner/a.go:2:3: undefined: X\nINTERNALERROR worker_collectionfinish',
                        'scanner/a.go:2:3: undefined: X\nKilled','go: no required module provides package x',
                        'bash: go: command not found','no tests ran in 54.94s']:
            self.assertFalse(checker.compilation_diagnostics(message))

    def test_reference_control_runs_real_git_worktree_and_preserves_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'repo';assets=root/'assets';repo.mkdir();assets.mkdir()
            def git(*args):
                return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            source=repo/'answer.py';source.write_text('answer = 0\n')
            git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD').strip()
            source.write_text('answer = 1\n');(assets/'gold.patch').write_text(git('diff'))
            # Official tests live in the control only and report a real assertion.
            (repo/'check.py').write_text('from answer import answer\nassert answer == 1\nprint("TestRequired PASSED")\n')
            git('add','-N','check.py');(assets/'test.patch').write_text(git('diff','--','check.py'))
            git('reset','--','check.py');(repo/'check.py').unlink()
            source.write_text('answer = broken candidate syntax !\n');candidate=source.read_bytes()
            (assets/'run_script.sh').write_text('python3 check.py\n')
            (assets/'parser.py').write_text('import json,pathlib,sys\ntext=pathlib.Path(sys.argv[1]).read_text()\npathlib.Path(sys.argv[3]).write_text(json.dumps({"tests":[{"name":"TestRequired","status":"PASSED"}] if "TestRequired PASSED" in text else []}))\n')
            with patch.object(checker,'HARNESS',assets),patch.object(checker,'APP_DIR',repo):
                control=checker.gold_execution_control(SimpleNamespace(base_commit=base),[],{'TestRequired'})
            self.assertTrue(control['passed'],control)
            self.assertEqual(source.read_bytes(),candidate)
            self.assertFalse((repo/'check.py').exists())
            self.assertEqual(len(git('worktree','list').splitlines()),1)

    def test_zero_tests_compile_failure_requires_passing_reference(self):
        spec={'selected_test_files_to_run':'[]','fail_to_pass':"['TestRequired']",'pass_to_pass':'[]'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'task_spec.json').write_text(json.dumps(spec))
            def fake_run(command,**kwargs):
                if command[0]=='bash':
                    Path(kwargs['stdout'].name).write_text('scanner/walk_dir_tree_test.go:21:20: undefined: walkResults\nFAIL scanner [build failed]')
                else:(root/'parsed.json').write_text('{"tests":[]}')
                return SimpleNamespace(returncode=0,stderr='')
            for reference_passed in (True,False):
                with self.subTest(reference_passed=reference_passed),patch.object(checker,'HARNESS',root), \
                     patch.object(checker,'APP_DIR',root),patch.object(checker,'_apply_hidden_tests',return_value=(True,'applied',{})), \
                     patch.object(checker.subprocess,'run',side_effect=fake_run), \
                     patch.object(checker,'gold_execution_control',return_value={'passed':reference_passed}) as control, \
                     patch.object(checker,'Path',side_effect=lambda value:root/'parsed.json' if str(value)=='/tmp/swepro_output.json' else root/Path(value).name), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    checker.check_official_tests(SimpleNamespace(base_commit='base'))
                    verdict=json.loads(output.getvalue().removeprefix('CHECK '))
                    self.assertEqual(verdict['status'],'fail' if reference_passed else 'unknown')
                    self.assertEqual(verdict['evidence']['failure_category'],'candidate_code' if reference_passed else 'test_environment_unavailable')
                    self.assertEqual(control.call_count,1)


if __name__=='__main__':unittest.main()
