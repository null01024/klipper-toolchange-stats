import importlib.util
import struct
import unittest
from collections import deque
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'klipper' / 'extras' / 'zdt_emm42.py'
SPEC = importlib.util.spec_from_file_location('zdt_emm42_under_test', MODULE_PATH)
ZDT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ZDT)


class CaptureSocket:
    def __init__(self):
        self.frames = []

    def send(self, frame):
        self.frames.append(frame)


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
