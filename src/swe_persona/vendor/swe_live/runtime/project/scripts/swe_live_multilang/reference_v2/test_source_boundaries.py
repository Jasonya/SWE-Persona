import bootstrap
import ast
from pathlib import Path
import unittest

from support import ROOT, COLLECTION_MODULES, collection_source_hashes, source_hashes


class SourceBoundaryTests(unittest.TestCase):
    def test_collection_and_episode_sources_are_disjoint_and_captured(self):
        runtime, collection = source_hashes(), collection_source_hashes()
        self.assertFalse(set(runtime) & set(collection))
        self.assertTrue(COLLECTION_MODULES <= {Path(p).name for p in collection})
        self.assertIn('scripts/swe_live_multilang/reference_v2/run_reference.py', runtime)
        self.assertIn('scripts/swe_live_multilang/reference_v2/calibration.py', runtime)

    def test_runtime_cannot_import_excluded_collection_code(self):
        excluded = {Path(p).stem for p in collection_source_hashes()}
        for relative in source_hashes():
            if not relative.endswith('.py'):
                continue
            tree = ast.parse((ROOT / relative).read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                         [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse(any(name.split('.')[0] in excluded or name.split('.')[0] == 'collection'
                                     for name in names), relative)


if __name__ == '__main__':
    unittest.main()
