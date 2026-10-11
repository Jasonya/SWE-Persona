import bootstrap
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from collection.run_once import collect, collect_history, report
from run_fullset import atomic_save
from support import file_hash, read


class OnePassTests(unittest.TestCase):
    def manifest(self, personas):
        return {'tasks':[{'key':'0001-js','instance_id':'x__y-1','path':'task.jsonl','sha256':'a'}],
            'personas':personas,'candidate_pairs':len(personas),'protocol':'v2',
            'config':{'seed':'fixed','initial_seconds':506.25,'maximum_seconds':3600,
                'update_every':20,'model':'test','max_turns':4,'max_steps':14}}

    def test_all_historical_outcomes_skip_before_image_or_model_work(self):
        statuses=['completed','timeout','not_applicable','evaluation_invalid','infrastructure_invalid','interrupted','historical_rescored']
        m=self.manifest(statuses)
        carried=[{'pair_id':'0001-js/'+s,'status':s,'base_task_success':None,'carried_forward':True} for s in statuses]
        with TemporaryDirectory() as t, patch('collection.run_once.get_image') as image, patch('collection.run_once.supervise_sample') as sample:
            result=collect(Path(t),m,carried,object())
            self.assertEqual(result,carried); image.assert_not_called(); sample.assert_not_called()
            self.assertEqual(read(Path(t)/'summary.json')['remaining'],0)

    def test_new_failure_is_retained_and_resume_never_buys_another_attempt(self):
        m=self.manifest(['old','new']); carried=[{'pair_id':'0001-js/old','status':'not_applicable','carried_forward':True}]
        with TemporaryDirectory() as t, ExitStack() as stack:
            study=Path(t)
            stack.enter_context(patch('collection.run_once.Manifest',return_value=object()))
            stack.enter_context(patch('collection.run_once.get_image',return_value=(SimpleNamespace(id='image'),False)))
            stack.enter_context(patch('collection.run_once.gold_directory',return_value=study/'gold'))
            stack.enter_context(patch('collection.run_once.release_image'))
            def run(command,directory,seconds):
                self.assertNotIn('replay_roleplay.py',' '.join(command))
                result={'status':'completed','base_task_success':0,'seconds':30,'cap_seconds':seconds}
                atomic_save(directory/'result.json',result); return result
            sample=stack.enter_context(patch('collection.run_once.supervise_sample',side_effect=run))
            collect(study,m,carried,object()); collect(study,m,carried,object())
            self.assertEqual(sample.call_count,1)
            self.assertEqual(read(study/'summary.json')['processed_pairs'],2)
            self.assertEqual(read(study/'successful-cases.json'),[])

    def test_history_uses_latest_not_best_score_and_deduplicates(self):
        with TemporaryDirectory() as t:
            studies=[]
            for i,score in enumerate((1,0)):
                s=Path(t)/str(i); s.mkdir(); task=s/'task'; task.write_text('{}',encoding='utf-8')
                atomic_save(s/'manifest.json',{'protocol':str(i),'personas':['p'],
                    'tasks':[{'key':'0001-js','path':'task','sha256':file_hash(task)}]})
                atomic_save(s/'samples/0001-js/p/result.json',{'status':'completed','base_task_success':score})
                studies.append(s)
            rows=collect_history(studies)
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['base_task_success'],0)
            self.assertEqual(rows[0]['source_study'],'1')


if __name__=='__main__': unittest.main()
