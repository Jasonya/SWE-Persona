"""Cross-platform probe caching must preserve the actual kernel-policy gate."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('fixture_network_probe_boot_id',
    ROOT/'src/swe_persona/vendor/swe_pro/mini/runtime/pcb_v6/network_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NetworkProbeBootTests(unittest.TestCase):
    def environment(self):
        env = Mock()
        env._EGRESS_CONTROL_KERNEL_PROBE_IMAGE = 'kernel-probe'
        env._EGRESS_CONTROL_KERNEL_PROBE_SCRIPT = 'test-kernel-policy'
        env._engine_cmd.side_effect = lambda *args:['docker',*args]
        return env

    def result(self, code=0):
        return subprocess.CompletedProcess([],code,'probe output','probe error' if code else '')

    def test_linux_uses_the_existing_boot_id_without_sysctl(self):
        with patch.object(probe.Path,'read_text',return_value='linux-boot-id\n'), \
                patch.object(probe.subprocess,'run') as run:
            self.assertEqual(probe.host_boot_id(),'linux-boot-id')
            run.assert_not_called()

    def test_macos_boot_time_fallback_is_bounded(self):
        stamp = '{ sec = 1791810000, usec = 0 } Mon Oct 12 00:00:00 2026'
        with patch.object(probe.Path,'read_text',side_effect=FileNotFoundError), \
                patch.object(probe.platform,'system',return_value='Darwin'), \
                patch.object(probe.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stamp+'\n','')) as run:
            self.assertEqual(probe.host_boot_id(),'darwin:'+stamp)
            run.assert_called_once_with(['sysctl','-n','kern.boottime'],
                                        capture_output=True,text=True,timeout=5)

    def test_unavailable_boot_metadata_never_raises_or_invents_a_token(self):
        for result,error in [(self.result(1),None),
                             (subprocess.CompletedProcess([],0,'  \n',''),None),
                             (None,FileNotFoundError()),
                             (None,subprocess.TimeoutExpired('sysctl',5))]:
            with self.subTest(error=error,result=result), \
                    patch.object(probe.Path,'read_text',side_effect=PermissionError), \
                    patch.object(probe.platform,'system',return_value='Darwin'), \
                    patch.object(probe.subprocess,'run',return_value=result,side_effect=error):
                self.assertIsNone(probe.host_boot_id())
        with patch.object(probe.Path,'read_text',return_value=''), \
                patch.object(probe.platform,'system',return_value='Linux'), \
                patch.object(probe.subprocess,'run') as run:
            self.assertIsNone(probe.host_boot_id())
            run.assert_not_called()

    def test_success_is_reused_only_during_the_same_boot_and_engine_context(self):
        env = self.environment()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(probe,'host_boot_id',side_effect=['boot-1','boot-1','boot-2','boot-2']), \
                patch.object(probe.subprocess,'run',return_value=self.result()) as run, \
                patch.dict(probe.os.environ,{'DOCKER_HOST':'first','DOCKER_CONTEXT':'first'}):
            probe.probe_kernel(env,temp);probe.probe_kernel(env,temp)
            self.assertEqual(run.call_count,1)
            probe.probe_kernel(env,temp)
            self.assertEqual(run.call_count,2)
            with patch.dict(probe.os.environ,{'DOCKER_CONTEXT':'second'}):
                probe.probe_kernel(env,temp)
            self.assertEqual(run.call_count,3)

    def test_macos_fallback_allows_startup_and_runs_the_real_probe(self):
        env = self.environment()
        # Only the Linux metadata read fails. Audit/cache files still use the
        # real filesystem, exercising the original startup failure path.
        original_read = probe.Path.read_text
        def read(path,*args,**kwargs):
            if str(path)=='/proc/sys/kernel/random/boot_id':raise FileNotFoundError(path)
            return original_read(path,*args,**kwargs)
        boot_time = subprocess.CompletedProcess([],0,'{ sec = 1791810000, usec = 0 }','')
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(probe.Path,'read_text',read), \
                patch.object(probe.platform,'system',return_value='Darwin'), \
                patch.object(probe.subprocess,'run',side_effect=[boot_time,self.result(),boot_time]) as run:
            self.assertTrue(probe.probe_kernel(env,temp))
            self.assertTrue(probe.probe_kernel(env,temp))
            self.assertEqual(run.call_count,3)
            self.assertEqual(run.call_args_list[1].args[0][0:4],['docker','container','run','--rm'])
            self.assertTrue((Path(temp)/'network_probe_success.json').exists())

    def test_missing_token_disables_both_cache_read_and_cache_write(self):
        env = self.environment()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(probe,'host_boot_id',return_value=None), \
                patch.object(probe.subprocess,'run',return_value=self.result()) as run:
            # Even a cache keyed with null must not bypass the kernel probe.
            command = env._engine_cmd('container','run','--rm','kernel-probe','sh','-c','test-kernel-policy')
            identity = hashlib.sha256(json.dumps([command,None,probe.os.environ.get('DOCKER_HOST'),
                probe.os.environ.get('DOCKER_CONTEXT')]).encode()).hexdigest()
            cache = Path(temp)/'network_probe_success.json'
            saved = json.dumps({'identity':identity,'timestamp':time.time()})
            cache.write_text(saved)
            probe.probe_kernel(env,temp);probe.probe_kernel(env,temp)
            self.assertEqual(run.call_count,2)
            self.assertEqual(cache.read_text(),saved)
            log = [json.loads(line) for line in (Path(temp)/'network_probe.jsonl').read_text().splitlines()]
            self.assertEqual(sum(row.get('status')=='cache_disabled' for row in log),2)

    def test_kernel_failure_still_blocks_startup_with_or_without_token(self):
        for boot in ('darwin:boot-time',None):
            with self.subTest(boot=boot), tempfile.TemporaryDirectory() as temp, \
                    patch.object(probe,'host_boot_id',return_value=boot), \
                    patch.object(probe.subprocess,'run',return_value=self.result(1)) as run:
                with self.assertRaisesRegex(RuntimeError,'allowlist remains required'):
                    probe.probe_kernel(self.environment(),temp,timeout=9,attempts=2)
                self.assertEqual(run.call_count,2)
                self.assertEqual(run.call_args.kwargs['timeout'],9)
                self.assertFalse((Path(temp)/'network_probe_success.json').exists())


if __name__ == '__main__':
    unittest.main()
