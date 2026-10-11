import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import docker
from recover_sample_cleanup import cleanup_verified, recover_receipt

OWNER='a'*32


class CleanupRecoveryTests(unittest.TestCase):
    def container(self, owner=OWNER):
        return SimpleNamespace(id='container',attrs={'Labels':{'personacode.sample':owner}},remove=Mock())

    def test_sparse_labels_do_not_require_inspection(self):
        class SparseContainer:
            id='sparse'
            attrs={'Labels':{'personacode.sample':OWNER}}
            remove=Mock()
            @property
            def labels(self):
                raise AssertionError('Sparse containers cannot use inspected labels property')
        container=SparseContainer(); client=Mock(); client.containers.list.side_effect=[[container],[]]
        proof=cleanup_verified(client,OWNER)
        self.assertEqual(proof['removed_containers'],['sparse'])
        container.remove.assert_called_once_with(force=True)

    def test_removal_in_progress_is_retried_and_verified(self):
        container=self.container(); container.remove.side_effect=docker.errors.APIError('removal in progress')
        client=Mock(); client.containers.list.side_effect=[[container],[],[]]; sleep=Mock()
        proof=cleanup_verified(client,OWNER,sleep=sleep)
        self.assertTrue(proof['verified_no_owned_containers']); self.assertEqual(proof['cleanup_attempts'],2)
        self.assertEqual(len(proof['transient_errors']),1); sleep.assert_called_once_with(1)

    def test_already_removed_container_is_success(self):
        container=self.container(); container.remove.side_effect=docker.errors.NotFound('gone')
        client=Mock(); client.containers.list.side_effect=[[container],[]]
        self.assertTrue(cleanup_verified(client,OWNER)['verified_no_owned_containers'])

    def test_other_owner_is_never_removed(self):
        container=self.container('b'*32); client=Mock(); client.containers.list.return_value=[container]
        with self.assertRaises(ValueError): cleanup_verified(client,OWNER)
        container.remove.assert_not_called()

    def test_persistent_daemon_error_remains_blocked(self):
        client=Mock(); client.containers.list.side_effect=docker.errors.APIError('unavailable')
        with self.assertRaises(RuntimeError): cleanup_verified(client,OWNER,attempts=2,sleep=Mock())

    def receipt(self, directory):
        result={'owner':OWNER,'status':'timeout','seconds':223.479,'cap_seconds':223.474,
            'base_task_success':None,'joint_success':0,'cleanup_error':'APIError','error_type':None}
        (directory/'started.json').write_text(json.dumps({'owner':OWNER}),encoding='utf-8')
        path=directory/'result.json'; path.write_text(json.dumps(result),encoding='utf-8')
        return path,result

    def test_recovery_preserves_timeout_score_and_original_receipt(self):
        with TemporaryDirectory() as t:
            path,original=self.receipt(Path(t)); original_bytes=path.read_bytes()
            cleanup=Mock(return_value={'verified_no_owned_containers':True,'removed_containers':[]})
            proof=recover_receipt(path,object(),cleanup)
            after=json.loads(path.read_text(encoding='utf-8'))
            for key in ('status','seconds','cap_seconds','base_task_success','joint_success'):
                self.assertEqual(after[key],original[key])
            self.assertIsNone(after['cleanup_error'])
            self.assertEqual((path.parent/proof['original_receipt']).read_bytes(),original_bytes)
            self.assertIsNone(recover_receipt(path,object(),cleanup))
            self.assertEqual(cleanup.call_count,1)

    def test_failed_recovery_does_not_rewrite_result(self):
        with TemporaryDirectory() as t:
            path,_=self.receipt(Path(t)); original=path.read_bytes()
            with self.assertRaises(RuntimeError):
                recover_receipt(path,object(),Mock(side_effect=RuntimeError('still there')))
            self.assertEqual(path.read_bytes(),original)

    def test_process_failure_is_not_cleared_by_container_cleanup(self):
        with TemporaryDirectory() as t:
            path,result=self.receipt(Path(t)); result['error_type']='process_cleanup:TimeoutExpired'
            path.write_text(json.dumps(result),encoding='utf-8'); cleanup=Mock()
            with self.assertRaises(ValueError): recover_receipt(path,object(),cleanup)
            cleanup.assert_not_called()


if __name__=='__main__': unittest.main()
