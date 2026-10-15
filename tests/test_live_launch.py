"""SWE-Live must start under the managed clock and keep gold preflight off the interaction budget."""
from pathlib import Path
import unittest
from types import SimpleNamespace

from swe_persona.swe_live import gold_preflight_seconds, live_episode_argv


def episode(deadline):
    return SimpleNamespace(gold_dir=Path('gold'), sample_seconds=157.71891049999977,
                           deadline_monotonic=deadline, output_dir=Path('out'))


class LiveLaunchTests(unittest.TestCase):
    def argv(self, deadline):
        row={'persona':{'persona_id':'student_needing_explanation'}}
        config={'agent':{'model':'example'}}
        return live_episode_argv(Path('task'), row, config, episode(deadline))

    def test_missing_deadline_is_omitted(self):
        argv=self.argv(None)
        self.assertNotIn('--deadline-monotonic', argv)
        self.assertNotIn('None', argv)
        self.assertEqual(argv[argv.index('--sample-seconds')+1], '157.71891049999977')

    def test_numeric_deadline_is_forwarded(self):
        argv=self.argv(123.5)
        self.assertEqual(argv[argv.index('--deadline-monotonic')+1], '123.5')

    def test_gold_preflight_floor_is_five_minutes(self):
        self.assertEqual(gold_preflight_seconds(157.71891049999977), 300)
        self.assertEqual(gold_preflight_seconds(540), 540)


if __name__=='__main__':
    unittest.main()
