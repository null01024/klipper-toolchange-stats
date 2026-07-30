# Generic transport primitives for closed-loop motor plugins.

import logging
import socket
import struct


CAN_EFF_FLAG = 0x80000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CAN_FRAME_FMT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)
MAX_RX_FRAMES_PER_CALLBACK = 256

MANAGER_OBJECT_NAME = '_closed_loop_motor_transport_manager'


class TransportError(Exception):
    pass


class TransportEndpoint:
    """Physical transport contract. Endpoints never parse vendor commands."""

    transport_type = 'unknown'
    topology = 'point_to_point'

    @property
    def identity(self):
        raise NotImplementedError

    def acquire(self, owner):
        raise NotImplementedError

    def release(self, owner):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def diagnostics(self):
        raise NotImplementedError


class SerialTransportEndpoint(TransportEndpoint):
    """Reserved contract for shared USB-RS485/RS232 endpoints.

    Actual serial I/O is intentionally not implemented until a supported
    device protocol supplies framing, timing, and topology requirements.
    """

    def __init__(self, spec):
        self.spec = spec
        self.owners = set()
        self.subscribers = {}
        self.last_error = 'serial transport is reserved but not implemented'

    @property
    def transport_type(self):
        return self.spec.transport

    @property
    def topology(self):
        return self.spec.topology

    @property
    def identity(self):
        return self.spec.endpoint_key

    @property
    def signature(self):
        return self.spec.signature

    def register(self, owner, address, link_profile, callback,
                 supports_daisy_chain=False):
        address = int(address)
        if address < 0:
            raise TransportError('serial device address must not be negative')
        if (self.spec.transport == 'rs232' and
                self.spec.topology == 'daisy_chain' and
                not supports_daisy_chain):
            raise TransportError(
                'RS232 daisy-chain use requires explicit device support')
        if (self.spec.transport == 'rs232' and
                self.spec.topology == 'point_to_point' and
                self.subscribers and
                all(value[0] is not owner
                    for value in self.subscribers.values())):
            raise TransportError(
                'point-to-point RS232 endpoint already has a device')
        current = self.subscribers.get(address)
        if current is not None and current[0] is not owner:
            raise TransportError(
                "serial endpoint %s already has a device at address %d" %
                (self.spec.interface, address))
        self.subscribers[address] = (
            owner, str(link_profile), callback,
            bool(supports_daisy_chain))

    def unregister(self, owner):
        for address, value in list(self.subscribers.items()):
            if value[0] is owner:
                del self.subscribers[address]
        self.owners.discard(owner)

    def acquire(self, owner):
        self.owners.add(owner)

    def release(self, owner):
        self.owners.discard(owner)

    def write(self, owner, payload):
        raise TransportError(self.last_error)

    def close(self):
        pass

    def diagnostics(self):
        return {
            'transport': self.spec.transport,
            'interface': self.spec.interface,
            'topology': self.spec.topology,
            'baud': self.spec.baud,
            'data_bits': self.spec.data_bits,
            'parity': self.spec.parity,
            'stop_bits': self.spec.stop_bits,
            'implemented': False,
            'last_error': self.last_error,
        }


