"""A release must calibrate without downloading a dataset or checker scripts."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

RUNTIME = Path(__file__).parents[1]/'src/swe_persona/vendor/swe_pro/shell/portable/runtime-v3'
RUNNER = RUNTIME/'runner'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def stub(name, **fields):
    module = types.ModuleType(name)
    module.__dict__.update(fields)
    return module


class PackagedCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = load('shell_calibration_task_fixture', RUNNER/'task_adapter.py')
        deps = {'task_adapter': self.adapter,
                'dockerx': stub('dockerx', Container=object, Mount=object),
                'checkers': stub('checkers', CheckerRegistry=object),
                'llm': stub('llm', CallLog=object, Llm=object),
                'registry': stub('registry', build_candidates=Mock()),
                'rubric_checker': stub('rubric_checker', GROUNDING_PROTOCOL='registered-evidence-ids-v2',
                                      CLAUSE_PROTOCOL='literal-clause-truth-v1', grading_identity=Mock())}
        with patch.dict(sys.modules, deps):
            self.calibrate = load('shell_calibration_fixture_loader', RUNNER/'validate_rubric.py')

    def test_default_control_materializes_without_hf_or_official_asset_cache(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(self.adapter, 'load_rows', side_effect=AssertionError('HF access forbidden')), \
                patch.object(self.adapter, '_official_asset', side_effect=AssertionError('download forbidden')):
            manifest, assets, identity = self.calibrate.packaged_calibration_fixture()
            out = manifest.materialize_checker_assets(Path(temp)/'assets', RUNNER/'fixtures',
                                                      official_assets=assets)
            self.assertEqual(manifest.row_index_zero, 43)
            self.assertEqual(manifest.row['repo'], 'navidrome/navidrome')
            self.assertEqual(identity['protocol'], 'packaged-rubric-control-v1')
            self.assertEqual((out/'gold.patch').read_text(), manifest.row['patch'])
            self.assertEqual((out/'test.patch').read_text(), manifest.row['test_patch'])
            for name, text in assets.items():
                self.assertEqual((out/name).read_text(), text)
            hashes = json.loads((out/'asset_hashes.json').read_text())
            self.assertEqual(hashes['parser.py'], identity['asset_sha256']['parser.py'])
            self.assertEqual(hashes['run_script.sh'], identity['asset_sha256']['run_script.sh'])

    def test_corrupt_control_provenance_task_or_assets_fails_closed(self):
        fixture = json.loads((RUNNER/'fixtures/rubric-calibration.json').read_text())
        variants = []
        bad = deepcopy(fixture); bad['dataset']['revision'] = 'unversioned'; variants.append(bad)
        bad = deepcopy(fixture); bad['row']['patch'] += '\nchanged'; variants.append(bad)
        bad = deepcopy(fixture); bad['official_assets']['parser.py'] += '\nchanged'; variants.append(bad)
        bad = deepcopy(fixture); del bad['official_assets']['run_script.sh']; variants.append(bad)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'fixture.json'
            for bad in variants:
                with self.subTest(bad=bad['protocol']):
                    path.write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        self.calibrate.packaged_calibration_fixture(path)

    def test_evaluator_errors_cannot_pass_a_negative_control(self):
        for category in ['judge_error', 'evidence_collection_error']:
            result = types.SimpleNamespace(status='unknown', evidence={'error_category': category})
            self.assertFalse(self.calibrate.calibration_result_ok(result, {'fail', 'unknown'}))
        result = types.SimpleNamespace(status='fail', evidence={})
        self.assertTrue(self.calibrate.calibration_result_ok(result, {'fail', 'unknown'}))
        self.assertFalse(self.calibrate.calibration_result_ok(result, {'pass'}))


if __name__ == '__main__':
    unittest.main()
