"""Release metadata must describe the code and files users actually download."""
import hashlib
import json
from pathlib import Path
import unittest

from swe_persona.cli import runtime_hash

ROOT=Path(__file__).parents[1]


class ReleaseIntegrityTests(unittest.TestCase):
    def test_release_runtime_matches_current_source(self):
        release=json.loads((ROOT/'release.json').read_text())
        self.assertEqual(release['runtime_sha256'],runtime_hash())

    def test_distributed_files_match_checksums(self):
        for line in (ROOT/'SHA256SUMS').read_text().splitlines():
            expected,name=line.split(None,1)
            with self.subTest(path=name):
                with (ROOT/name).open('rb') as stream:
                    self.assertEqual(hashlib.file_digest(stream,'sha256').hexdigest(),expected)

    def test_checksum_manifest_covers_source_and_tests(self):
        names={line.split(None,1)[1] for line in (ROOT/'SHA256SUMS').read_text().splitlines()}
        for directory in (ROOT/'src/swe_persona',ROOT/'tests'):
            for path in directory.rglob('*.py'):
                if '__pycache__' not in path.parts:
                    self.assertIn(path.relative_to(ROOT).as_posix(),names)


if __name__=='__main__':unittest.main()
