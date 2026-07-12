import importlib.util
import math
import struct
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / 'klipper' / 'extras' / 'zdt_emm42.py'
SPEC = importlib.util.spec_from_file_location('zdt_emm42_under_test', MODULE_PATH)
ZDT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ZDT)


class CaptureSocket:
    def __init__(self):
        self.frames = []

    def send(self, frame):
        self.frames.append(frame)


class ReplySocket(CaptureSocket):
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.receive_queue = []

    def send(self, frame):
        super().send(frame)
        if not self.receive_queue:
            self.receive_queue.extend(self.replies)

    def recv(self, size):
        if not self.receive_queue:
            raise BlockingIOError
        return self.receive_queue.pop(0)

    def fileno(self):
        return 0


class FakeReactor:
    NOW = 0.0
    NEVER = 1.0e30

    def __init__(self):
        self.now = 10.0
        self.pauses = []

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.pauses.append(waketime)
        self.now = max(self.now, waketime)

    def update_timer(self, timer, waketime):
        pass


class FakeCommandError(Exception):
    pass


class FakeGcmd:
    def __init__(self, values):
        self.values = values
        self.messages = []

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_float(self, name, default=None, **kwargs):
        value = self.values.get(name, default)
        if value is None:
            raise FakeCommandError('missing ' + name)
        return float(value)

    def get_int(self, name, default=None, **kwargs):
        value = self.values.get(name, default)
        if value is None:
            raise FakeCommandError('missing ' + name)
        return int(value)

    def error(self, message):
        return FakeCommandError(message)

    def respond_info(self, message):
        self.messages.append(message)


def make_monitor():
    monitor = ZDT.ZdtEmm42.__new__(ZDT.ZdtEmm42)
    monitor.name = 'test'
    monitor.addr = 1
    monitor.check_byte = 0x6B
    monitor.checksum_mode = 'fixed'
    monitor.can_payload_includes_addr = False
    monitor.error_poll_interval = 0.05
    monitor.query_timeout = 0.006
    monitor.offline_timeout = 1.0
    monitor.rotation_distance = 40.0
    monitor.microsteps = 16
    monitor.full_steps_per_rotation = 200
    monitor.error_history = deque(maxlen=202)
    monitor.last_error_update_time = None
    monitor.pending_cmd = None
    monitor.pending_error_cmd = None
    monitor.query_index = 0
    monitor.error_count = 0
    monitor.last_error = 'not started'
    monitor.csv_logging = False
    monitor.csv_file = None
    monitor.csv_writer = None
    monitor.autotune_min_samples = 3
    monitor.autotune_pid_min = 0
    monitor.autotune_pid_max = 0xFFFFFFFF
    monitor.autotune_capture_active = False
    monitor.autotune_capture_phase = None
    monitor.autotune_capture_samples = []
    monitor.autotune_max_error_deg = None
    monitor.autotune_safety_violation = None
    monitor.autotune_abort = False
    monitor.last = monitor._empty_status()
    monitor.sock = CaptureSocket()
    return monitor


