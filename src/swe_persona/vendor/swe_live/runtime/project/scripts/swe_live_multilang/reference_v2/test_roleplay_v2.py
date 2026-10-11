import bootstrap
import unittest
from types import SimpleNamespace
from roleplay import dialogue_persona, validate_fit, fit_messages
from registry import load_persona
from personas import POOL_PERSONAS
from protected_patch import paths, project
from collection.run_roleplay import summarize
from collection.online import budget_after, advance_budget


class RoleplayRepairTests(unittest.TestCase):
    def test_ten_profiles_preserve_core_but_do_not_expose_fixture_gates(self):
        for name in POOL_PERSONAS:
            original = load_persona(name); profile = dialogue_persona(original)
            self.assertEqual(profile['id'], name)
            self.assertEqual(profile['attributes']['constraints'], original['attributes']['constraints'])
            self.assertNotIn('episode_binding', profile['attributes'])
            self.assertNotIn('evolved_requirements', profile['attributes'])

    def test_uncertainty_runs_but_rejection_requires_real_evidence(self):
        m = SimpleNamespace(instruction='Fix the parser.', assert_no_leak=lambda *a:None)
        validate_fit({'decision':'uncertain','reason':'No incident evidence','public_quote':'','opening':'Please fix it.'},m)
        with self.assertRaises(ValueError):
            validate_fit({'decision':'explicit_contradiction','reason':'conflict','public_quote':'made up','opening':''},m)

    def test_projection_preserves_source_and_excludes_all_official_test_edits(self):
        test = 'diff --git a/test/a b/test/a\n--- a/test/a\n+++ b/test/a\n@@ -1 +1 @@\n-old\n+new\n'
        source = test.replace('test/a','src/a')
        self.assertEqual(project(source+test,paths(test)),(source,['test/a']))
        self.assertEqual(project(source,paths(test)),(source,[]))
        self.assertEqual(project(test,paths(test)),('', ['test/a']))

    def test_historical_rescores_do_not_tighten_runtime_or_mix_success_rate(self):
        record={'status':'historical_rescored','seconds':1,'cap_seconds':900,'base_task_success':1}
        state=advance_budget(budget_after([]),[record])
        self.assertEqual(state['observations_seen'],0)
        report=summarize([record],10,state)
        self.assertIsNone(report['original_success_rate_including_timeouts'])
        self.assertEqual(report['historical_original_successes'],1)


if __name__ == '__main__': unittest.main()
