import importlib.util
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


def make_monitor():
    monitor = ZDT.ZdtEmm42.__new__(ZDT.ZdtEmm42)
    monitor.name = 'test'
    monitor.addr = 1
    monitor.check_byte = 0x6B
    monitor.checksum_mode = 'fixed'
    monitor.can_payload_includes_addr = False
    monitor.error_poll_interval = 0.10
    monitor.query_timeout = 0.006
    monitor.offline_timeout = 1.0
    monitor.rotation_distance = 40.0
    monitor.microsteps = 16
    monitor.full_steps_per_rotation = 200
    monitor.error_history = deque(maxlen=52)
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
        self.assertEqual(monitor.last['error_counts'], -8)

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
        self.assertEqual(len(monitor.get_status(20.0)['error_history']), 0)

        monitor._register_no_response(ZDT.CMD_POS_ERROR)
        self.assertFalse(monitor.get_status(10.5)['online'])

    def test_history_is_limited_to_five_seconds(self):
        monitor = make_monitor()

        for eventtime in range(8):
            raw = bytearray([1, ZDT.CMD_POS_ERROR, 0, 0, 0, 0, eventtime, 0x6B])
            self.assertTrue(monitor._record_valid_response(ZDT.CMD_POS_ERROR, raw, float(eventtime)))

        self.assertTrue(all(sample['time'] >= 2.0 for sample in monitor.error_history))
        self.assertLessEqual(
            max(sample['time'] for sample in monitor.error_history) -
            min(sample['time'] for sample in monitor.error_history),
            5.0,
        )
        self.assertEqual(len(monitor.last['error_history']), len(monitor.error_history))


if __name__ == '__main__':
    unittest.main()