class ZdtEmm42Test(unittest.TestCase):
    def test_position_error_request_uses_extended_id_and_command_only_payload(self):
        monitor = make_monitor()

        monitor._send_command(ZDT.CMD_POS_ERROR)

        can_id, dlc, payload = struct.unpack(ZDT.CAN_FRAME_FMT, monitor.sock.frames[-1])
        self.assertEqual(can_id & ZDT.CAN_EFF_MASK, 0x0100)
        self.assertTrue(can_id & ZDT.CAN_EFF_FLAG)
        self.assertEqual(dlc, 2)
        self.assertEqual(payload[:2], bytes([ZDT.CMD_POS_ERROR, 0x6B]))

    def test_pid_write_uses_repeated_command_long_frame_format(self):
        monitor = make_monitor()
        extra = bytes([
            ZDT.PID_WRITE_SUBCOMMAND, 0x00,
            0x00, 0x00, 0xF2, 0x30,
            0x00, 0x00, 0x00, 0x64,
            0x00, 0x00, 0xF2, 0x30,
        ])

        monitor._send_long_command(ZDT.CMD_WRITE_PID, extra)

        frames = [struct.unpack(ZDT.CAN_FRAME_FMT, frame)
                  for frame in monitor.sock.frames]
        self.assertEqual(
            [can_id & ZDT.CAN_EFF_MASK for can_id, _, _ in frames],
            [0x0100, 0x0101, 0x0102])
        self.assertEqual(frames[0][1], 8)
        self.assertEqual(frames[0][2][:8], bytes(
            [ZDT.CMD_WRITE_PID, ZDT.PID_WRITE_SUBCOMMAND, 0x00,
             0x00, 0x00, 0xF2, 0x30, 0x00]))
        self.assertEqual(frames[1][1], 8)
        self.assertEqual(frames[1][2][:8], bytes(
            [ZDT.CMD_WRITE_PID, 0x00, 0x00, 0x64, 0x00,
             0x00, 0xF2, 0x30]))
        self.assertEqual(frames[2][1], 2)
        self.assertEqual(frames[2][2][:2], bytes([ZDT.CMD_WRITE_PID, 0x6B]))

    def test_position_pid_response_is_big_endian(self):
        monitor = make_monitor()
        raw = bytearray([
            0x01, ZDT.CMD_READ_PID,
            0x00, 0x00, 0xF2, 0x30,
            0x00, 0x00, 0x00, 0x64,
            0x00, 0x00, 0xF2, 0x30,
            0x6B,
        ])

        self.assertTrue(monitor._record_valid_response(
            ZDT.CMD_READ_PID, raw, 10.0))
        self.assertEqual(monitor.last['pid_kp'], 62000)
        self.assertEqual(monitor.last['pid_ki'], 100)
        self.assertEqual(monitor.last['pid_kd'], 62000)

    def test_position_pid_long_response_is_reassembled(self):
        monitor = make_monitor()
        normalized = bytes([
            0x01, ZDT.CMD_READ_PID,
            0x00, 0x00, 0xF2, 0x30,
            0x00, 0x00, 0x00, 0x64,
            0x00, 0x00, 0xF2, 0x30,
            0x6B,
        ])
        command_only = normalized[1:]
        replies = []
        tail = command_only[1:]
        for packet_no, offset in enumerate((0, 7)):
            payload = bytes([ZDT.CMD_READ_PID]) + tail[offset:offset + 7]
            replies.append(struct.pack(
                ZDT.CAN_FRAME_FMT,
                ZDT.CAN_EFF_FLAG | 0x0100 | packet_no,
                len(payload), payload.ljust(8, b'\x00')))
        monitor.sock = ReplySocket(replies)

        with mock.patch.object(ZDT.select, 'select',
                               side_effect=lambda *_args: ([monitor.sock], [], [])):
            data = monitor._query_long_sync(
                ZDT.CMD_READ_PID, response_len=ZDT.PID_RESPONSE_LEN,
                timeout=0.05)

        self.assertEqual(data, bytearray(normalized))
        self.assertEqual(len(monitor.sock.frames), 1)
        request = struct.unpack(ZDT.CAN_FRAME_FMT, monitor.sock.frames[0])
        self.assertEqual(request[2][:2], bytes([ZDT.CMD_READ_PID, 0x6B]))

    def test_pid_write_waits_and_retries_stale_readback(self):
        monitor = make_monitor()
        monitor.reactor = FakeReactor()
        monitor.pid_write_settle_time = 0.05
        monitor._query_sync = lambda *args, **kwargs: bytearray(
            [0x01, ZDT.CMD_WRITE_PID, 0x02, 0x6B])
        readbacks = iter([(1, 2, 3), (1, 2, 3), (4, 5, 6)])
        monitor._read_pid = lambda timeout=None: next(readbacks)

        self.assertTrue(monitor._write_pid((4, 5, 6), store=0, verify=True))
        self.assertEqual(len(monitor.reactor.pauses), 3)

    def test_autotune_score_and_candidate_search(self):
        monitor = make_monitor()
        samples = [
            {'time': 1.0, 'error_deg': 1.0, 'error_counts': 1},
            {'time': 1.1, 'error_deg': -0.5, 'error_counts': -1},
            {'time': 1.2, 'error_deg': 0.1, 'error_counts': 1},
        ]

        metrics = monitor._score_error_samples(samples)
        candidate, direction = monitor._next_pid_candidate(
            (62000, 100, 62000), 0, 1, 5000)

        self.assertIsNotNone(metrics)
        self.assertGreater(metrics['peak'], 0.0)
        self.assertGreater(metrics['overshoot'], 0.0)
        self.assertEqual(candidate, (67000, 100, 62000))
        self.assertEqual(direction, 1)

    def test_corexy_phase_score_separates_motion_and_settle(self):
        monitor = make_monitor()
        samples = [
            {'phase': 'motion:x', 'error_deg': 1.0},
            {'phase': 'motion:y', 'error_deg': -1.0},
            {'phase': 'motion:diag', 'error_deg': 2.0},
            {'phase': 'motion:return', 'error_deg': 0.5},
            {'phase': 'settle', 'error_deg': 0.1},
            {'phase': 'settle', 'error_deg': -0.2},
        ]

        metrics = monitor._score_print_samples(samples)

        self.assertIsNotNone(metrics)
        self.assertAlmostEqual(metrics['motion_rms'], 1.25)
        self.assertEqual(metrics['motion_p95'], 2.0)
        self.assertEqual(metrics['motion_peak'], 2.0)
        self.assertLess(metrics['settle_rms'], 0.2)

    def test_corexy_route_covers_both_diagonals_and_returns_origin(self):
        monitor = make_monitor()
        origin = [100.0, 100.0, 5.0, 0.0]

        route = monitor._corexy_route(origin, 10.0)
        labels = [label for label, _ in route]

        self.assertIn('diag_return', labels)
        self.assertIn('diag_x_minus_y', labels)
        self.assertEqual(route[-1][1], origin)
        self.assertTrue(all(point[0] >= origin[0] and point[1] >= origin[1]
                            for _, point in route))

    def test_corexy_corner_route_uses_ten_mm_segments(self):
        monitor = make_monitor()
        origin = [20.0, 30.0, 5.0, 0.0]

        route = monitor._corexy_route(origin, 100.0, profile='corner')

        self.assertEqual(len(route), 40)
        self.assertEqual(route[-1][1], origin)
        previous = origin
        for _, point in route:
            self.assertLessEqual(
                math.hypot(point[0] - previous[0],
                           point[1] - previous[1]), 10.000001)
            self.assertGreaterEqual(point[0], origin[0])
            self.assertLessEqual(point[0], origin[0] + 100.0)
            self.assertGreaterEqual(point[1], origin[1])
            self.assertLessEqual(point[1], origin[1] + 100.0)
            previous = point

    def test_corexy_curve_route_has_32_segments_and_stays_in_square(self):
        monitor = make_monitor()
        origin = [20.0, 30.0, 5.0, 0.0]

        route = monitor._corexy_route(origin, 100.0, profile='curve')

        self.assertEqual(len(route), 34)
        self.assertEqual(route[-1][1], origin)
        self.assertTrue(all(
            origin[0] <= point[0] <= origin[0] + 100.0 and
            origin[1] <= point[1] <= origin[1] + 100.0
            for _, point in route))

    def test_corexy_route_is_flushed_once_for_continuous_lookahead(self):
        monitor = make_monitor()

        class Toolhead:
            def __init__(self):
                self.moves = []
                self.wait_count = 0

            def manual_move(self, target, speed):
                self.moves.append((target, speed))

            def wait_moves(self):
                self.wait_count += 1

        toolhead = Toolhead()
        route = monitor._corexy_route(
            [0.0, 0.0, 0.0, 0.0], 100.0, profile='corner')

        monitor._queue_corexy_route(toolhead, route, 200.0, 'corner')

        self.assertEqual(len(toolhead.moves), len(route))
        self.assertEqual(toolhead.wait_count, 1)
        self.assertEqual(monitor.autotune_capture_phase, 'motion:corner')

    def test_corexy_motion_limits_use_official_setter_and_restore_all(self):
        monitor = make_monitor()
        monitor.reactor = FakeReactor()

        class Toolhead:
            def __init__(self):
                self.values = [300.0, 10000.0, 5.0, 0.5]
                self.calls = []

            def get_status(self, eventtime):
                return dict(zip((
                    'max_velocity', 'max_accel', 'square_corner_velocity',
                    'minimum_cruise_ratio'), self.values))

            def set_max_velocities(self, velocity, accel, scv, cruise):
                self.values = [velocity, accel, scv, cruise]
                self.calls.append(tuple(self.values))

        toolhead = Toolhead()
        saved = monitor._set_corexy_motion_limits(toolhead, 200.0, 5000.0)
        monitor._restore_corexy_motion_limits(toolhead, saved)

        self.assertEqual(toolhead.calls, [
            (200.0, 5000.0, 5.0, 0.5),
            (300.0, 10000.0, 5.0, 0.5),
        ])

    def test_corexy_aggregate_uses_repeat_median(self):
        monitor = make_monitor()

        def metrics(score):
            return {
                'score': score,
                'motion_rms': score,
                'motion_p95': score,
                'motion_peak': score,
                'settle_rms': score,
                'samples': 10,
            }

        aggregate = monitor._aggregate_print_metrics([
            (0.4, [metrics(1.0), metrics(100.0), metrics(2.0)]),
            (1.0, [metrics(3.0), metrics(4.0), metrics(5.0)]),
        ])

        self.assertAlmostEqual(aggregate['score'], 3.0)
        self.assertEqual(aggregate['samples'], 60)

    def test_corexy_aggregate_weights_profiles_equally(self):
        monitor = make_monitor()

        def metrics(score):
            return {
                'score': score, 'motion_rms': score,
                'motion_p95': score, 'motion_peak': score,
                'settle_rms': score, 'samples': 10,
            }

        aggregate = monitor._aggregate_print_metrics([
            (1.0, 'long', [metrics(1.0)]),
            (1.0, 'corner', [metrics(2.0)]),
            (1.0, 'curve', [metrics(9.0)]),
        ])

        self.assertAlmostEqual(aggregate['score'], 4.0)
        self.assertEqual(
            [tier['profile'] for tier in aggregate['tiers']],
            ['long', 'corner', 'curve'])

    def test_autotune_capture_is_independent_of_ui_ten_second_window(self):
        monitor = make_monitor()
        monitor.autotune_capture_active = True
        monitor.autotune_capture_phase = 'motion:test'
        monitor.autotune_max_error_deg = 100.0

        for eventtime in range(13):
            monitor.last['error_deg'] = float(eventtime)
            monitor.last['error_counts'] = eventtime
            monitor._append_error_sample(float(eventtime))

        self.assertEqual(len(monitor.autotune_capture_samples), 13)
        self.assertTrue(all(sample['time'] >= 2.0
                            for sample in monitor.error_history))
        self.assertTrue(all(sample['phase'] == 'motion:test'
                            for sample in monitor.autotune_capture_samples))

    def test_corexy_max_error_marks_candidate_for_rejection(self):
        monitor = make_monitor()
        monitor.autotune_capture_active = True
        monitor.autotune_capture_phase = 'motion:test'
        monitor.autotune_max_error_deg = 1.0
        monitor.last['error_deg'] = 1.5
        monitor.last['error_counts'] = 10

        monitor._append_error_sample(1.0)

        with self.assertRaises(ZDT.AutotuneCandidateRejected):
            monitor._check_autotune_runtime_safety()

    def test_corexy_baseline_safety_rejection_is_command_error(self):
        monitor = make_monitor()
        monitor.reactor = FakeReactor()
        monitor.printer = type(
            'Printer', (), {'command_error': FakeCommandError})()
        monitor.enabled = False
        monitor.timer = object()
        monitor.error_timer = object()
        monitor.autotune_active = False
        monitor.autotune_settle_time = 0.5
        monitor.autotune_kp_step = 5000
        monitor.autotune_ki_step = 20
        monitor.autotune_kd_step = 5000
        toolhead = type('Toolhead', (), {
            'max_accel': 10000.0,
            'get_status': lambda self, eventtime: {'homed_axes': 'xyz'},
        })()
        original = (26000, 10, 26000)
        restored = []

        def set_polling(enabled):
            monitor.enabled = enabled

        monitor._set_polling_enabled = set_polling
        monitor._check_autotune_preconditions = (
            lambda gcmd, axis: (toolhead, original))
        monitor._check_corexy_workspace = lambda *_args: None
        monitor._corexy_pid_bounds = lambda *_args: [
            (13000, 52000), (0, 210), (13000, 52000)]
        monitor._evaluate_corexy_profile = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(ZDT.AutotuneCandidateRejected(
                'position error 5.289917 deg exceeded MAX_ERROR_DEG 5.000000')))
        monitor._write_pid = lambda pid, store=0, verify=True: (
            restored.append((tuple(pid), store)) or True)
        gcmd = FakeGcmd({'CONFIRM': 1})

        with self.assertRaisesRegex(
                FakeCommandError, 'COREXY_PRINT baseline rejected'):
            monitor._cmd_AUTOTUNE_COREXY(gcmd)

        self.assertIn((original, 0), restored)
        self.assertFalse(monitor.autotune_active)

    def test_bounded_pid_candidate_stays_inside_parameter_range(self):
        monitor = make_monitor()
        bounds = [(20000, 30000), (0, 200), (20000, 30000)]

        high = monitor._bounded_pid_candidate(
            (29000, 100, 26000), 0, 5000, bounds)
        low = monitor._bounded_pid_candidate(
            (21000, 100, 26000), 0, -5000, bounds)

        self.assertEqual(high, (30000, 100, 26000))
        self.assertEqual(low, (20000, 100, 26000))

    def test_long_packet_normalization_drops_address_only_once(self):
        monitor = make_monitor()

        first = monitor._normalize_long_packet(
            bytes([0x01, ZDT.CMD_READ_PID, 0x00, 0x00]),
            ZDT.CMD_READ_PID)
        continuation = monitor._normalize_long_packet(
            bytes([ZDT.CMD_READ_PID, 0xF2, 0x30]), ZDT.CMD_READ_PID)

        self.assertEqual(first, bytearray([ZDT.CMD_READ_PID, 0x00, 0x00]))
        self.assertEqual(continuation, bytearray([ZDT.CMD_READ_PID, 0xF2, 0x30]))

    def test_position_error_poll_is_independent_from_general_poll(self):
        monitor = make_monitor()
        monitor.enabled = True
        monitor.poll_interval = 0.10
        sent = []
        monitor._send_command = lambda cmd: sent.append(cmd)

        monitor._poll_timer(0.0)
        monitor._error_poll_timer(0.0)

        self.assertEqual(sent, [ZDT.CMD_VOLTAGE, ZDT.CMD_POS_ERROR])
        self.assertEqual(monitor.pending_cmd, ZDT.CMD_VOLTAGE)
        self.assertEqual(monitor.pending_error_cmd, ZDT.CMD_POS_ERROR)

    def test_signed_position_error_and_angle_conversion(self):
        monitor = make_monitor()

        negative = bytearray([1, ZDT.CMD_POS_ERROR, 1, 0, 0, 0, 8, 0x6B])
        positive = bytearray([1, ZDT.CMD_POS_ERROR, 0, 0, 0, 0, 8, 0x6B])

        self.assertTrue(monitor._verify_checksum(negative))
        self.assertTrue(monitor._record_valid_response(ZDT.CMD_POS_ERROR, negative, 10.0))
        self.assertAlmostEqual(monitor.last['error_deg'], -0.0439453125)
        self.assertAlmostEqual(monitor.last['error_mm'], -0.0048828125)
        self.assertEqual(monitor.last['error_counts'], -8)
        self.assertAlmostEqual(monitor.error_history[-1]['error_mm'], -0.0048828125)

        self.assertTrue(monitor._record_valid_response(ZDT.CMD_POS_ERROR, positive, 10.1))
        self.assertAlmostEqual(monitor.last['error_deg'], 0.0439453125)
        self.assertEqual(monitor.last['error_counts'], 8)

    def test_invalid_checksum_does_not_create_history_point(self):
        monitor = make_monitor()
        monitor.pending_error_cmd = ZDT.CMD_POS_ERROR
        invalid = bytes([ZDT.CMD_POS_ERROR, 1, 0, 0, 0, 8, 0x00])

        monitor._process_frame(0x0100, invalid, 10.0)

        self.assertEqual(len(monitor.error_history), 0)
        self.assertFalse(monitor.last['online'])
        self.assertEqual(monitor.error_count, 1)
        self.assertIsNone(monitor.pending_error_cmd)

    def test_timeout_and_device_error_do_not_create_history_point(self):
        monitor = make_monitor()
        monitor.pending_error_cmd = ZDT.CMD_POS_ERROR

        monitor._register_no_response(ZDT.CMD_POS_ERROR)

        self.assertEqual(len(monitor.error_history), 0)
        self.assertFalse(monitor.last['online'])
        self.assertEqual(monitor.error_count, 1)

        monitor.pending_error_cmd = ZDT.CMD_POS_ERROR
        monitor._process_frame(0x0100, bytes([0x00, 0xEE, 0x6B]), 10.0)

        self.assertEqual(len(monitor.error_history), 0)
        self.assertFalse(monitor.last['online'])
        self.assertEqual(monitor.error_count, 2)
        self.assertIsNone(monitor.pending_error_cmd)

    def test_status_online_requires_recent_valid_sample(self):
        monitor = make_monitor()
        monitor.enabled = True
        raw = bytearray([1, ZDT.CMD_POS_ERROR, 0, 0, 0, 0, 8, 0x6B])

        monitor._record_valid_response(ZDT.CMD_POS_ERROR, raw, 10.0)
        self.assertTrue(monitor.get_status(10.5)['online'])
        self.assertFalse(monitor.get_status(11.1)['online'])
        self.assertEqual(len(monitor.get_status(20.001)['error_history']), 0)

        monitor._register_no_response(ZDT.CMD_POS_ERROR)
        self.assertFalse(monitor.get_status(10.5)['online'])

    def test_history_is_limited_to_ten_seconds(self):
        monitor = make_monitor()

        for eventtime in range(13):
            raw = bytearray([1, ZDT.CMD_POS_ERROR, 0, 0, 0, 0, eventtime, 0x6B])
            self.assertTrue(monitor._record_valid_response(ZDT.CMD_POS_ERROR, raw, float(eventtime)))

        self.assertTrue(all(sample['time'] >= 2.0 for sample in monitor.error_history))
        self.assertLessEqual(
            max(sample['time'] for sample in monitor.error_history) -
            min(sample['time'] for sample in monitor.error_history),
            10.0,
        )
        self.assertEqual(len(monitor.last['error_history']), len(monitor.error_history))


if __name__ == '__main__':
    unittest.main()
