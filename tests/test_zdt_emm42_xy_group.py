import importlib.util
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


EXTRAS_PATH = Path(__file__).resolve().parents[1] / 'klipper' / 'extras'
sys.path.insert(0, str(EXTRAS_PATH))
MODULE_PATH = EXTRAS_PATH / 'zdt_emm42_xy_group.py'
SPEC = importlib.util.spec_from_file_location(
    'zdt_emm42_xy_group_under_test', MODULE_PATH)
XY_GROUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(XY_GROUP)


class FakeMember:
    def __init__(self, name, addr, error_deg, error_mm, eventtime):
        self.name = name
        self.addr = addr
        self.error_history = deque([{
            'time': eventtime,
            'error_deg': error_deg,
            'error_mm': error_mm,
        }])
        self.status = {
            'online': True,
            'error_deg': error_deg,
            'error_mm': error_mm,
            'pid_kp': 62000,
            'pid_ki': 100,
            'pid_kd': 62000,
            'stalled': False,
            'stall_protect': False,
        }
        self.autotune_pid_min = 0
        self.autotune_pid_max = 100000
        self.autotune_abort = False
        self.writes = []

    def get_status(self, eventtime):
        return dict(self.status)

    def _write_pid(self, pid, store=0, verify=True):
        self.writes.append((tuple(pid), store, verify))
        return True


def make_group():
    group = XY_GROUP.ZdtEmm42XYGroup.__new__(
        XY_GROUP.ZdtEmm42XYGroup)
    group.expected_member_count = 2
    group.name = 'xy'
    group.member_names = ['motor_a', 'motor_b']
    group.members = [
        FakeMember('motor_a', 4, 1.0, 0.10, 10.00),
        FakeMember('motor_b', 5, -0.5, -0.05, 10.01),
    ]
    group.max_error_deg = 6.5
    group.max_spread_deg = None
    group.warning_hold_time = 0.5
    group.warning_clear_time = 0.5
    group.warning_log_interval = 30.0
    group.sample_interval = 0.025
    group.sample_skew_tolerance = 0.04
    group.history = deque(maxlen=402)
    group.last_sample_times = {}
    group.warning_active = False
    group.warning_reasons = []
    group.warning_since = None
    group.warning_pending_since = None
    group.warning_clear_since = None
    group.warning_signature = None
    group.active_warning_signature = None
    group.last_warning_log_time = 0.0
    group.autotune_active = False
    group.autotune_abort = False
    group.monitor_suspended = False
    group.last = group._empty_status()
    return group


