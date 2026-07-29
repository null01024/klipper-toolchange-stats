import importlib
import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock


EXTRAS_PATH = Path(__file__).resolve().parents[1] / 'klipper' / 'extras'
sys.path.insert(0, str(EXTRAS_PATH))
CORE = importlib.import_module('closed_loop_motor_core')
TRANSPORT = importlib.import_module('closed_loop_motor_transport')
ZDT_ADAPTER = importlib.import_module(
    'closed_loop_motor_zdt_emm42_v5_can')
MOTOR_LOADER = importlib.import_module('closed_loop_motor')
GROUP_LOADER = importlib.import_module('closed_loop_motor_group')


class FakeReactor:
    NOW = 0.0
    NEVER = 1.0e30

    def __init__(self):
        self.registered = []
        self.unregistered = []

    def register_fd(self, fd, callback):
        handle = (fd, callback)
        self.registered.append(handle)
        return handle

    def unregister_fd(self, handle):
        self.unregistered.append(handle)


class FakeGcode:
    def __init__(self):
        self.commands = []

    def register_mux_command(self, command, key, value, callback, **kwargs):
        self.commands.append(command)


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.objects = {'gcode': self.gcode}
        self.handlers = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=...):
        if name in self.objects:
            return self.objects[name]
        if default is not ...:
            return default
        raise KeyError(name)

    def add_object(self, name, value):
        self.objects[name] = value

    def register_event_handler(self, event, callback):
        self.handlers.append((event, callback))


class FakeConfig:
    def __init__(self, printer, name='closed_loop_motor axis_x', values=None):
        self.printer = printer
        self.name = name
        self.values = values or {}

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getint(self, key, default=None, **kwargs):
        return int(self.values.get(key, default))

    def getfloat(self, key, default=None, **kwargs):
        return float(self.values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self.values.get(key, default))

    def error(self, message):
        return ValueError(message)


