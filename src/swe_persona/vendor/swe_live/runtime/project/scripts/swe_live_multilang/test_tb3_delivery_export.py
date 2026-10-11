import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from export_tb3_delivery import save, ROOT


class ReviewEncodingTests(unittest.TestCase):
    def test_original_test_names_roundtrip_without_loss(self):
        value={'test_names':['正常名称','lone surrogate '+chr(0xd800),'literal \\ud800'],
               'original_reward':0,'seconds':153.1540083,'unknown':None}
        with TemporaryDirectory() as temp:
            target=Path(temp)/'verifier/report.json'
            save(target,value)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')),value)

    def test_only_workspace_prefix_is_projected(self):
        value={'path':str(ROOT/'somewhere/file.json'),'message':'Keep all original test outcomes','score':1}
        with TemporaryDirectory() as temp:
            target=Path(temp)/'result.json';save(target,value)
            result=json.loads(target.read_text(encoding='utf-8'))
            self.assertTrue(result['path'].startswith('<workspace>'))
            self.assertEqual(result['message'],value['message']);self.assertEqual(result['score'],1)


if __name__=='__main__':unittest.main()