class ZdtEmm42XYGroupTest(unittest.TestCase):
    def test_collects_two_member_sample_and_reports_corexy_type(self):
        group = make_group()

        group._collect_sample(10.02)

        self.assertEqual(group.last['group_type'], 'corexy')
        self.assertTrue(group.last['online'])
        self.assertEqual(len(group.history), 1)
        self.assertEqual(
            [value['name'] for value in group.history[-1]['members']],
            ['motor_a', 'motor_b'])
        self.assertAlmostEqual(group.last['max_abs_error_deg'], 1.0)
        self.assertAlmostEqual(group.last['spread_deg'], 1.5)

    def test_corexy_warning_ignores_spread_but_checks_absolute_error(self):
        group = make_group()
        snapshots = [
            {'name': 'motor_a', 'online': True, 'error_deg': 3.5,
             'stalled': False, 'stall_protect': False},
            {'name': 'motor_b', 'online': True, 'error_deg': -3.5,
             'stalled': False, 'stall_protect': False},
        ]

        self.assertEqual(group._current_reasons(snapshots), [])
        snapshots[0]['error_deg'] = 6.6
        self.assertEqual(
            group._current_reasons(snapshots)[0]['code'],
            'absolute_error')

    def test_centered_test_square_preserves_z_and_extrusion(self):
        group = make_group()
        status = {
            'axis_minimum': [0.0, -6.0, -5.0, 0.0],
            'axis_maximum': [300.0, 323.0, 210.0, 0.0],
        }

        origin = group._centered_test_origin(
            status, [275.0, 300.0, 25.0, 4.0], 50.0)

        self.assertEqual(origin, [125.0, 133.5, 25.0, 4.0])

    def test_centered_test_square_rejects_small_workspace(self):
        group = make_group()
        status = {
            'axis_minimum': [0.0, 0.0, 0.0, 0.0],
            'axis_maximum': [40.0, 100.0, 100.0, 0.0],
        }

        with self.assertRaisesRegex(RuntimeError, 'does not fit'):
            group._centered_test_origin(
                status, [0.0, 0.0, 10.0, 0.0], 50.0)

    def test_group_score_weights_mean_and_worst_member(self):
        group = make_group()
        samples = []
        for index in range(4):
            samples.append({
                'phase': 'motion:long', 'time': float(index),
                'errors': [1.0, 2.0],
            })
        samples.append({
            'phase': 'settle', 'time': 5.0,
            'errors': [0.1, 0.2],
        })

        metrics = group._score_group_samples(samples)

        self.assertIsNotNone(metrics)
        member_scores = [
            value['score'] for value in metrics['member_metrics']]
        expected_mean = sum(member_scores) / 2.0
        self.assertAlmostEqual(metrics['mean_score'], expected_mean)
        self.assertAlmostEqual(metrics['worst_score'], max(member_scores))
        self.assertAlmostEqual(
            metrics['score'],
            0.70 * expected_mean + 0.30 * max(member_scores))

    def test_group_safety_rejects_either_member(self):
        group = make_group()

        with self.assertRaisesRegex(
                XY_GROUP.GroupCandidateRejected, 'position error'):
            group._check_group_samples([{
                'phase': 'motion:long', 'time': 1.0,
                'errors': [1.0, -6.6],
            }])

    def test_repeated_metrics_use_median(self):
        group = make_group()
        metrics = group._aggregate_repeat_metrics([
            {'score': 3.0, 'mean_score': 2.0,
             'worst_score': 4.0, 'samples': 10},
            {'score': 1.0, 'mean_score': 4.0,
             'worst_score': 2.0, 'samples': 12},
        ])

        self.assertEqual(metrics['score'], 2.0)
        self.assertEqual(metrics['mean_score'], 3.0)
        self.assertEqual(metrics['worst_score'], 3.0)
        self.assertEqual(metrics['samples'], 22)

    def test_balanced_profile_runs_two_factors_three_routes_and_repeats(self):
        group = make_group()
        group._run_profile = mock.Mock(return_value={
            'score': 2.0, 'mean_score': 1.5,
            'worst_score': 2.5, 'samples': 10,
        })
        gcmd = mock.Mock()

        metrics = group._evaluate_profiles(
            gcmd, 'baseline', mock.sentinel.toolhead,
            [0.0, 0.0, 10.0, 0.0], 50.0, 200.0, 4000.0,
            0.5, 2, (0.50, 1.00))

        self.assertEqual(group._run_profile.call_count, 12)
        self.assertEqual(gcmd.respond_info.call_count, 6)
        self.assertEqual(metrics['score'], 2.0)
        self.assertEqual(metrics['samples'], 120)

    def test_candidate_changes_only_selected_member_and_parameter(self):
        group = make_group()
        current = [(62000, 100, 62000), (61000, 90, 61000)]
        bounds = [
            [(30000, 100000), (0, 1000), (30000, 100000)],
            [(30000, 100000), (0, 1000), (30000, 100000)],
        ]

        candidate = group._candidate(current, 1, 2, 5000, bounds)

        self.assertEqual(candidate[0], current[0])
        self.assertEqual(candidate[1], (61000, 90, 66000))

    def test_default_search_cycle_visits_both_members_and_all_parameters(self):
        group = make_group()

        targets = [group._search_target(index) for index in range(12)]

        self.assertEqual(targets, [
            (0, 0), (0, 1), (0, 2),
            (1, 0), (1, 1), (1, 2),
        ] * 2)

    def test_cancel_marks_group_and_both_members(self):
        group = make_group()
        group.autotune_active = True
        gcmd = mock.Mock()

        group.cmd_AUTOTUNE_CANCEL(gcmd)

        self.assertTrue(group.autotune_abort)
        self.assertTrue(all(member.autotune_abort for member in group.members))
        self.assertIn(
            'active profile will finish',
            gcmd.respond_info.call_args.args[0])

    def test_partial_pid_write_can_restore_both_originals(self):
        group = make_group()
        originals = [(62000, 100, 62000), (61000, 90, 61000)]
        group.members[1]._write_pid = mock.Mock(return_value=False)

        ok, failed = group._write_pid_set(
            [(63000, 100, 62000), (62000, 90, 61000)],
            store=1, verify=True)
        failures = group._restore_pid_set(originals, store=1)

        self.assertFalse(ok)
        self.assertEqual(failed.name, 'motor_b')
        self.assertEqual(failures, ['motor_b'])
        self.assertEqual(group.members[0].writes[-1][0], originals[0])


if __name__ == '__main__':
    unittest.main()