class FakeSocket:
    def __init__(self):
        self.frames = []
        self.sent = []
        self.bound = None
        self.options = []
        self.closed = False

    def setblocking(self, value):
        pass

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        self.bound = address

    def fileno(self):
        return 42

    def send(self, frame):
        self.sent.append(frame)

    def recv(self, size):
        if not self.frames:
            raise BlockingIOError
        value = self.frames.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class ClosedLoopMotorArchitectureTest(unittest.TestCase):
    def test_canonical_zdt_can_config_registers_only_generic_commands(self):
        printer = FakePrinter()
        config = FakeConfig(printer, values={
            'vendor': 'zdt',
            'model': 'emm42_v5',
            'transport': 'can',
            'interface': 'can2',
            'address': 7,
        })

        controller = CORE.create_motor(config)

        self.assertIsInstance(controller, ZDT_ADAPTER.ZdtEmm42V5Can)
        self.assertEqual(
            controller.closed_loop_identity(),
            ('zdt', 'emm42_v5', 'can'))
        self.assertEqual(controller.transport_address(), ('can', 'can2', 7))
        self.assertTrue(printer.gcode.commands)
        self.assertTrue(all(
            command.startswith('CLOSED_LOOP_MOTOR_')
            for command in printer.gcode.commands))
        self.assertNotIn('ZDT_EMM_STATUS', printer.gcode.commands)
        status = controller._empty_status()
        self.assertEqual(status['schema_version'], 1)
        self.assertEqual(status['object_type'], 'closed_loop_motor')
        self.assertEqual(status['interface'], 'can2')
        self.assertEqual(status['address'], 7)
        self.assertNotIn('can_interface', status)
        self.assertNotIn('addr', status)

    def test_unsupported_catalog_entry_fails_before_adapter_creation(self):
        config = FakeConfig(FakePrinter(), values={
            'vendor': 'leadshine',
            'model': 'closed_loop_stepper',
            'transport': 'rs485',
        })

        with self.assertRaisesRegex(ValueError, 'unsupported'):
            CORE.create_motor(config)

    def test_legacy_config_entrypoints_are_removed(self):
        retired = (
            'zdt_emm42.py',
            'zdt_emm42_group.py',
            'zdt_emm42_xy_group.py',
        )

        self.assertTrue(all(
            not (EXTRAS_PATH / filename).exists()
            for filename in retired))
        self.assertFalse(hasattr(CORE, 'register_legacy_motor'))
        self.assertFalse(hasattr(CORE, 'register_legacy_group'))
        self.assertFalse(hasattr(CORE, 'group_object_name'))

    def test_socketcan_endpoint_is_shared_and_routes_by_address(self):
        printer = FakePrinter()
        fake_socket = FakeSocket()
        owner_a, owner_b = object(), object()
        received_a, received_b = [], []
        manager = TRANSPORT.get_transport_manager(printer)
        endpoint = manager.socketcan('can0')

        self.assertIs(endpoint, manager.socketcan('can0'))
        endpoint.register(
            owner_a, 1,
            lambda frame_id, payload, eventtime:
                received_a.append((frame_id, payload, eventtime)))
        endpoint.register(
            owner_b, 2,
            lambda frame_id, payload, eventtime:
                received_b.append((frame_id, payload, eventtime)))

        with mock.patch.object(socket, 'socket', return_value=fake_socket):
            endpoint.acquire(owner_a)
            endpoint.acquire(owner_b)
            fake_socket.frames.extend([
                struct.pack(
                    TRANSPORT.CAN_FRAME_FMT,
                    TRANSPORT.CAN_EFF_FLAG | 0x0100, 2,
                    b'\x24\x6b'.ljust(8, b'\x00')),
                struct.pack(
                    TRANSPORT.CAN_FRAME_FMT,
                    TRANSPORT.CAN_EFF_FLAG | 0x0200, 2,
                    b'\x27\x6b'.ljust(8, b'\x00')),
            ])
            endpoint._handle_rx(12.5)

        self.assertEqual(received_a, [(0x0100, b'\x24\x6b', 12.5)])
        self.assertEqual(received_b, [(0x0200, b'\x27\x6b', 12.5)])
        self.assertEqual(len(printer.reactor.registered), 1)
        self.assertEqual(
            fake_socket.options[-1],
            (socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
             struct.pack('=II', TRANSPORT.CAN_EFF_FLAG,
                         TRANSPORT.CAN_EFF_FLAG)))

    def test_shared_endpoint_observers_receive_standard_frames(self):
        printer = FakePrinter()
        fake_socket = FakeSocket()
        owner = object()
        observed = []
        endpoint = TRANSPORT.SocketCanEndpoint(printer, 'can0')
        endpoint.register(owner, 1, lambda *args: None)
        observer = lambda extended, frame_id, payload, eventtime: (
            observed.append((extended, frame_id, payload)))

        with mock.patch.object(socket, 'socket', return_value=fake_socket):
            endpoint.acquire(owner)
            endpoint.add_observer(observer, include_standard=True)
            fake_socket.frames.append(struct.pack(
                TRANSPORT.CAN_FRAME_FMT, 0x321, 1,
                b'\xaa'.ljust(8, b'\x00')))
            endpoint._handle_rx(1.0)
            endpoint.remove_observer(observer)

        self.assertEqual(observed, [(False, 0x321, b'\xaa')])
        self.assertEqual(endpoint.standard_frames, 1)
        self.assertEqual(
            fake_socket.options[-2][2], struct.pack('=II', 0, 0))
        self.assertEqual(
            fake_socket.options[-1][2],
            struct.pack('=II', TRANSPORT.CAN_EFF_FLAG,
                        TRANSPORT.CAN_EFF_FLAG))

    def test_socketcan_rx_callback_yields_after_a_bounded_batch(self):
        printer = FakePrinter()
        fake_socket = FakeSocket()
        owner = object()
        endpoint = TRANSPORT.SocketCanEndpoint(printer, 'can0')
        endpoint.register(owner, 1, lambda *args: None)
        frame = struct.pack(
            TRANSPORT.CAN_FRAME_FMT,
            TRANSPORT.CAN_EFF_FLAG | 0x0100, 1,
            b'\x24'.ljust(8, b'\x00'))

        with mock.patch.object(socket, 'socket', return_value=fake_socket):
            endpoint.acquire(owner)
            fake_socket.frames.extend(
                [frame] * (TRANSPORT.MAX_RX_FRAMES_PER_CALLBACK + 1))
            endpoint._handle_rx(1.0)

        self.assertEqual(len(fake_socket.frames), 1)
        self.assertEqual(endpoint.rx_budget_yields, 1)

    def test_socketcan_open_failure_is_transactional(self):
        printer = FakePrinter()
        printer.reactor.register_fd = mock.Mock(
            side_effect=RuntimeError('register failed'))
        fake_socket = FakeSocket()
        endpoint = TRANSPORT.SocketCanEndpoint(printer, 'can0')

        with mock.patch.object(socket, 'socket', return_value=fake_socket):
            with self.assertRaisesRegex(RuntimeError, 'register failed'):
                endpoint.open()

        self.assertIsNone(endpoint.sock)
        self.assertIsNone(endpoint.fd_handle)
        self.assertTrue(fake_socket.closed)

    def test_socketcan_receive_error_closes_endpoint_for_reconnect(self):
        printer = FakePrinter()
        fake_socket = FakeSocket()
        endpoint = TRANSPORT.SocketCanEndpoint(printer, 'can0')
        owner = object()
        endpoint.register(owner, 1, lambda *args: None)

        with mock.patch.object(socket, 'socket', return_value=fake_socket):
            endpoint.acquire(owner)
            fake_socket.frames.append(OSError('interface failed'))
            endpoint._handle_rx(1.0)

        self.assertIsNone(endpoint.sock)
        self.assertIsNone(endpoint.fd_handle)
        self.assertTrue(fake_socket.closed)
        self.assertIn('interface failed', endpoint.last_error)

    def test_adapter_sniff_filter_is_applied_in_software(self):
        controller = CORE.create_motor(FakeConfig(FakePrinter(), values={
            'vendor': 'zdt',
            'model': 'emm42_v5',
            'transport': 'can',
            'address': 7,
            'can_filter': 'addr',
        }))

        self.assertTrue(controller._sniff_accepts_frame(True, 0x0701))
        self.assertFalse(controller._sniff_accepts_frame(True, 0x0801))
        self.assertFalse(controller._sniff_accepts_frame(False, 0x701))
        controller.can_filter = 'off'
        self.assertTrue(controller._sniff_accepts_frame(False, 0x701))

    def test_transport_diagnostics_keep_device_and_endpoint_errors(self):
        controller = CORE.create_motor(FakeConfig(FakePrinter(), values={
            'vendor': 'zdt',
            'model': 'emm42_v5',
            'transport': 'can',
        }))
        controller.last_error = 'device timeout'
        controller.transport_endpoint.last_error = 'interface down'

        diagnostics = controller.get_status(1.0)['transport_diagnostics']

        self.assertEqual(diagnostics['last_error'], 'device timeout')
        self.assertEqual(
            diagnostics['endpoint']['last_error'], 'interface down')

    def test_group_member_contract_is_adapter_agnostic(self):
        controller = CORE.create_motor(FakeConfig(FakePrinter(), values={
            'vendor': 'zdt',
            'model': 'emm42_v5',
            'transport': 'can',
        }))

        CORE.validate_group_member(controller)
        with self.assertRaisesRegex(ValueError, 'group capabilities'):
            CORE.validate_group_member(object())

    def test_position_error_sample_requires_degrees_but_not_millimeters(self):
        sample = CORE.normalize_position_error_sample({
            'time': 1.25, 'error_deg': -0.5,
        })

        self.assertEqual(sample['time'], 1.25)
        self.assertEqual(sample['error_deg'], -0.5)
        self.assertIsNone(sample['error_mm'])
        self.assertIsNone(CORE.normalize_position_error_sample({
            'time': 1.25,
        }))

    def test_canonical_loaders_require_an_instance_name(self):
        config = FakeConfig(FakePrinter(), name='closed_loop_motor')
        group_config = FakeConfig(
            FakePrinter(), name='closed_loop_motor_group')

        with self.assertRaisesRegex(ValueError, 'instance name'):
            MOTOR_LOADER.load_config(config)
        with self.assertRaisesRegex(ValueError, 'instance name'):
            GROUP_LOADER.load_config(group_config)

    def test_socketcan_endpoint_rejects_duplicate_address(self):
        endpoint = TRANSPORT.SocketCanEndpoint(FakePrinter(), 'can0')
        endpoint.register(object(), 9, lambda *args: None)

        with self.assertRaises(TRANSPORT.TransportError):
            endpoint.register(object(), 9, lambda *args: None)


if __name__ == '__main__':
    unittest.main()