class SocketCanEndpoint(TransportEndpoint):
    """One reactor-owned SocketCAN connection shared by addressed devices."""

    transport_type = 'can'
    topology = 'multidrop'

    def __init__(self, printer, interface):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.interface = interface
        self.sock = None
        self.fd_handle = None
        self.owners = set()
        self.subscribers = {}
        self.observers = {}
        self.tx_frames = 0
        self.rx_frames = 0
        self.error_frames = 0
        self.standard_frames = 0
        self.rx_budget_yields = 0
        self.last_error = ''

    @property
    def identity(self):
        return ('can', self.interface)

    def register(self, owner, address, callback):
        address = int(address)
        current = self.subscribers.get(address)
        if current is not None and current[0] is not owner:
            raise TransportError(
                "CAN endpoint %s already has a device at address %d" %
                (self.interface, address))
        self.subscribers[address] = (owner, callback)

    def unregister(self, owner):
        for address, value in list(self.subscribers.items()):
            if value[0] is owner:
                del self.subscribers[address]
        self.owners.discard(owner)
        if not self.owners:
            self.close()

    def acquire(self, owner):
        self.owners.add(owner)
        self.open()

    def release(self, owner):
        self.owners.discard(owner)
        if not self.owners:
            self.close()

    def open(self):
        if self.sock is not None:
            return
        sock = None
        fd_handle = None
        try:
            sock = socket.socket(
                socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sock.setblocking(False)
            self._apply_kernel_filter(sock)
            sock.bind((self.interface,))
            fd_handle = self.reactor.register_fd(
                sock.fileno(), self._handle_rx)
        except Exception as exc:
            self.last_error = str(exc)
            if fd_handle is not None:
                try:
                    self.reactor.unregister_fd(fd_handle)
                except Exception:
                    pass
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise
        self.sock = sock
        self.fd_handle = fd_handle
        self.last_error = ''

    def _apply_kernel_filter(self, sock=None):
        target = sock if sock is not None else self.sock
        if target is None:
            return
        include_standard = any(self.observers.values())
        can_id = 0 if include_standard else CAN_EFF_FLAG
        can_mask = 0 if include_standard else CAN_EFF_FLAG
        target.setsockopt(
            socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
            struct.pack('=II', can_id, can_mask))

    def close(self):
        if self.fd_handle is not None:
            try:
                self.reactor.unregister_fd(self.fd_handle)
            except Exception:
                pass
            self.fd_handle = None
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def send(self, address, packet_no, payload):
        if len(payload) > 8:
            raise TransportError('SocketCAN payload must not exceed 8 bytes')
        self.open()
        arb_id = (((int(address) & 0xFF) << 8) |
                  (int(packet_no) & 0xFF) | CAN_EFF_FLAG)
        frame = struct.pack(
            CAN_FRAME_FMT, arb_id, len(payload), payload.ljust(8, b'\x00'))
        try:
            self.sock.send(frame)
            self.tx_frames += 1
        except Exception as exc:
            self.last_error = str(exc)
            self.close()
            raise
        return arb_id & CAN_EFF_MASK

    def add_observer(self, callback, include_standard=False):
        self.observers[callback] = bool(include_standard)
        try:
            self._apply_kernel_filter()
        except Exception as exc:
            self.observers.pop(callback, None)
            self.last_error = str(exc)
            self.close()
            raise

    def remove_observer(self, callback):
        self.observers.pop(callback, None)
        try:
            self._apply_kernel_filter()
        except Exception as exc:
            self.last_error = str(exc)
            self.close()

    def _notify_observers(self, extended, frame_id, payload, eventtime):
        for callback in list(self.observers):
            try:
                callback(extended, frame_id, payload, eventtime)
            except Exception:
                logging.exception(
                    'closed_loop_motor: SocketCAN observer failed')

    def _handle_rx(self, eventtime):
        try:
            for _ in range(MAX_RX_FRAMES_PER_CALLBACK):
                try:
                    frame = self.sock.recv(CAN_FRAME_SIZE)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError as exc:
                    self.last_error = str(exc)
                    self.close()
                    return
                if len(frame) < CAN_FRAME_SIZE:
                    continue
                can_id, dlc, data = struct.unpack(CAN_FRAME_FMT, frame)
                payload = bytes(data[:dlc])
                extended = bool(can_id & CAN_EFF_FLAG)
                frame_id = can_id & (
                    CAN_EFF_MASK if extended else CAN_SFF_MASK)
                self.rx_frames += 1
                self._notify_observers(
                    extended, frame_id, payload, eventtime)
                if can_id & CAN_ERR_FLAG:
                    self.error_frames += 1
                    continue
                if not extended:
                    self.standard_frames += 1
                    continue
                address = (frame_id >> 8) & 0xFF
                subscriber = self.subscribers.get(address)
                if subscriber is None:
                    continue
                subscriber[1](frame_id, payload, eventtime)
            self.rx_budget_yields += 1
        except Exception:
            logging.exception(
                'closed_loop_motor: SocketCAN receive handler failed')

    def diagnostics(self):
        return {
            'transport': self.transport_type,
            'interface': self.interface,
            'tx_frames': self.tx_frames,
            'rx_frames': self.rx_frames,
            'error_frames': self.error_frames,
            'standard_frames': self.standard_frames,
            'rx_budget_yields': self.rx_budget_yields,
            'last_error': self.last_error,
        }


class ClosedLoopTransportManager:
    def __init__(self, printer):
        self.printer = printer
        self.endpoints = {}
        printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect)

    def socketcan(self, interface):
        key = ('can', str(interface))
        endpoint = self.endpoints.get(key)
        if endpoint is None:
            endpoint = self.endpoints[key] = SocketCanEndpoint(
                self.printer, str(interface))
        return endpoint

    def serial(self, spec):
        key = spec.endpoint_key
        endpoint = self.endpoints.get(key)
        if endpoint is None:
            endpoint = self.endpoints[key] = SerialTransportEndpoint(spec)
            return endpoint
        if not isinstance(endpoint, SerialTransportEndpoint):
            raise TransportError(
                'transport endpoint identity is already in use')
        if endpoint.signature != spec.signature:
            raise TransportError(
                '%s endpoint %s has conflicting serial settings' %
                (spec.transport.upper(), spec.interface))
        return endpoint

    def _handle_disconnect(self):
        for endpoint in self.endpoints.values():
            endpoint.close()


def get_transport_manager(printer):
    manager = printer.lookup_object(MANAGER_OBJECT_NAME, None)
    if manager is None:
        manager = ClosedLoopTransportManager(printer)
        printer.add_object(MANAGER_OBJECT_NAME, manager)
    return manager
