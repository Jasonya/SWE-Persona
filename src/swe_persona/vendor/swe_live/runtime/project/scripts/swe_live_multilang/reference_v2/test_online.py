import bootstrap
import unittest
from collection.online import censored_p20, budget_after, advance_budget, ordered
from collection.timing import next_reference_cap, round_ceiling


class OnlineTimingTests(unittest.TestCase):
    def test_incremental_budget_matches_replay_including_censored_and_invalid_records(self):
        rows = [{'status': 'completed', 'seconds': 400 + i} for i in range(25)]
        rows += [{'status': 'evaluation_invalid'}, {'status': 'timeout', 'cap_seconds': 900}] * 25
        state = budget_after([])
        history = []
        for row in rows:
            history.append(row)
            state = advance_budget(state, history)
            self.assertEqual(state['next_cap_seconds'], budget_after(history)['next_cap_seconds'])
        self.assertEqual(state['records_seen'], len(rows))

    def test_uncensored_p20_matches_order_statistic_and_counts_failed_solutions(self):
        rows = [{'status': 'completed', 'seconds': i, 'joint_success': 0} for i in range(1, 101)]
        self.assertEqual(censored_p20(rows)['seconds'], 20)

    def test_timeouts_are_not_dropped_as_if_only_fast_completions_existed(self):
        rows = [{'status': 'completed', 'seconds': i} for i in range(1, 31)]
        rows += [{'status': 'timeout', 'cap_seconds': 100}] * 70
        estimate = censored_p20(rows)
        self.assertEqual(estimate['seconds'], 20)
        self.assertEqual(estimate['population'], 100)
        self.assertFalse(estimate['exact_global_rank_claim'])

    def test_unidentified_quantile_expands_budget_and_invalid_errors_do_not_drive_it(self):
        rows = [{'status': 'timeout', 'cap_seconds': 900}] * 20
        self.assertIsNone(censored_p20(rows)['seconds'])
        result = budget_after(rows)
        self.assertEqual(result['next_cap_seconds'], 1350)
        self.assertEqual(budget_after(rows + [{'status': 'evaluation_invalid', 'seconds': .01}] * 80)['next_cap_seconds'], 1350)
        self.assertEqual(budget_after(rows[:19])['next_cap_seconds'], 900)

    def test_updates_are_deterministic_bounded_and_do_not_use_success_labels(self):
        rows = [{'status': 'completed', 'seconds': 100, 'joint_success': 0}] * 40
        result = budget_after(rows)
        self.assertEqual(result['next_cap_seconds'], 506.25)
        self.assertEqual(result, budget_after([dict(r, joint_success=1) for r in rows]))
        self.assertEqual(budget_after([{'status': 'timeout', 'cap_seconds': 900}] * 100)['next_cap_seconds'], 3600)

    def test_censored_before_completion_is_an_estimate_not_claimed_exact_rank(self):
        rows = [{'status': 'timeout', 'cap_seconds': 5}] * 80
        rows += [{'status': 'completed', 'seconds': i} for i in range(10, 30)]
        self.assertEqual(censored_p20(rows)['seconds'], 13)
        self.assertFalse(censored_p20(rows)['exact_global_rank_claim'])

    def test_order_is_frozen_and_small_pilots_cannot_tighten_full_reference_bounds(self):
        items = [{'id': str(i)} for i in range(100)]
        self.assertEqual(ordered(items, lambda r: r['id'], 'seed'), ordered(list(reversed(items)), lambda r: r['id'], 'seed'))
        rows = [{'status': 'completed', 'seconds': i} for i in range(1, 21)]
        self.assertEqual(next_reference_cap(rows[:19], 100, 600), 600)
        self.assertEqual(next_reference_cap(rows, 100, 600), 20)
        self.assertEqual(round_ceiling(600, 7200, 5), 7200)


if __name__ == '__main__':
    unittest.main()
