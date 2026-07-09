import importlib.util
import math
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


MODULE_PATH = (Path(__file__).resolve().parents[1] / 'klipper' / 'extras' /
               'zdt_emm42_group.py')
SPEC = importlib.util.spec_from_file_location(
    'zdt_emm42_group_under_test', MODULE_PATH)
GROUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GROUP)


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
    group = GROUP.ZdtEmm42Group.__new__(GROUP.ZdtEmm42Group)
    group.name = 'z'
    group.member_names = ['z_left', 'z_right', 'z_rear']
    group.members = [
        FakeMember('z_left', 1, 1.0, 0.01, 10.00),
        FakeMember('z_right', 2, -0.5, -0.005, 10.01),
        FakeMember('z_rear', 3, 0.25, 0.0025, 10.02),
    ]
    group.max_error_deg = 5.0
    group.max_spread_deg = 2.0
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
    group.monitor_suspended = False
    group.last = group._empty_status()
    return group


class ZdtEmm42GroupTest(unittest.TestCase):
    def test_collects_one_aligned_three_member_sample(self):
        group = make_group()

        group._collect_sample(10.02)

        self.assertTrue(group.last['online'])
        self.assertEqual(len(group.history), 1)
        sample = group.history[-1]
        self.assertEqual(
            [value['name'] for value in sample['members']],
            ['z_left', 'z_right', 'z_rear'])
        self.assertAlmostEqual(sample['max_abs_error_deg'], 1.0)
        self.assertAlmostEqual(sample['spread_deg'], 1.5)
        self.assertFalse(group.last['warning'])

    def test_does_not_emit_duplicate_or_skewed_group_samples(self):
        group = make_group()
        group._collect_sample(10.02)
        group._collect_sample(10.03)
        group.members[2].error_history[-1]['time'] = 10.20

        group._collect_sample(10.20)

        self.assertEqual(len(group.history), 1)

    def test_warning_requires_hold_time_and_clears_after_healthy_hold(self):
        group = make_group()
        group.members[0].status['error_deg'] = 6.0

        group._collect_sample(10.0)
        self.assertFalse(group.warning_active)
        group._collect_sample(10.49)
        self.assertFalse(group.warning_active)
        group._collect_sample(10.50)
        self.assertTrue(group.warning_active)
        self.assertEqual(
            group.warning_reasons[0]['code'], 'absolute_error')

        group.members[0].status['error_deg'] = 1.0
        group._collect_sample(11.0)
        self.assertTrue(group.warning_active)
        group._collect_sample(11.50)
        self.assertFalse(group.warning_active)

    def test_warning_log_is_throttled_while_reason_type_is_unchanged(self):
        group = make_group()
        reasons = [{
            'code': 'absolute_error', 'value': 6.0, 'limit': 5.0,
        }]

        with mock.patch.object(GROUP.logging, 'warning') as warning:
            group._update_warning(reasons, 10.0)
            group._update_warning(reasons, 10.5)
            group._update_warning([{
                'code': 'absolute_error', 'value': 6.2, 'limit': 5.0,
            }], 10.6)
            self.assertEqual(warning.call_count, 1)

            group._update_warning(reasons, 40.5)
            self.assertEqual(warning.call_count, 2)

    def test_offline_member_is_reported_without_motion_side_effects(self):
        group = make_group()
        group.members[1].status['online'] = False

        reasons = group._current_reasons([
            group._member_snapshot(member, member.get_status(10.0))
            for member in group.members])

        self.assertEqual(reasons, [{'code': 'offline', 'member': 'z_right'}])
        self.assertFalse(hasattr(group, 'toolhead'))

    def test_group_score_weights_members_and_spread(self):
        group = make_group()
        samples = []
        for index in range(4):
            samples.append({
                'phase': 'motion:z', 'time': float(index),
                'errors': [1.0, 0.0, -1.0],
            })
        samples.append({
            'phase': 'settle', 'time': 5.0,
            'errors': [0.1, 0.0, -0.1],
        })

        metrics = group._score_group_samples(samples)

        self.assertIsNotNone(metrics)
        expected_member = (0.955 + 0.0 + 0.955) / 3.0
        expected_spread = 1.91
        self.assertAlmostEqual(
            metrics['score'], 0.70 * expected_member +
            0.30 * expected_spread)
        self.assertEqual(metrics['samples'], 5)

    def test_group_safety_checks_absolute_error_and_spread(self):
        group = make_group()
        safe = [{
            'phase': 'motion:z', 'time': 1.0,
            'errors': [1.0, 0.0, -0.5],
        }]
        group._check_group_samples(safe)

        with self.assertRaisesRegex(
                GROUP.GroupCandidateRejected, 'position error'):
            group._check_group_samples([{
                'phase': 'motion:z', 'time': 1.0,
                'errors': [5.1, 4.9, 5.0],
            }])
        with self.assertRaisesRegex(
                GROUP.GroupCandidateRejected, '3Z spread'):
            group._check_group_samples([{
                'phase': 'motion:z', 'time': 1.0,
                'errors': [1.1, -1.1, 0.0],
            }])

    def test_repeated_motion_uses_median_score_and_sums_samples(self):
        group = make_group()
        group._run_motion = mock.Mock(side_effect=[
            {'score': 3.0, 'member_score': 2.0,
             'spread_score': 5.0, 'samples': 10},
            {'score': 1.0, 'member_score': 3.0,
             'spread_score': 4.0, 'samples': 11},
            {'score': 2.0, 'member_score': 1.0,
             'spread_score': 6.0, 'samples': 12},
        ])
        responses = []
        gcmd = mock.Mock()
        gcmd.respond_info.side_effect = responses.append

        metrics = group._run_repeated_motions(
            gcmd, 'baseline', 3, mock.sentinel.toolhead,
            [0.0, 0.0, 10.0, 0.0], 10.0, 5.0, 100.0, 0.5)

        self.assertEqual(group._run_motion.call_count, 3)
        self.assertEqual(metrics['score'], 2.0)
        self.assertEqual(metrics['member_score'], 2.0)
        self.assertEqual(metrics['spread_score'], 5.0)
        self.assertEqual(metrics['samples'], 33)
        self.assertIn('repeat 3/3', responses[-1])

    def test_repeated_motion_stops_on_first_safety_rejection(self):
        group = make_group()
        rejection = GROUP.GroupCandidateRejected('position error')
        group._run_motion = mock.Mock(side_effect=[
            {'score': 1.0, 'member_score': 1.0,
             'spread_score': 1.0, 'samples': 10},
            rejection,
            {'score': 2.0, 'member_score': 2.0,
             'spread_score': 2.0, 'samples': 10},
        ])
        gcmd = mock.Mock()

        with self.assertRaises(GROUP.GroupCandidateRejected):
            group._run_repeated_motions(
                gcmd, 'candidate', 3, mock.sentinel.toolhead,
                [0.0, 0.0, 10.0, 0.0], 10.0, 5.0, 100.0, 0.5)

        self.assertEqual(group._run_motion.call_count, 2)

    def test_candidate_changes_only_one_member_and_respects_bounds(self):
        group = make_group()
        current = [(62000, 100, 62000)] * 3

        candidate, direction = group._candidate(
            current, 1, 2, 1, 5000)

        self.assertEqual(candidate[0], current[0])
        self.assertEqual(candidate[1], (62000, 100, 67000))
        self.assertEqual(candidate[2], current[2])
        self.assertEqual(direction, 1)

    def test_default_27_round_search_cycles_each_member_and_parameter(self):
        group = make_group()
        one_cycle = [
            (member_index, parameter)
            for member_index in range(3)
            for parameter in range(3)
        ]

        targets = [group._search_target(iteration) for iteration in range(27)]

        self.assertEqual(targets, one_cycle * 3)

    def test_cancel_requests_abort_for_group_and_all_members(self):
        group = make_group()
        group.autotune_active = True
        responses = []
        gcmd = mock.Mock()
        gcmd.respond_info.side_effect = responses.append

        group.cmd_AUTOTUNE_CANCEL(gcmd)

        self.assertTrue(group.autotune_abort)
        self.assertTrue(all(member.autotune_abort for member in group.members))
        self.assertIn('current move will finish', responses[0])

    def test_partial_pid_set_failure_can_restore_all_originals(self):
        group = make_group()
        originals = [(62000, 100, 62000)] * 3
        group.members[1]._write_pid = lambda *args, **kwargs: False

        ok, failed = group._write_pid_set(
            [(63000, 100, 62000)] * 3, store=1, verify=True)
        failures = group._restore_pid_set(originals, store=1)

        self.assertFalse(ok)
        self.assertEqual(failed.name, 'z_right')
        self.assertEqual(failures, ['z_right'])
        self.assertEqual(group.members[0].writes[-1][0], originals[0])
        self.assertEqual(group.members[2].writes[-1][0], originals[2])


if __name__ == '__main__':
    unittest.main()
