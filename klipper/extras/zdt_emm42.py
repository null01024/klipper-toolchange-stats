# zdt_emm42.py
# Klipper extra module for monitoring ZHANGDATOU/ZDT Emm42_V5.0 closed-loop stepper
# through its custom CAN protocol while motion is still driven by STEP/DIR/EN.
#
# Install:
#   cp zdt_emm42.py ~/klipper/klippy/extras/zdt_emm42.py
#   sudo systemctl restart klipper
#
# Example printer.cfg:
#   [zdt_emm42 shadow_a]
#   can_interface: can0
#   addr: 1
#   can_payload_includes_addr: False
#   can_filter: ext            # off | ext (default) | addr
#   checksum_mode: 0x6B        # 0x6B (default) | xor | crc8 (crc8 unverified)
#   poll_interval: 0.10
#   error_poll_interval: 0.05  # independent 0x37 position-error sample period
#   query_timeout: 0.006
#   rotation_distance: 40
#   microsteps: 16             # MUST match the driver's MStep setting (driver default is 16)
#   full_steps_per_rotation: 200
#   csv_path: /tmp/zdt_emm42_shadow_a.csv
#   autotune_settle_time: 0.50
#   autotune_kp_step: 5000
#   autotune_ki_step: 20
#   autotune_kd_step: 5000
#   pid_write_settle_time: 0.05
#
# G-code:
#   ZDT_EMM_STATUS NAME=shadow_a
#   ZDT_EMM_QUERY  NAME=shadow_a CMD=0x36
#   ZDT_EMM_QUERY  NAME=shadow_a CMD=0x42 DATA=6C   ; DATA appends extra request bytes
#   ZDT_EMM_SNIFF  NAME=shadow_a SECONDS=2          ; capture raw CAN frames for debugging
#   ZDT_EMM_LOG    NAME=shadow_a ENABLE=1
#   ZDT_EMM_LOG    NAME=shadow_a ENABLE=0
#   ZDT_EMM_POLL   NAME=shadow_a ENABLE=1
#   ZDT_EMM_AUTOTUNE NAME=shadow_a AXIS=X DISTANCE=10 SPEED=20 ACCEL=200 ITERATIONS=20 CONFIRM=1
#   ZDT_EMM_AUTOTUNE NAME=shadow_a PROFILE=COREXY_PRINT DISTANCE=100 SPEED=200 ACCEL=5000 ITERATIONS=20 REPEATS=3 MAX_ERROR_DEG=6.5 CONFIRM=1
#   ZDT_EMM_AUTOTUNE_CANCEL NAME=shadow_a
#
# Notes:
# - Emm42 must be set to CAN1_MAP, extended CAN frame, and the same bus bitrate as can0.
# - This module uses Linux SocketCAN directly, not Klipper's MCU CAN protocol.
# - Reception is asynchronous via the Klipper reactor (register_fd); the poll timer only
#   sends one short read command per tick and never blocks waiting for the reply.
# - The normal monitor only issues read commands.  ZDT_EMM_AUTOTUNE is the explicit
#   opt-in command that temporarily writes the position-loop PID while exercising
#   one user-selected Klipper axis.

import csv
from collections import deque
import logging
import math
import os
import select
import socket
import struct
import time

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CAN_FRAME_FMT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)

# Common ZDT read commands used here.
CMD_READ_PID = 0x21          # response is >8 bytes and uses CAN long-response framing
CMD_WRITE_PID = 0x4A         # long request, short response
CMD_VOLTAGE = 0x24           # addr 24 hi lo 6B
CMD_CURRENT = 0x27           # addr 27 hi lo 6B
CMD_ENCODER = 0x31           # addr 31 hi lo 6B
CMD_INPUT_PULSES = 0x32      # addr 32 sign u32 6B
CMD_TARGET_POS = 0x33        # addr 33 sign u32 6B
CMD_REALTIME_TARGET = 0x34   # addr 34 sign u32 6B (realtime setpoint / open-loop realtime pos)
CMD_RPM = 0x35               # addr 35 sign u16 6B
CMD_REAL_POS = 0x36          # addr 36 sign u32 6B
CMD_POS_ERROR = 0x37         # addr 37 sign u32 6B
CMD_MOTOR_FLAGS = 0x3A       # addr 3A flags 6B
CMD_HOME_FLAGS = 0x3B        # addr 3B flags 6B

ERROR_HISTORY_SECONDS = 10.0
AUTOTUNE_SAMPLE_INTERVAL = 0.02
AUTOTUNE_MAX_SAMPLES = 100000
PID_RESPONSE_LEN = 15        # address + command + KP[4] + KI[4] + KD[4] + check
PID_WRITE_SUBCOMMAND = 0xC3
PID_MIN_VALUE = 0
PID_MAX_VALUE = 0xFFFFFFFF

READ_COMMANDS = [
    CMD_VOLTAGE,
    CMD_CURRENT,
    CMD_ENCODER,
    CMD_INPUT_PULSES,
    CMD_TARGET_POS,
    CMD_REALTIME_TARGET,
    CMD_RPM,
    CMD_REAL_POS,
    CMD_POS_ERROR,
    CMD_MOTOR_FLAGS,
    CMD_HOME_FLAGS,
]

# Position error is sampled by its own timer.  Keep it in READ_COMMANDS so the
# diagnostic sniffer still exercises the complete set of read commands, but do
# not let the slower general telemetry round-robin determine its sample rate.
GENERAL_READ_COMMANDS = [cmd for cmd in READ_COMMANDS if cmd != CMD_POS_ERROR]


class AutotuneCandidateRejected(Exception):
    """A PID candidate violated a recoverable tuning constraint."""


def _u16(data, index):
    return (data[index] << 8) | data[index + 1]


def _u32(data, index):
    return ((data[index] << 24) | (data[index + 1] << 16) |
            (data[index + 2] << 8) | data[index + 3])


def _pack_u32(value):
    value = int(value)
    if value < PID_MIN_VALUE or value > PID_MAX_VALUE:
        raise ValueError('PID value must be in the range 0..0xFFFFFFFF')
    return struct.pack('>I', value)


def _signed_value(sign_byte, raw):
    # ZDT convention: 0x01 = negative, 0x00 = positive.
    return -raw if sign_byte == 0x01 else raw


def _parse_int(value):
    # Klipper gcmd values arrive as strings. ZDT command bytes are normally
    # written as hex, so accept 0xNN and bare two-digit forms like "36".
    if isinstance(value, int):
        return value
    value = str(value).strip()
    if value.lower().startswith("0x"):
        return int(value, 16)
    if len(value) <= 2:
        return int(value, 16)
    try:
        return int(value, 10)
    except ValueError:
        return int(value, 16)


def _parse_hex_bytes(value):
    # Parse an optional data field like "6C", "6C 00", "0x6C,0x00" into bytes.
    if value is None:
        return b''
    text = str(value).strip()
    if not text:
        return b''
    text = text.replace(',', ' ')
    out = bytearray()
    for tok in text.split():
        if tok.lower().startswith('0x'):
            tok = tok[2:]
        if not tok:
            continue
        if len(tok) > 2 and len(tok) % 2 == 0:
            for i in range(0, len(tok), 2):
                out.append(int(tok[i:i + 2], 16))
        else:
            out.append(int(tok, 16))
    return bytes(out)


class ZdtEmm42:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        section = config.get_name().split(None, 1)
        self.name = section[1] if len(section) > 1 else "default"
        self.can_interface = config.get('can_interface', 'can0')
        self.addr = config.getint('addr', 1, minval=1, maxval=255)
        self.check_byte = config.getint('check_byte', 0x6B, minval=0, maxval=255)
        self.can_payload_includes_addr = config.getboolean(
            'can_payload_includes_addr', False)
        self.checksum_mode = self._parse_checksum_mode(config)
        self.can_filter = self._parse_can_filter(config)
        self.poll_interval = config.getfloat('poll_interval', 0.10, above=0.0)
        self.error_poll_interval = config.getfloat(
            'error_poll_interval', 0.05, above=0.0)
        self.query_timeout = config.getfloat('query_timeout', 0.006, above=0.0)
        self.offline_timeout = config.getfloat(
            'offline_timeout', max(1.0, self.error_poll_interval * 3.0), above=0.0)
        self.rotation_distance = config.getfloat('rotation_distance', 40.0, above=0.0)
        # microsteps must match the driver's MStep setting (driver default is 16).
        self.microsteps = config.getint('microsteps', 16, minval=1)
        self.full_steps_per_rotation = config.getint('full_steps_per_rotation', 200, minval=1)
        self.auto_start = config.getboolean('auto_start', True)
        self.csv_path = config.get('csv_path', '')
        self.autotune_settle_time = config.getfloat(
            'autotune_settle_time', 0.50, above=0.0, maxval=30.0)
        self.autotune_min_samples = config.getint(
            'autotune_min_samples', 3, minval=1, maxval=1000)
        self.autotune_kp_step = config.getint(
            'autotune_kp_step', 5000, minval=1, maxval=PID_MAX_VALUE)
        self.autotune_ki_step = config.getint(
            'autotune_ki_step', 20, minval=1, maxval=PID_MAX_VALUE)
        self.autotune_kd_step = config.getint(
            'autotune_kd_step', 5000, minval=1, maxval=PID_MAX_VALUE)
        self.autotune_pid_min = config.getint(
            'autotune_pid_min', PID_MIN_VALUE, minval=PID_MIN_VALUE,
            maxval=PID_MAX_VALUE)
        self.autotune_pid_max = config.getint(
            'autotune_pid_max', PID_MAX_VALUE, minval=PID_MIN_VALUE,
            maxval=PID_MAX_VALUE)
        self.pid_write_settle_time = config.getfloat(
            'pid_write_settle_time', 0.05, minval=0.0, maxval=5.0)
        if self.autotune_pid_min > self.autotune_pid_max:
            raise config.error('zdt_emm42: autotune_pid_min must not exceed '
                               'autotune_pid_max')

        self.sock = None
        self.fd_handle = None
        self.timer = None
        self.error_timer = None
        self.enabled = False
        self.query_index = 0
        self.pending_cmd = None
        self.pending_since = 0.0
        self.pending_error_cmd = None
        self.pending_error_since = 0.0
        self.error_history = deque(
            maxlen=max(2, int(math.ceil(ERROR_HISTORY_SECONDS /
                                        self.error_poll_interval)) + 2))
        self.last_error_update_time = None
        self.last = self._empty_status()
        self.error_count = 0
        self.ignored_frames = 0
        self.request_like_frames = 0
        self.standard_frames = 0
        self.ext_other_frames = 0
        self.error_frames = 0
        self.last_error = "not started"
        self.csv_file = None
        self.csv_writer = None
        self.csv_logging = False
        self.autotune_active = False
        self.autotune_abort = False
        self.autotune_capture_active = False
        self.autotune_capture_phase = None
        self.autotune_capture_samples = []
        self.autotune_max_error_deg = None
        self.autotune_safety_violation = None

        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)

        self.gcode.register_mux_command(
            'ZDT_EMM_STATUS', 'NAME', self.name, self.cmd_STATUS,
            desc='Report latest ZDT Emm42 CAN monitor values')
        self.gcode.register_mux_command(
            'ZDT_EMM_QUERY', 'NAME', self.name, self.cmd_QUERY,
            desc='Send one raw short read command to a ZDT Emm42')
        self.gcode.register_mux_command(
            'ZDT_EMM_SNIFF', 'NAME', self.name, self.cmd_SNIFF,
            desc='Capture raw CAN frames to verify the ZDT Emm42 reply framing')
        self.gcode.register_mux_command(
            'ZDT_EMM_LOG', 'NAME', self.name, self.cmd_LOG,
            desc='Enable or disable CSV logging for ZDT Emm42 monitor')
        self.gcode.register_mux_command(
            'ZDT_EMM_POLL', 'NAME', self.name, self.cmd_POLL,
            desc='Enable or disable periodic ZDT Emm42 polling')
        self.gcode.register_mux_command(
            'ZDT_EMM_AUTOTUNE', 'NAME', self.name, self.cmd_AUTOTUNE,
            desc='Tune ZDT Emm42 position-loop PID using a safe axis motion')
        self.gcode.register_mux_command(
            'ZDT_EMM_AUTOTUNE_CANCEL', 'NAME', self.name,
            self.cmd_AUTOTUNE_CANCEL,
            desc='Request cancellation of an active ZDT Emm42 autotune')

    def _parse_checksum_mode(self, config):
        cs = config.get('checksum_mode', '0x6B').strip().lower()
        if cs in ('0x6b', '6b', 'fixed'):
            return 'fixed'
        if cs == 'xor':
            return 'xor'
        if cs in ('crc8', 'crc-8'):
            return 'crc8'
        raise config.error(
            "zdt_emm42: invalid checksum_mode '%s' (use 0x6B, xor or crc8)" % cs)

    def _parse_can_filter(self, config):
        cf = config.get('can_filter', 'ext').strip().lower()
        if cf in ('off', 'none'):
            return 'off'
        if cf in ('ext', 'extended'):
            return 'ext'
        if cf in ('addr', 'address'):
            return 'addr'
        raise config.error(
            "zdt_emm42: invalid can_filter '%s' (use off, ext or addr)" % cf)

    def _empty_status(self):
        return {
            'name': getattr(self, 'name', 'default'),
            'online': False,
            'can_interface': getattr(self, 'can_interface', ''),
            'addr': getattr(self, 'addr', 0),
            'last_update_time': 0.0,
            'error_poll_interval': getattr(self, 'error_poll_interval', 0.05),
            'error_history': [],
            'voltage_mv': None,
            'current_ma': None,
            'encoder_counts': None,
            'input_pulses': None,
            'input_pulses_mm': None,
            'target_counts': None,
            'target_deg': None,
            'target_mm': None,
            'realtime_target_counts': None,
            'realtime_target_deg': None,
            'realtime_target_mm': None,
            'rpm': None,
            'actual_counts': None,
            'actual_deg': None,
            'actual_mm': None,
            'error_counts': None,
            'error_deg': None,
            'error_mm': None,
            'pid_kp': None,
            'pid_ki': None,
            'pid_kd': None,
            'pid_write_status': None,
            'motor_flags': None,
            'enabled': None,
            'reached': None,
            'stalled': None,
            'stall_protect': None,
            'home_flags': None,
            'encoder_ready': None,
            'calibration_ready': None,
            'homing': None,
            'home_failed': None,
            'error_count': 0,
            'last_error': getattr(self, 'last_error', ''),
            'csv_logging': False,
            'last_tx_id': None,
            'last_tx_payload': None,
            'last_rx_id': None,
            'last_rx_payload': None,
            'ignored_frames': 0,
            'request_like_frames': 0,
            'standard_frames': 0,
            'ext_other_frames': 0,
            'error_frames': 0,
            'last_ignored_id': None,
            'last_ignored_payload': None,
            'last_ignored_type': None,
        }

    def _handle_connect(self):
        try:
            self._open_socket()
            self.enabled = self.auto_start
            if self.timer is None:
                self.timer = self.reactor.register_timer(self._poll_timer)
            if self.error_timer is None:
                self.error_timer = self.reactor.register_timer(self._error_poll_timer)
            waketime = self.reactor.NOW if self.enabled else self.reactor.NEVER
            self.reactor.update_timer(self.timer, waketime)
            self.reactor.update_timer(self.error_timer, waketime)
            self.last_error = "ok"
        except Exception as e:
            self.enabled = False
            self.last_error = "CAN open failed: %s" % (e,)
            logging.exception("zdt_emm42 %s: CAN open failed", self.name)

    def _handle_disconnect(self):
        self.enabled = False
        self.pending_cmd = None
        self.pending_error_cmd = None
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)
        if self.error_timer is not None:
            self.reactor.update_timer(self.error_timer, self.reactor.NEVER)
        self._close_csv()
        self._unregister_fd()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.last_error_update_time = None
        self.error_history.clear()
        self.last['error_history'] = []
        self.last['online'] = False
        self.last['last_update_time'] = 0.0
        self.last['error_counts'] = None
        self.last['error_deg'] = None
        self.last['error_mm'] = None

    def _open_socket(self):
        if self.sock is not None:
            return
        s = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.setblocking(False)
        self._apply_can_filter(s)
        s.bind((self.can_interface,))
        self.sock = s
        # Reception is handled asynchronously by the reactor so the poll timer
        # never has to block waiting for a reply.
        self.fd_handle = self.reactor.register_fd(s.fileno(), self._handle_rx)

    def _apply_can_filter(self, s):
        # Filter in-kernel so we don't have to drain the whole bus (e.g. Klipper's
        # own standard-frame MCU traffic) on every poll.
        if self.can_filter == 'off':
            return
        if self.can_filter == 'addr':
            # Only extended frames whose id high byte equals our address; the low
            # byte (CAN packet number) is ignored via the mask.
            can_id = ((self.addr & 0xFF) << 8) | CAN_EFF_FLAG
            can_mask = 0x0000FF00 | CAN_EFF_FLAG
        else:  # 'ext': all extended frames (keeps ext-other diagnostics usable).
            can_id = CAN_EFF_FLAG
            can_mask = CAN_EFF_FLAG
        flt = struct.pack("=II", can_id, can_mask)
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)

    def _unregister_fd(self):
        if self.fd_handle is not None:
            try:
                self.reactor.unregister_fd(self.fd_handle)
            except Exception:
                pass
            self.fd_handle = None

    def _poll_timer(self, eventtime):
        if not self.enabled:
            return self.reactor.NEVER
        try:
            if self.pending_cmd is not None:
                # The previous command was never answered. Count it as a timeout
                # (the async rx handler clears pending_cmd as soon as a reply lands).
                if eventtime - self.pending_since >= self.query_timeout:
                    self._register_no_response(self.pending_cmd)
                    self.pending_cmd = None
                else:
                    return eventtime + self.poll_interval
            cmd = GENERAL_READ_COMMANDS[self.query_index % len(GENERAL_READ_COMMANDS)]
            self.query_index += 1
            self._send_command(cmd)
            self.pending_cmd = cmd
            self.pending_since = eventtime
        except Exception as e:
            self._register_error_failure(str(e))
            self.pending_cmd = None
            logging.exception("zdt_emm42 %s: poll failed", self.name)
        return eventtime + self.poll_interval

    def _error_poll_timer(self, eventtime):
        if not self.enabled:
            return self.reactor.NEVER
        try:
            if self.pending_error_cmd is not None:
                if eventtime - self.pending_error_since >= self.query_timeout:
                    self._register_no_response(self.pending_error_cmd)
                    self.pending_error_cmd = None
                else:
                    return eventtime + self.error_poll_interval
            self._send_command(CMD_POS_ERROR)
            self.pending_error_cmd = CMD_POS_ERROR
            self.pending_error_since = eventtime
        except Exception as e:
            self._register_error_failure(str(e), position_error=True)
            self.pending_error_cmd = None
            logging.exception("zdt_emm42 %s: position-error poll failed", self.name)
        return eventtime + self.error_poll_interval

    def _register_no_response(self, cmd):
        self._register_error_failure(
            "no response for cmd 0x%02X" % cmd,
            position_error=(cmd == CMD_POS_ERROR),
            throttle_message=True)

    def _register_error_failure(self, message, position_error=False,
                                throttle_message=False):
        self.error_count += 1
        self.last['error_count'] = self.error_count
        if position_error:
            self.last['online'] = False
        if not throttle_message or self.error_count % 50 == 1:
            self.last_error = message
            self.last['last_error'] = self.last_error

    def _can_id(self, packet_no=0):
        # ZDT CAN extended ID: ID_Addr left-shifted by 8 bits; low byte is packet number.
        return ((self.addr & 0xFF) << 8) | (packet_no & 0xFF)

    def _checksum(self, logical_bytes):
        # logical_bytes is the command/response WITHOUT the trailing check byte, in
        # its address-included ("serial-shaped") form. The manual defines XOR/CRC as
        # "over all preceding bytes", where the address is the first byte.
        # NOTE: over CAN the address is carried in the frame id, not the payload, so
        # whether the device folds it into XOR/CRC is not documented. This is only
        # relevant for the (opt-in, unverified) xor/crc8 modes; 0x6B is fixed.
        if self.checksum_mode == 'xor':
            c = 0
            for b in logical_bytes:
                c ^= b
            return c & 0xFF
        if self.checksum_mode == 'crc8':
            return self._crc8(logical_bytes)
        return self.check_byte

    def _crc8(self, data):
        # Standard CRC-8 (poly 0x07, init 0x00). Parameters are a guess: the manual
        # gives no CRC-8 example, so treat crc8 mode as experimental until verified.
        crc = 0
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 0x80:
                    crc = ((crc << 1) ^ 0x07) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    def _verify_checksum(self, normalized):
        # normalized is the address-included form: [addr, func, data..., check].
        if len(normalized) < 2:
            return False
        return normalized[-1] == self._checksum(normalized[:-1])

    def _send_command(self, cmd, extra=b''):
        logical = bytearray([self.addr & 0xFF, cmd & 0xFF])
        logical.extend(extra)
        check = self._checksum(logical)
        if self.can_payload_includes_addr:
            payload = bytes(logical) + bytes([check])
        else:
            # Address travels in the extended frame id, so drop it from the payload.
            payload = bytes(logical[1:]) + bytes([check])
        self._send_payload(payload, 0)

    def _send_long_command(self, cmd, extra=b''):
        """Send a ZDT long command using the documented CAN packet format.

        The serial-shaped checksum still includes the address, while the CAN
        payload carries the command and command data only.  Each CAN packet
        repeats the command byte and carries at most seven continuation bytes.
        """
        if self.can_payload_includes_addr:
            raise ValueError(
                'long CAN commands require can_payload_includes_addr=False')
        logical = bytearray([self.addr & 0xFF, cmd & 0xFF])
        logical.extend(extra)
        tail = bytearray(extra)
        tail.append(self._checksum(logical))
        packet_no = 0
        while tail:
            chunk = tail[:7]
            del tail[:7]
            self._send_payload(bytes([cmd & 0xFF]) + bytes(chunk), packet_no)
            packet_no += 1

    def _normalize_long_packet(self, raw, cmd):
        """Return command-only bytes from one CAN response packet."""
        data = bytearray(raw)
        if len(data) >= 2 and data[0] == self.addr and data[1] in (cmd, 0x00):
            return data[1:]
        return data

    def _send_payload(self, payload, packet_no=0):
        if self.sock is None:
            self._open_socket()
        if len(payload) > 8:
            raise ValueError("short-frame sender only supports <=8 bytes")
        arb_id = self._can_id(packet_no) | CAN_EFF_FLAG
        frame = struct.pack(CAN_FRAME_FMT, arb_id, len(payload), payload.ljust(8, b'\x00'))
        self.last['last_tx_id'] = "0x%08X" % (arb_id & CAN_EFF_MASK)
        self.last['last_tx_payload'] = ' '.join('%02X' % b for b in payload)
        self.sock.send(frame)

    def _read_one_frame(self):
        # Non-blocking read of a single CAN frame with classification. Returns
        # ('frame', (arb_id, payload)), ('ignore', None), or None when the socket
        # is empty. The reactor tells us when the fd is readable, so no select here.
        if self.sock is None:
            return None
        try:
            frame = self.sock.recv(CAN_FRAME_SIZE)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            return None
        if len(frame) < CAN_FRAME_SIZE:
            return ('ignore', None)
        can_id, dlc, data = struct.unpack(CAN_FRAME_FMT, frame)
        payload = data[:dlc]
        if can_id & CAN_ERR_FLAG:
            self.error_frames += 1
            self._record_ignored_frame('error', can_id & CAN_EFF_MASK, payload)
            return ('ignore', None)
        if not (can_id & CAN_EFF_FLAG):
            # Standard CAN frames (e.g. Klipper's own MCU traffic) are not ours.
            self.standard_frames += 1
            self._record_ignored_frame('standard', can_id & CAN_SFF_MASK, payload)
            return ('ignore', None)
        arb_id = can_id & CAN_EFF_MASK
        return ('frame', (arb_id, payload))

    def _read_raw_frame(self):
        # Non-blocking read that returns raw (is_extended, id, dlc, payload) with no
        # filtering or bookkeeping. Used only by the sniffer for diagnostics.
        if self.sock is None:
            return None
        try:
            frame = self.sock.recv(CAN_FRAME_SIZE)
        except (BlockingIOError, InterruptedError, OSError):
            return None
        if len(frame) < CAN_FRAME_SIZE:
            return None
        can_id, dlc, data = struct.unpack(CAN_FRAME_FMT, frame)
        eff = bool(can_id & CAN_EFF_FLAG)
        disp_id = can_id & (CAN_EFF_MASK if eff else CAN_SFF_MASK)
        return (eff, disp_id, dlc, bytes(data[:dlc]))

    def _handle_rx(self, eventtime):
        # Reactor callback: drain everything currently readable on the socket.
        try:
            while True:
                frame = self._read_one_frame()
                if frame is None:
                    break
                kind, value = frame
                if kind != 'frame':
                    self.ignored_frames += 1
                    self.last['ignored_frames'] = self.ignored_frames
                    continue
                arb_id, data = value
                self._process_frame(arb_id, data, eventtime)
        except Exception:
            logging.exception("zdt_emm42 %s: rx handler failed", self.name)

    def _find_pending_response(self, raw):
        # There can be one general telemetry request and one position-error
        # request in flight.  Match by the echoed function code instead of
        # assuming that the most recently sent request owns every frame.
        candidates = []
        if self.pending_error_cmd is not None:
            candidates.append(self.pending_error_cmd)
        if self.pending_cmd is not None:
            candidates.append(self.pending_cmd)
        for cmd in candidates:
            data = self._normalize_response(raw, cmd)
            if len(data) >= 2 and data[0] == self.addr and data[1] in (cmd, 0x00):
                return cmd, data
        return None, None

    def _clear_pending(self, cmd):
        if cmd == CMD_POS_ERROR:
            self.pending_error_cmd = None
        elif self.pending_cmd == cmd:
            self.pending_cmd = None

    def _append_error_sample(self, eventtime):
        error_deg = self.last.get('error_deg')
        error_counts = self.last.get('error_counts')
        if error_deg is None or error_counts is None:
            return
        sample = {
            'time': eventtime,
            'error_deg': error_deg,
            'error_counts': error_counts,
            'error_mm': self.last.get('error_mm'),
        }
        self.error_history.append(sample)
        if getattr(self, 'autotune_capture_active', False):
            captured = dict(sample)
            captured['phase'] = getattr(
                self, 'autotune_capture_phase', None) or 'unknown'
            capture_samples = self.autotune_capture_samples
            if len(capture_samples) >= AUTOTUNE_MAX_SAMPLES:
                self.autotune_safety_violation = 'autotune sample buffer full'
            else:
                capture_samples.append(captured)
            limit = getattr(self, 'autotune_max_error_deg', None)
            if limit is not None and abs(float(error_deg)) > limit:
                self.autotune_safety_violation = (
                    'position error %.6f deg exceeded MAX_ERROR_DEG %.6f' %
                    (abs(float(error_deg)), limit))
        self._prune_error_history(eventtime)

    def _prune_error_history(self, eventtime):
        cutoff = eventtime - ERROR_HISTORY_SECONDS
        while self.error_history and self.error_history[0]['time'] < cutoff:
            self.error_history.popleft()
        self.last['error_history'] = [dict(sample) for sample in self.error_history]

    def _resize_error_history(self, maxlen):
        samples = list(self.error_history)
        if len(samples) > maxlen:
            if maxlen <= 1:
                samples = samples[-1:]
            else:
                last_index = len(samples) - 1
                indices = [int(round(index * last_index / float(maxlen - 1)))
                           for index in range(maxlen)]
                samples = [samples[index] for index in indices]
        self.error_history = deque(samples, maxlen=maxlen)
        self.last['error_history'] = [dict(sample) for sample in self.error_history]

    def _process_frame(self, arb_id, raw, eventtime):
        if (arb_id >> 8) != self.addr:
            self.ext_other_frames += 1
            self._record_ignored_frame('extended-other', arb_id, raw)
            self.ignored_frames += 1
            self.last['ignored_frames'] = self.ignored_frames
            return
        if self._is_request_like_payload(raw):
            self.request_like_frames += 1
            self._record_ignored_frame('request-like', arb_id, raw)
            self.ignored_frames += 1
            self.last['ignored_frames'] = self.ignored_frames
            return
        cmd, data = self._find_pending_response(raw)
        if cmd is None:
            # Unsolicited extended frame from our address (e.g. a reached command).
            self.ignored_frames += 1
            self.last['ignored_frames'] = self.ignored_frames
            return
        if len(data) < 4:
            return
        # Generic error: addr 00 EE 6B
        if data[0] == self.addr and data[1] == 0x00 and data[2] == 0xEE:
            self.last_error = "device returned EE for cmd 0x%02X" % cmd
            self._register_error_failure(self.last_error, cmd == CMD_POS_ERROR)
            self._clear_pending(cmd)
            return
        if data[0] == self.addr and data[1] == cmd:
            self.last['last_rx_id'] = "0x%08X" % arb_id
            self.last['last_rx_payload'] = ' '.join('%02X' % b for b in raw)
            if not self._verify_checksum(data):
                self._register_error_failure(
                    "bad check byte for cmd 0x%02X" % cmd,
                    position_error=(cmd == CMD_POS_ERROR))
                self._clear_pending(cmd)
                return
            if not self._record_valid_response(cmd, bytearray(data), eventtime):
                self._register_error_failure(
                    "invalid response for cmd 0x%02X" % cmd,
                    position_error=(cmd == CMD_POS_ERROR))
            self._clear_pending(cmd)

    def _record_valid_response(self, cmd, data, eventtime):
        if not self._parse_response(cmd, data):
            return False
        if cmd == CMD_POS_ERROR:
            self.last['online'] = True
            self.last['last_update_time'] = eventtime
            self.last_error_update_time = eventtime
            self._append_error_sample(eventtime)
        self.last_error = "ok"
        self.last['last_error'] = self.last_error
        self._maybe_write_csv(eventtime)
        return True

    def _record_ignored_frame(self, frame_type, arb_id, payload):
        self.last['request_like_frames'] = self.request_like_frames
        self.last['standard_frames'] = self.standard_frames
        self.last['ext_other_frames'] = self.ext_other_frames
        self.last['error_frames'] = self.error_frames
        self.last['last_ignored_type'] = frame_type
        self.last['last_ignored_id'] = "0x%08X" % arb_id
        self.last['last_ignored_payload'] = ' '.join('%02X' % b for b in payload)

    def _is_request_like_payload(self, data):
        data = bytearray(data)
        if len(data) == 2:
            logical = bytearray([self.addr, data[0]])
            return data[1] == self._checksum(logical)
        if len(data) == 3 and data[0] == self.addr:
            return data[2] == self._checksum(data[:2])
        return False

    def _query_sync(self, cmd, extra=b'', timeout=None, send_long=False):
        # Synchronous request/response for interactive g-code commands only. Blocking
        # briefly here is acceptable because it runs from a g-code handler, not the
        # periodic poll. The periodic path is fully asynchronous (_handle_rx).
        if timeout is None:
            timeout = self.query_timeout
        if self.sock is None:
            self._open_socket()
        # Discard any stale frames still queued before we send.
        while self._read_one_frame() is not None:
            pass
        if send_long:
            self._send_long_command(cmd, extra)
        else:
            self._send_command(cmd, extra)
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0.0:
                return None
            r, _, _ = select.select([self.sock], [], [], remain)
            if not r:
                return None
            frame = self._read_one_frame()
            if frame is None:
                continue
            kind, value = frame
            if kind != 'frame':
                self.ignored_frames += 1
                self.last['ignored_frames'] = self.ignored_frames
                continue
            arb_id, data = value
            if (arb_id >> 8) != self.addr:
                self.ext_other_frames += 1
                self._record_ignored_frame('extended-other', arb_id, data)
                self.ignored_frames += 1
                self.last['ignored_frames'] = self.ignored_frames
                continue
            if self._is_request_like_payload(data):
                self.request_like_frames += 1
                self._record_ignored_frame('request-like', arb_id, data)
                self.ignored_frames += 1
                self.last['ignored_frames'] = self.ignored_frames
                continue
            data = self._normalize_response(data, cmd)
            if len(data) < 4:
                continue
            if data[0] == self.addr and data[1] == 0x00 and data[2] == 0xEE:
                self.last_error = "device returned EE for cmd 0x%02X" % cmd
                return None
            if data[0] == self.addr and data[1] == cmd:
                self.last['last_rx_id'] = "0x%08X" % arb_id
                self.last['last_rx_payload'] = ' '.join('%02X' % b for b in data)
                if not self._verify_checksum(data):
                    self.last_error = "bad check byte for cmd 0x%02X" % cmd
                    return None
                return bytearray(data)

    def _query_long_sync(self, cmd, extra=b'', response_len=None, timeout=None):
        """Send a short request and reassemble a repeated-command CAN response."""
        if response_len is None:
            raise ValueError('response_len is required for a long response')
        if timeout is None:
            timeout = self.query_timeout
        if self.sock is None:
            self._open_socket()
        while self._read_one_frame() is not None:
            pass
        self._send_command(cmd, extra)
        deadline = time.monotonic() + timeout
        expected_tail_len = response_len - 2  # address and command are omitted
        tail = bytearray()
        expected_packet = 0
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0.0:
                self.last_error = "timeout waiting for long response to cmd 0x%02X" % cmd
                return None
            r, _, _ = select.select([self.sock], [], [], remain)
            if not r:
                self.last_error = "timeout waiting for long response to cmd 0x%02X" % cmd
                return None
            frame = self._read_one_frame()
            if frame is None:
                continue
            kind, value = frame
            if kind != 'frame':
                self.ignored_frames += 1
                self.last['ignored_frames'] = self.ignored_frames
                continue
            arb_id, raw = value
            if (arb_id >> 8) != self.addr:
                self.ext_other_frames += 1
                self._record_ignored_frame('extended-other', arb_id, raw)
                self.ignored_frames += 1
                self.last['ignored_frames'] = self.ignored_frames
                continue
            data = self._normalize_long_packet(raw, cmd)
            if len(data) >= 3 and data[0] == 0x00 and data[1] == 0xEE:
                self.last_error = "device returned EE for cmd 0x%02X" % cmd
                return None
            if not data or data[0] != (cmd & 0xFF):
                continue
            packet_no = arb_id & 0xFF
            if packet_no != expected_packet:
                self.last_error = (
                    "out-of-order long response packet for cmd 0x%02X: "
                    "expected %d got %d" % (cmd, expected_packet, packet_no))
                return None
            tail.extend(data[1:])
            expected_packet += 1
            if len(tail) < expected_tail_len:
                continue
            if len(tail) != expected_tail_len:
                self.last_error = "invalid long response length for cmd 0x%02X" % cmd
                return None
            normalized = bytearray([self.addr, cmd & 0xFF]) + tail
            self.last['last_rx_id'] = "0x%08X" % arb_id
            self.last['last_rx_payload'] = ' '.join('%02X' % b for b in raw)
            if not self._verify_checksum(normalized):
                self.last_error = "bad check byte for long cmd 0x%02X" % cmd
                return None
            return normalized

    def _normalize_response(self, data, cmd):
        # Serial-style responses include the address: 01 24 5C 6A 6B. Over CAN the
        # address is usually omitted because the frame id already carries it:
        # 24 5C 6A 6B. Decide using the known function code position (byte 1 is the
        # echoed func code, or 0x00 for an error) rather than a bare "== addr" test,
        # so an address that happens to equal a data byte cannot fool us.
        data = bytearray(data)
        if len(data) >= 2 and data[0] == self.addr and (data[1] == cmd or data[1] == 0x00):
            return data
        return bytearray([self.addr]) + data

    def _parse_response(self, cmd, data):
        parsed = False
        if cmd == CMD_READ_PID and len(data) >= PID_RESPONSE_LEN:
            self.last['pid_kp'] = _u32(data, 2)
            self.last['pid_ki'] = _u32(data, 6)
            self.last['pid_kd'] = _u32(data, 10)
            parsed = True
        elif cmd == CMD_WRITE_PID and len(data) >= 4:
            self.last['pid_write_status'] = data[2]
            parsed = data[2] != 0x00
        elif cmd == CMD_VOLTAGE and len(data) >= 5:
            self.last['voltage_mv'] = _u16(data, 2)
            parsed = True
        elif cmd == CMD_CURRENT and len(data) >= 5:
            self.last['current_ma'] = _u16(data, 2)
            parsed = True
        elif cmd == CMD_ENCODER and len(data) >= 5:
            self.last['encoder_counts'] = _u16(data, 2)
            parsed = True
        elif cmd == CMD_INPUT_PULSES and len(data) >= 8:
            pulses = _signed_value(data[2], _u32(data, 3))
            self.last['input_pulses'] = pulses
            ppr = float(self.microsteps * self.full_steps_per_rotation)
            self.last['input_pulses_mm'] = pulses * self.rotation_distance / ppr
            parsed = True
        elif cmd == CMD_TARGET_POS and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['target_counts'] = counts
            self.last['target_deg'] = self._counts_to_deg(counts)
            self.last['target_mm'] = self._counts_to_mm(counts)
            parsed = True
        elif cmd == CMD_REALTIME_TARGET and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['realtime_target_counts'] = counts
            self.last['realtime_target_deg'] = self._counts_to_deg(counts)
            self.last['realtime_target_mm'] = self._counts_to_mm(counts)
            parsed = True
        elif cmd == CMD_RPM and len(data) >= 6:
            self.last['rpm'] = _signed_value(data[2], _u16(data, 3))
            parsed = True
        elif cmd == CMD_REAL_POS and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['actual_counts'] = counts
            self.last['actual_deg'] = self._counts_to_deg(counts)
            self.last['actual_mm'] = self._counts_to_mm(counts)
            parsed = True
        elif cmd == CMD_POS_ERROR and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['error_counts'] = counts
            self.last['error_deg'] = self._counts_to_deg(counts)
            self.last['error_mm'] = self._counts_to_mm(counts)
            parsed = True
        elif cmd == CMD_MOTOR_FLAGS and len(data) >= 4:
            flags = data[2]
            self.last['motor_flags'] = flags
            self.last['enabled'] = bool(flags & 0x01)
            self.last['reached'] = bool(flags & 0x02)
            self.last['stalled'] = bool(flags & 0x04)
            self.last['stall_protect'] = bool(flags & 0x08)
            parsed = True
        elif cmd == CMD_HOME_FLAGS and len(data) >= 4:
            flags = data[2]
            self.last['home_flags'] = flags
            self.last['encoder_ready'] = bool(flags & 0x01)
            self.last['calibration_ready'] = bool(flags & 0x02)
            self.last['homing'] = bool(flags & 0x04)
            self.last['home_failed'] = bool(flags & 0x08)
            parsed = True
        self.last['csv_logging'] = self.csv_logging
        return parsed

    def _counts_to_deg(self, counts):
        return counts * 360.0 / 65536.0

    def _counts_to_mm(self, counts):
        return counts * self.rotation_distance / 65536.0

    def _read_pid(self, timeout=None):
        data = self._query_long_sync(
            CMD_READ_PID, response_len=PID_RESPONSE_LEN, timeout=timeout)
        if data is None:
            return None
        eventtime = self.reactor.monotonic()
        if not self._record_valid_response(CMD_READ_PID, data, eventtime):
            self.last_error = 'invalid position PID response'
            return None
        return (
            int(self.last['pid_kp']),
            int(self.last['pid_ki']),
            int(self.last['pid_kd']),
        )

    def _write_pid(self, pid, store=0, verify=True, timeout=None):
        if len(pid) != 3:
            raise ValueError('PID must contain Kp, Ki and Kd')
        if store not in (0, 1):
            raise ValueError('PID store flag must be 0 or 1')
        extra = bytearray([PID_WRITE_SUBCOMMAND, store])
        for value in pid:
            extra.extend(_pack_u32(value))
        data = self._query_sync(
            CMD_WRITE_PID, bytes(extra), timeout=timeout, send_long=True)
        if data is None:
            return False
        if not self._record_valid_response(
                CMD_WRITE_PID, data, self.reactor.monotonic()):
            self.last_error = 'invalid position PID write response'
            return False
        settle_time = getattr(self, 'pid_write_settle_time', 0.0)
        if settle_time > 0.0:
            # The driver can acknowledge 0x4A before the new runtime values are
            # visible to a subsequent 0x21 read.  Let that update complete before
            # checking the value or starting the next motion.
            self.reactor.pause(self.reactor.monotonic() + settle_time)
        if verify:
            expected = tuple(int(value) for value in pid)
            readback_retries = 3
            readback = None
            for attempt in range(readback_retries):
                readback = self._read_pid(timeout)
                if readback == expected:
                    return True
                if attempt + 1 < readback_retries and settle_time > 0.0:
                    self.reactor.pause(self.reactor.monotonic() + settle_time)
            self.last_error = (
                'position PID readback mismatch: expected %s got %s' %
                (self._format_pid(pid), self._format_pid(readback)))
            return False
        return True

    def _format_pid(self, pid):
        if pid is None:
            return 'None'
        return 'Kp=%d Ki=%d Kd=%d' % tuple(int(value) for value in pid)

    def _set_polling_enabled(self, enabled):
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._poll_timer)
        if self.error_timer is None:
            self.error_timer = self.reactor.register_timer(self._error_poll_timer)
        self.enabled = bool(enabled)
        if not self.enabled:
            self.pending_cmd = None
            self.pending_error_cmd = None
            self.last['online'] = False
        waketime = self.reactor.NOW if self.enabled else self.reactor.NEVER
        self.reactor.update_timer(self.timer, waketime)
        self.reactor.update_timer(self.error_timer, waketime)

    def _check_autotune_preconditions(self, gcmd, axis):
        if self.can_payload_includes_addr:
            raise gcmd.error(
                "ZDT_EMM_AUTOTUNE requires can_payload_includes_addr=False; "
                "the CAN address belongs in the extended CAN ID")
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise gcmd.error('ZDT_EMM_AUTOTUNE requires the Klipper toolhead object')
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is not None:
            state = str(print_stats.get_status(self.reactor.monotonic()).get(
                'state', '')).lower()
            if state not in ('standby', 'complete', 'cancelled', 'error'):
                raise gcmd.error(
                    'ZDT_EMM_AUTOTUNE requires an idle printer (state=%s)' % state)
        toolhead_status = toolhead.get_status(self.reactor.monotonic())
        if axis in ('x', 'y', 'z') and axis not in str(
                toolhead_status.get('homed_axes', '')).lower():
            raise gcmd.error(
                'axis %s must be homed before ZDT_EMM_AUTOTUNE' % axis.upper())
        # Do not append the calibration moves behind an already queued manual
        # move.  The print-state check above covers the normal case; this wait
        # also handles a just-finished jog or macro.
        toolhead.wait_moves()
        data = self._query_sync(CMD_POS_ERROR, timeout=self.query_timeout)
        if data is None or not self._record_valid_response(
                CMD_POS_ERROR, data, self.reactor.monotonic()):
            raise gcmd.error(
                "ZDT_EMM_AUTOTUNE CAN preflight failed: %s" % self.last_error)
        if not self.get_status(self.reactor.monotonic()).get('online'):
            raise gcmd.error(
                "ZDT_EMM_AUTOTUNE CAN device is offline: %s" % self.last_error)
        pid = self._read_pid()
        if pid is None:
            raise gcmd.error(
                "ZDT_EMM_AUTOTUNE PID read failed: %s" % self.last_error)
        # A same-value STORE=0 write proves the long-request framing and the
        # device's run-time write path before any exploratory candidate is used.
        if not self._write_pid(pid, store=0, verify=True):
            raise gcmd.error(
                "ZDT_EMM_AUTOTUNE PID temporary-write validation failed: %s" %
                self.last_error)
        return toolhead, pid

    def _run_autotune_motion(self, toolhead, axis_index, distance, speed,
                             settle_time, start_time):
        origin = list(toolhead.get_position())
        target = list(origin)
        target[axis_index] += distance
        returned = False
        try:
            toolhead.manual_move(target, speed)
            toolhead.wait_moves()
            toolhead.manual_move(origin, speed)
            toolhead.wait_moves()
            returned = True
        finally:
            if not returned:
                try:
                    toolhead.manual_move(origin, speed)
                    toolhead.wait_moves()
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to return after autotune motion',
                        self.name)
        if settle_time > 0.0:
            self.reactor.pause(self.reactor.monotonic() + settle_time)
        end_time = self.reactor.monotonic()
        self._prune_error_history(end_time)
        return [dict(sample) for sample in self.error_history
                    if start_time <= sample['time'] <= end_time]

    def _score_error_samples(self, samples):
        if len(samples) < self.autotune_min_samples:
            return None
        errors = [float(sample['error_deg']) for sample in samples]
        squared = [value * value for value in errors]
        rms = math.sqrt(sum(squared) / float(len(squared)))
        peak = max(abs(value) for value in errors)
        signs = []
        for value in errors:
            if value > 0.0:
                signs.append(1)
            elif value < 0.0:
                signs.append(-1)
        sign_changes = sum(1 for before, after in zip(signs, signs[1:])
                           if before != after)
        overshoot = 0.0
        if sign_changes:
            first_sign = signs[0] if signs else 0
            crossed = False
            for value in errors:
                if not crossed and value * first_sign < 0.0:
                    crossed = True
                if crossed:
                    overshoot = max(overshoot, abs(value))
        threshold = max(0.01, peak * 0.10)
        last_above = 0
        for index, value in enumerate(errors):
            if abs(value) > threshold:
                last_above = index
        settling_time = max(
            0.0, float(samples[last_above]['time']) - float(samples[0]['time']))
        score = (rms + 0.50 * peak + 0.10 * overshoot +
                 0.05 * peak * sign_changes + 0.01 * settling_time)
        return {
            'score': score,
            'rms': rms,
            'peak': peak,
            'overshoot': overshoot,
            'settling_time': settling_time,
            'samples': len(samples),
        }

    def _median(self, values):
        ordered = sorted(float(value) for value in values)
        count = len(ordered)
        if not count:
            raise ValueError('median requires at least one value')
        middle = count // 2
        if count % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) * 0.5

    def _percentile_abs(self, values, percentile):
        ordered = sorted(abs(float(value)) for value in values)
        if not ordered:
            raise ValueError('percentile requires at least one value')
        index = int(math.ceil(percentile * len(ordered))) - 1
        return ordered[max(0, min(len(ordered) - 1, index))]

    def _score_print_samples(self, samples):
        motion = [float(sample['error_deg']) for sample in samples
                  if str(sample.get('phase', '')).startswith('motion:')]
        settle = [float(sample['error_deg']) for sample in samples
                  if sample.get('phase') == 'settle']
        if len(motion) < self.autotune_min_samples or not settle:
            return None
        motion_rms = math.sqrt(
            sum(value * value for value in motion) / float(len(motion)))
        motion_p95 = self._percentile_abs(motion, 0.95)
        motion_peak = max(abs(value) for value in motion)
        settle_rms = math.sqrt(
            sum(value * value for value in settle) / float(len(settle)))
        score = (0.50 * motion_rms + 0.30 * motion_p95 +
                 0.15 * motion_peak + 0.05 * settle_rms)
        return {
            'score': score,
            'motion_rms': motion_rms,
            'motion_p95': motion_p95,
            'motion_peak': motion_peak,
            'settle_rms': settle_rms,
            'samples': len(samples),
        }

    def _aggregate_print_metrics(self, scenarios):
        if not scenarios:
            return None
        fields = ('score', 'motion_rms', 'motion_p95', 'motion_peak',
                  'settle_rms')
        tier_metrics = []
        normalized = []
        for scenario in scenarios:
            if len(scenario) == 2:
                factor, repeats = scenario
                profile = 'legacy'
            else:
                factor, profile, repeats = scenario
            if not repeats:
                return None
            normalized.append((factor, profile, repeats))
            aggregate = {
                'factor': factor, 'profile': profile,
                'repeats': len(repeats),
            }
            for field in fields:
                aggregate[field] = self._median(
                    [metrics[field] for metrics in repeats])
            aggregate['spread'] = (
                max(metrics['score'] for metrics in repeats) -
                min(metrics['score'] for metrics in repeats))
            tier_metrics.append(aggregate)
        result = {'tiers': tier_metrics}
        for field in fields:
            result[field] = sum(
                metrics[field] for metrics in tier_metrics) / len(tier_metrics)
        result['samples'] = sum(
            metrics['samples'] for _, _, repeats in normalized
            for metrics in repeats)
        result['spread'] = max(metrics['spread'] for metrics in tier_metrics)
        return result

    def _set_error_capture_timer(self, active):
        if self.error_timer is None:
            self.error_timer = self.reactor.register_timer(
                self._error_poll_timer)
        self.pending_error_cmd = None
        self.reactor.update_timer(
            self.error_timer,
            self.reactor.NOW if active else self.reactor.NEVER)

    def _begin_autotune_capture(self, max_error_deg):
        self.autotune_capture_samples = []
        self.autotune_capture_phase = 'prepare'
        self.autotune_safety_violation = None
        self.autotune_max_error_deg = max_error_deg
        self.autotune_capture_active = True
        self._set_error_capture_timer(True)

    def _end_autotune_capture(self):
        self.autotune_capture_active = False
        self.autotune_capture_phase = None
        self._set_error_capture_timer(False)

    def _check_autotune_runtime_safety(self):
        if self.autotune_abort:
            raise RuntimeError('ZDT_EMM_AUTOTUNE cancellation requested')
        violation = self.autotune_safety_violation
        if violation:
            if violation.startswith('position error'):
                raise AutotuneCandidateRejected(violation)
            raise RuntimeError(violation)

    def _corexy_route(self, origin, distance, validation=False,
                      profile='long'):
        if profile not in ('long', 'corner', 'curve'):
            raise ValueError('unknown CoreXY print profile %s' % profile)
        x, y = origin[0], origin[1]
        if profile == 'corner':
            step = min(10.0, distance)
            count = max(1, int(math.floor(distance / step)))
            step = distance / float(count)
            points = []
            if validation:
                for index in range(1, count + 1):
                    points.append(('corner_y_%d' % index,
                                   x + (index - 1) * step,
                                   y + index * step))
                    points.append(('corner_x_%d' % index,
                                   x + index * step,
                                   y + index * step))
                for index in range(count - 1, -1, -1):
                    points.append(('corner_y_return_%d' % index,
                                   x + (index + 1) * step,
                                   y + index * step))
                    points.append(('corner_x_return_%d' % index,
                                   x + index * step,
                                   y + index * step))
            else:
                for index in range(1, count + 1):
                    points.append(('corner_x_%d' % index,
                                   x + index * step,
                                   y + (index - 1) * step))
                    points.append(('corner_y_%d' % index,
                                   x + index * step,
                                   y + index * step))
                for index in range(count - 1, -1, -1):
                    points.append(('corner_x_return_%d' % index,
                                   x + index * step,
                                   y + (index + 1) * step))
                    points.append(('corner_y_return_%d' % index,
                                   x + index * step,
                                   y + index * step))
        elif profile == 'curve':
            segments = 32
            center_x = x + distance * 0.5
            center_y = y + distance * 0.5
            radius = distance * 0.5
            direction = -1.0 if validation else 1.0
            # Enter near the left midpoint of the circle.  This keeps the
            # non-circular lead-in and return inside half of DISTANCE instead
            # of adding a long diagonal across the test square.
            offset = (math.pi + math.pi / segments
                      if validation else math.pi)
            points = []
            for index in range(segments + 1):
                angle = offset + direction * 2.0 * math.pi * index / segments
                points.append(('curve_%02d' % index,
                               center_x + radius * math.cos(angle),
                               center_y + radius * math.sin(angle)))
            points.append(('curve_return', x, y))
        elif validation:
            points = [
                ('diag_xy', x + distance, y + distance),
                ('x_reverse', x, y + distance),
                ('diag_x_minus_y', x + distance, y),
                ('y_forward', x + distance, y + distance),
                ('diag_return', x, y),
            ]
        else:
            points = [
                ('x_forward', x + distance, y),
                ('y_forward', x + distance, y + distance),
                ('diag_return', x, y),
                ('y_forward_alt', x, y + distance),
                ('diag_x_minus_y', x + distance, y),
                ('x_return', x, y),
            ]
        route = []
        for label, target_x, target_y in points:
            target = list(origin)
            target[0] = target_x
            target[1] = target_y
            route.append((label, target))
        return route

    def _check_corexy_workspace(self, toolhead, distance):
        origin = list(toolhead.get_position())
        try:
            kinematics = toolhead.get_kinematics()
            status = kinematics.get_status(self.reactor.monotonic())
        except Exception:
            return origin
        minimum = status.get('axis_minimum')
        maximum = status.get('axis_maximum')

        def coord(value, axis, index):
            if value is None:
                return None
            if hasattr(value, axis):
                return float(getattr(value, axis))
            try:
                return float(value[index])
            except Exception:
                return None

        min_x, min_y = coord(minimum, 'x', 0), coord(minimum, 'y', 1)
        max_x, max_y = coord(maximum, 'x', 0), coord(maximum, 'y', 1)
        if ((min_x is not None and origin[0] < min_x) or
                (min_y is not None and origin[1] < min_y) or
                (max_x is not None and origin[0] + distance > max_x) or
                (max_y is not None and origin[1] + distance > max_y)):
            raise RuntimeError(
                'COREXY_PRINT test square exceeds configured X/Y workspace')
        return origin

    def _set_corexy_motion_limits(self, toolhead, speed, accel):
        status = toolhead.get_status(self.reactor.monotonic())
        saved = (
            float(status['max_velocity']),
            float(status['max_accel']),
            float(status['square_corner_velocity']),
            float(status['minimum_cruise_ratio']),
        )
        if not hasattr(toolhead, 'set_max_velocities'):
            raise RuntimeError(
                'Klipper toolhead does not provide set_max_velocities()')
        toolhead.set_max_velocities(
            min(saved[0], float(speed)),
            min(saved[1], float(accel)),
            saved[2], saved[3])
        return saved

    def _restore_corexy_motion_limits(self, toolhead, saved):
        if saved is not None:
            toolhead.set_max_velocities(saved[0], saved[1], saved[2], saved[3])

    def _queue_corexy_route(self, toolhead, route, speed, profile):
        self.autotune_capture_phase = 'motion:' + profile
        for _, target in route:
            toolhead.manual_move(target, speed)
        # Flush once per complete profile so Klipper can calculate junction
        # velocities across all adjacent segments using its lookahead queue.
        toolhead.wait_moves()
        self._check_autotune_runtime_safety()

    def _run_corexy_print_sample(self, toolhead, distance, speed, accel,
                                 settle_time, max_error_deg,
                                 validation=False, profile='long'):
        origin = self._check_corexy_workspace(toolhead, distance)
        saved_limits = None
        returned = False
        self._begin_autotune_capture(max_error_deg)
        try:
            saved_limits = self._set_corexy_motion_limits(
                toolhead, speed, accel)
            route = self._corexy_route(
                origin, distance, validation=validation, profile=profile)
            self._queue_corexy_route(toolhead, route, speed, profile)
            returned = True
            self.autotune_capture_phase = 'settle'
            self.reactor.pause(self.reactor.monotonic() + settle_time)
            self._check_autotune_runtime_safety()
        finally:
            if not returned:
                try:
                    toolhead.manual_move(origin, speed)
                    toolhead.wait_moves()
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to return CoreXY autotune path',
                        self.name)
            samples = [dict(sample)
                       for sample in self.autotune_capture_samples]
            self._end_autotune_capture()
            self._restore_corexy_motion_limits(toolhead, saved_limits)
        if not self.get_status(self.reactor.monotonic()).get('online'):
            raise RuntimeError('CAN device went offline during CoreXY autotune')
        flags = self._query_sync(CMD_MOTOR_FLAGS, timeout=self.query_timeout)
        if flags is None or not self._record_valid_response(
                CMD_MOTOR_FLAGS, flags, self.reactor.monotonic()):
            raise RuntimeError(
                'failed to query motor state after CoreXY autotune path')
        if self.last.get('stalled') or self.last.get('stall_protect'):
            raise RuntimeError('motor reported stall during CoreXY autotune')
        metrics = self._score_print_samples(samples)
        if metrics is None:
            raise AutotuneCandidateRejected(
                'too few phase-tagged position-error samples')
        return metrics

    def _evaluate_corexy_profile(self, toolhead, distance, max_speed,
                                 max_accel, settle_time, max_error_deg,
                                 repeats, factors, validation=False):
        scenarios = []
        for factor in factors:
            for profile in ('long', 'corner', 'curve'):
                results = []
                for _ in range(repeats):
                    results.append(self._run_corexy_print_sample(
                        toolhead, distance,
                        max(0.001, max_speed * factor),
                        max(0.001, max_accel * factor),
                        settle_time, max_error_deg,
                        validation=validation, profile=profile))
                scenarios.append((factor, profile, results))
        return self._aggregate_print_metrics(scenarios)

    def _corexy_pid_bounds(self, gcmd, original, steps):
        defaults = [
            (max(self.autotune_pid_min, int(original[0] * 0.5)),
             min(self.autotune_pid_max, max(1, int(original[0] * 2.0)))),
            (self.autotune_pid_min,
             min(self.autotune_pid_max,
                 max(int(original[1] * 10), original[1] + 10 * steps[1]))),
            (max(self.autotune_pid_min, int(original[2] * 0.5)),
             min(self.autotune_pid_max, max(1, int(original[2] * 2.0)))),
        ]
        names = ('KP', 'KI', 'KD')
        bounds = []
        for index, name in enumerate(names):
            lower = gcmd.get_int(
                name + '_MIN', defaults[index][0], minval=PID_MIN_VALUE,
                maxval=PID_MAX_VALUE)
            upper = gcmd.get_int(
                name + '_MAX', defaults[index][1], minval=PID_MIN_VALUE,
                maxval=PID_MAX_VALUE)
            if lower > upper or not lower <= original[index] <= upper:
                raise gcmd.error(
                    '%s_MIN/%s_MAX must contain the original PID value' %
                    (name, name))
            bounds.append((lower, upper))
        return bounds

    def _bounded_pid_candidate(self, current, parameter, delta, bounds):
        candidate = list(current)
        lower, upper = bounds[parameter]
        candidate[parameter] = max(
            lower, min(upper, candidate[parameter] + delta))
        if candidate[parameter] == current[parameter]:
            return None
        return tuple(candidate)

    def _format_print_metrics(self, metrics):
        summary = ('score=%.6f rms=%.6f p95=%.6f peak=%.6f '
                   'settle_rms=%.6f spread=%.6f samples=%d' % (
                       metrics['score'], metrics['motion_rms'],
                       metrics['motion_p95'], metrics['motion_peak'],
                       metrics['settle_rms'], metrics.get('spread', 0.0),
                       metrics['samples']))
        tiers = metrics.get('tiers', [])
        if tiers:
            summary += ' tiers=[' + ', '.join(
                '%d%%-%s:%.6f' % (
                    int(round(tier['factor'] * 100)),
                    tier.get('profile', 'legacy'), tier['score'])
                for tier in tiers) + ']'
        return summary

    def _cmd_AUTOTUNE_COREXY(self, gcmd):
        distance = gcmd.get_float(
            'DISTANCE', 100.0, minval=0.1, maxval=100.0)
        max_speed = gcmd.get_float(
            'SPEED', 200.0, minval=0.1, maxval=1000.0)
        max_accel = gcmd.get_float(
            'ACCEL', 5000.0, minval=1.0, maxval=100000.0)
        iterations = gcmd.get_int(
            'ITERATIONS', 20, minval=1, maxval=1000)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=5)
        max_error_deg = gcmd.get_float(
            'MAX_ERROR_DEG', 6.5, minval=0.001, maxval=360.0)
        min_improvement = gcmd.get_float(
            'MIN_IMPROVEMENT', 0.02, minval=0.0, maxval=0.50)
        settle_time = gcmd.get_float(
            'SETTLE', self.autotune_settle_time, minval=0.05, maxval=30.0)
        if gcmd.get_int('CONFIRM', 0, minval=0, maxval=1) != 1:
            raise gcmd.error(
                'COREXY_PRINT moves X and Y; pass CONFIRM=1')
        steps = [
            gcmd.get_int('KP_STEP', self.autotune_kp_step, minval=1,
                         maxval=PID_MAX_VALUE),
            gcmd.get_int('KI_STEP', self.autotune_ki_step, minval=1,
                         maxval=PID_MAX_VALUE),
            gcmd.get_int('KD_STEP', self.autotune_kd_step, minval=1,
                         maxval=PID_MAX_VALUE),
        ]
        was_enabled = self.enabled
        original_interval = self.error_poll_interval
        original_history_maxlen = self.error_history.maxlen
        original = None
        toolhead = None
        general_timer_paused = False
        persist_attempted = False
        self.autotune_active = True
        self.autotune_abort = False
        try:
            self._set_polling_enabled(True)
            self.reactor.update_timer(self.timer, self.reactor.NEVER)
            self.reactor.update_timer(self.error_timer, self.reactor.NEVER)
            self.pending_cmd = None
            self.pending_error_cmd = None
            general_timer_paused = True
            toolhead, original = self._check_autotune_preconditions(gcmd, 'x')
            homed = str(toolhead.get_status(
                self.reactor.monotonic()).get('homed_axes', '')).lower()
            if 'y' not in homed:
                raise gcmd.error(
                    'X and Y must both be homed for PROFILE=COREXY_PRINT')
            self._check_corexy_workspace(toolhead, distance)
            bounds = self._corexy_pid_bounds(gcmd, original, steps)

            # Dedicated tuning capture owns the 0x37 timer.  The slower general
            # telemetry timer is paused so it cannot compete with PID readback.
            self.error_poll_interval = AUTOTUNE_SAMPLE_INTERVAL
            self._resize_error_history(max(
                2, int(math.ceil(ERROR_HISTORY_SECONDS /
                                 AUTOTUNE_SAMPLE_INTERVAL)) + 2))

            current = tuple(original)
            try:
                baseline = self._evaluate_corexy_profile(
                    toolhead, distance, max_speed, max_accel, settle_time,
                    max_error_deg, repeats, (0.40, 0.70, 1.00))
            except AutotuneCandidateRejected as exc:
                raise gcmd.error(
                    'COREXY_PRINT baseline rejected: %s. Increase '
                    'MAX_ERROR_DEG or reduce SPEED/ACCEL' % exc)
            if baseline is None:
                raise gcmd.error('COREXY_PRINT baseline produced no metrics')
            best = baseline
            gcmd.respond_info(
                'ZDT Emm42 CoreXY baseline: %s %s' % (
                    self._format_pid(current),
                    self._format_print_metrics(best)))

            no_improvement_cycles = 0
            cycle_improved = False
            parameter_names = ('Kp', 'Ki', 'Kd')
            for iteration in range(1, iterations + 1):
                self._check_autotune_runtime_safety()
                parameter = (iteration - 1) % 3
                candidates = []
                for sign in (1, -1):
                    candidate = self._bounded_pid_candidate(
                        current, parameter, sign * steps[parameter], bounds)
                    if candidate is not None and candidate not in candidates:
                        candidates.append(candidate)
                evaluated = []
                for candidate in candidates:
                    if not self._write_pid(candidate, store=0, verify=True):
                        raise gcmd.error(
                            'temporary PID write failed: %s' % self.last_error)
                    try:
                        metrics = self._evaluate_corexy_profile(
                            toolhead, distance, max_speed, max_accel,
                            settle_time, max_error_deg, repeats,
                            (0.40, 0.70, 1.00))
                    except AutotuneCandidateRejected as exc:
                        self.autotune_safety_violation = None
                        gcmd.respond_info(
                            'CoreXY iteration %d %s rejected: %s (%s)' % (
                                iteration, parameter_names[parameter],
                                self._format_pid(candidate), exc))
                        continue
                    evaluated.append((metrics['score'], candidate, metrics))
                    gcmd.respond_info(
                        'CoreXY iteration %d %s candidate %s %s' % (
                            iteration, parameter_names[parameter],
                            self._format_pid(candidate),
                            self._format_print_metrics(metrics)))

                accepted = False
                if evaluated:
                    _, candidate, metrics = min(evaluated, key=lambda item: item[0])
                    threshold = best['score'] * (1.0 - min_improvement)
                    if metrics['score'] < threshold:
                        current = candidate
                        best = metrics
                        accepted = True
                        cycle_improved = True
                if not self._write_pid(current, store=0, verify=True):
                    raise gcmd.error(
                        'failed to restore selected PID: %s' % self.last_error)
                gcmd.respond_info(
                    'CoreXY iteration %d %s: %s; selected %s' % (
                        iteration, parameter_names[parameter],
                        'accepted' if accepted else 'no significant improvement',
                        self._format_pid(current)))

                if iteration % 3 == 0:
                    steps = [max(1, step // 2) for step in steps]
                    if cycle_improved:
                        no_improvement_cycles = 0
                    else:
                        no_improvement_cycles += 1
                    cycle_improved = False
                    if no_improvement_cycles >= 2:
                        gcmd.respond_info(
                            'CoreXY autotune converged: two full PID cycles '
                            'without significant improvement')
                        break

            # Held-out route and speed factors are not used during the search.
            if not self._write_pid(original, store=0, verify=True):
                raise gcmd.error(
                    'failed to prepare original PID validation: %s' %
                    self.last_error)
            try:
                original_validation = self._evaluate_corexy_profile(
                    toolhead, distance, max_speed, max_accel, settle_time,
                    max_error_deg, repeats, (0.55, 0.85), validation=True)
            except AutotuneCandidateRejected as exc:
                raise gcmd.error(
                    'original PID validation exceeded safety limit: %s' % exc)
            if not self._write_pid(current, store=0, verify=True):
                raise gcmd.error(
                    'failed to prepare best PID validation: %s' %
                    self.last_error)
            try:
                best_validation = self._evaluate_corexy_profile(
                    toolhead, distance, max_speed, max_accel, settle_time,
                    max_error_deg, repeats, (0.55, 0.85), validation=True)
            except AutotuneCandidateRejected as exc:
                self.autotune_safety_violation = None
                if not self._write_pid(original, store=0, verify=True):
                    raise gcmd.error(
                        'validation rejected and original PID restore failed: %s' %
                        self.last_error)
                gcmd.respond_info(
                    'CoreXY validation rejected the tuned PID (%s); original '
                    'PID restored and nothing stored' % exc)
                return
            validation_threshold = (
                original_validation['score'] * (1.0 - min_improvement))
            if (current == tuple(original) or
                    best_validation['score'] >= validation_threshold):
                if not self._write_pid(original, store=0, verify=True):
                    raise gcmd.error(
                        'no validated improvement and original restore failed: %s' %
                        self.last_error)
                gcmd.respond_info(
                    'CoreXY autotune found no validated improvement; original '
                    'PID restored. original_validation %s tuned_validation %s' % (
                        self._format_print_metrics(original_validation),
                        self._format_print_metrics(best_validation)))
                return

            persist_attempted = True
            if not self._write_pid(current, store=1, verify=True):
                self._write_pid(original, store=1, verify=False)
                raise gcmd.error(
                    'failed to persist validated PID: %s' % self.last_error)
            gcmd.respond_info(
                'ZDT Emm42 CoreXY autotune complete: original %s; best %s; '
                'training %s; validation %s; stored without Klipper restart' % (
                    self._format_pid(original), self._format_pid(current),
                    self._format_print_metrics(best),
                    self._format_print_metrics(best_validation)))
        except self.printer.command_error:
            if original is not None:
                try:
                    self._write_pid(
                        original, store=1 if persist_attempted else 0,
                        verify=False)
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to restore original CoreXY PID',
                        self.name)
            raise
        except Exception as exc:
            if original is not None:
                try:
                    self._write_pid(
                        original, store=1 if persist_attempted else 0,
                        verify=False)
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to restore original CoreXY PID',
                        self.name)
            logging.exception(
                'zdt_emm42 %s: unexpected CoreXY autotune failure', self.name)
            raise gcmd.error('COREXY_PRINT autotune failed safely: %s' % exc)
        finally:
            if self.autotune_capture_active:
                self._end_autotune_capture()
            self.error_poll_interval = original_interval
            self._prune_error_history(self.reactor.monotonic())
            self._resize_error_history(original_history_maxlen)
            if general_timer_paused and was_enabled:
                self.enabled = True
                self.reactor.update_timer(self.timer, self.reactor.NOW)
                self.reactor.update_timer(self.error_timer, self.reactor.NOW)
            elif not was_enabled:
                self._set_polling_enabled(False)
            self.autotune_max_error_deg = None
            self.autotune_safety_violation = None
            self.autotune_abort = False
            self.autotune_active = False

    def _next_pid_candidate(self, current, parameter, direction, step):
        candidate = list(current)
        value = candidate[parameter] + direction * step
        if value < self.autotune_pid_min or value > self.autotune_pid_max:
            direction = -direction
            value = candidate[parameter] + direction * step
        value = max(self.autotune_pid_min,
                    min(self.autotune_pid_max, int(value)))
        if value == candidate[parameter]:
            return None, direction
        candidate[parameter] = value
        return tuple(candidate), direction

    def _autotune_info(self, gcmd, iteration, pid, metrics, accepted,
                       reason=None):
        if metrics is None:
            gcmd.respond_info(
                'ZDT Emm42 autotune iteration %d: %s invalid (%s)' %
                (iteration, self._format_pid(pid), reason or self.last_error))
            return
        gcmd.respond_info(
            'ZDT Emm42 autotune iteration %d: %s score=%.6f rms=%.6f '
            'peak=%.6f overshoot=%.6f settling=%.3fs samples=%d %s' % (
                iteration, self._format_pid(pid), metrics['score'],
                metrics['rms'], metrics['peak'], metrics['overshoot'],
                metrics['settling_time'], metrics['samples'],
                'accepted' if accepted else 'rejected'))

    def cmd_AUTOTUNE(self, gcmd):
        if self.autotune_active:
            raise gcmd.error('ZDT_EMM_AUTOTUNE is already running')
        profile = gcmd.get('PROFILE', 'AXIS').strip().upper()
        if profile == 'COREXY_PRINT':
            return self._cmd_AUTOTUNE_COREXY(gcmd)
        if profile != 'AXIS':
            raise gcmd.error('PROFILE must be AXIS or COREXY_PRINT')
        axis = gcmd.get('AXIS', '').strip().lower()
        if axis not in ('x', 'y', 'z', 'e'):
            raise gcmd.error('AXIS must be one of X, Y, Z or E')
        distance = gcmd.get_float('DISTANCE', minval=0.001, maxval=100.0)
        speed = gcmd.get_float('SPEED', minval=0.001, maxval=1000.0)
        accel = gcmd.get_float('ACCEL', minval=0.001, maxval=100000.0)
        iterations = gcmd.get_int('ITERATIONS', minval=1, maxval=1000)
        if gcmd.get_int('CONFIRM', 0, minval=0, maxval=1) != 1:
            raise gcmd.error(
                'ZDT_EMM_AUTOTUNE moves the selected axis; pass CONFIRM=1')
        settle_time = gcmd.get_float(
            'SETTLE', self.autotune_settle_time, minval=0.0, maxval=30.0)
        steps = [
            gcmd.get_int('KP_STEP', self.autotune_kp_step, minval=1,
                         maxval=PID_MAX_VALUE),
            gcmd.get_int('KI_STEP', self.autotune_ki_step, minval=1,
                         maxval=PID_MAX_VALUE),
            gcmd.get_int('KD_STEP', self.autotune_kd_step, minval=1,
                         maxval=PID_MAX_VALUE),
        ]
        axis_index = {'x': 0, 'y': 1, 'z': 2, 'e': 3}[axis]
        was_enabled = self.enabled
        saved_accel = None
        original = None
        current = None
        self.autotune_active = True
        self.autotune_abort = False
        try:
            self._set_polling_enabled(True)
            toolhead, original = self._check_autotune_preconditions(gcmd, axis)
            current = tuple(original)
            saved_accel = getattr(toolhead, 'max_accel', None)
            if saved_accel is not None:
                toolhead.max_accel = min(float(saved_accel), accel)

            baseline_start = self.reactor.monotonic()
            baseline_samples = self._run_autotune_motion(
                toolhead, axis_index, distance, speed, settle_time,
                baseline_start)
            baseline = self._score_error_samples(baseline_samples)
            if baseline is None:
                raise gcmd.error(
                    'ZDT_EMM_AUTOTUNE baseline has too few valid error samples')
            best = baseline
            directions = [1, 1, 1]
            failed = [0, 0, 0]
            gcmd.respond_info(
                'ZDT Emm42 autotune baseline: %s score=%.6f rms=%.6f '
                'peak=%.6f samples=%d' % (
                    self._format_pid(current), best['score'], best['rms'],
                    best['peak'], best['samples']))

            for iteration in range(1, iterations + 1):
                if self.autotune_abort:
                    raise gcmd.error('ZDT_EMM_AUTOTUNE aborted')
                parameter = (iteration - 1) % 3
                candidate, directions[parameter] = self._next_pid_candidate(
                    current, parameter, directions[parameter], steps[parameter])
                if candidate is None:
                    failed[parameter] += 1
                    steps[parameter] = max(1, steps[parameter] // 2)
                    directions[parameter] *= -1
                    self._autotune_info(
                        gcmd, iteration, current, None, False,
                        'search bound reached; step reduced')
                    continue
                if not self._write_pid(candidate, store=0, verify=True):
                    raise gcmd.error(
                        'temporary PID write failed at iteration %d: %s' %
                        (iteration, self.last_error))
                before_errors = self.error_count
                sample_start = self.reactor.monotonic()
                samples = self._run_autotune_motion(
                    toolhead, axis_index, distance, speed, settle_time,
                    sample_start)
                failure_reason = None
                if self.error_count != before_errors or not self.get_status(
                        self.reactor.monotonic()).get('online'):
                    metrics = None
                    self.last_error = 'CAN error or offline during motion'
                    failure_reason = self.last_error
                elif self.last.get('stalled'):
                    metrics = None
                    self.last_error = 'motor reported stall during motion'
                    failure_reason = self.last_error
                else:
                    metrics = self._score_error_samples(samples)
                    if metrics is None:
                        self.last_error = 'too few valid error samples'
                        failure_reason = self.last_error
                accepted = metrics is not None and metrics['score'] < best['score']
                if accepted:
                    current = candidate
                    best = metrics
                    failed[parameter] = 0
                else:
                    if not self._write_pid(current, store=0, verify=True):
                        raise gcmd.error(
                            'failed to restore PID after iteration %d: %s' %
                            (iteration, self.last_error))
                    directions[parameter] *= -1
                    failed[parameter] += 1
                    if failed[parameter] >= 2:
                        steps[parameter] = max(1, steps[parameter] // 2)
                        failed[parameter] = 0
                self._autotune_info(
                    gcmd, iteration, candidate, metrics, accepted,
                    failure_reason)

            if not self._write_pid(current, store=1, verify=True):
                self._write_pid(original, store=1, verify=False)
                raise gcmd.error(
                    'failed to persist best PID; restored original where possible: %s' %
                    self.last_error)
            final_pid = self._read_pid()
            if final_pid != tuple(current):
                self._write_pid(original, store=1, verify=False)
                raise gcmd.error(
                    'final PID readback mismatch; restored original where possible')
            gcmd.respond_info(
                'ZDT Emm42 autotune complete: original %s; best %s; '
                'score=%.6f; stored in driver without Klipper restart' % (
                    self._format_pid(original), self._format_pid(current),
                    best['score']))
        except self.printer.command_error:
            if original is not None:
                try:
                    self._write_pid(original, store=0, verify=False)
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to restore original PID', self.name)
            raise
        except Exception as exc:
            if original is not None:
                try:
                    self._write_pid(original, store=0, verify=False)
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to restore original PID', self.name)
            logging.exception(
                'zdt_emm42 %s: unexpected axis autotune failure', self.name)
            raise gcmd.error('ZDT_EMM_AUTOTUNE failed safely: %s' % exc)
        finally:
            if saved_accel is not None:
                try:
                    toolhead.max_accel = saved_accel
                except Exception:
                    logging.exception(
                        'zdt_emm42 %s: failed to restore acceleration', self.name)
            if not was_enabled:
                self._set_polling_enabled(False)
            self.autotune_abort = False
            self.autotune_active = False

    def cmd_AUTOTUNE_CANCEL(self, gcmd):
        if not self.autotune_active:
            gcmd.respond_info('ZDT EMM autotune is not running')
            return
        self.autotune_abort = True
        gcmd.respond_info(
            'ZDT EMM autotune cancellation requested; the active move segment '
            'will finish before PID restoration')

    def _open_csv(self, path=None):
        if path:
            self.csv_path = path
        if not self.csv_path:
            raise self.printer.command_error("csv_path is empty; pass PATH=... or set csv_path in config")
        if self.csv_file is not None:
            return
        directory = os.path.dirname(self.csv_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
        self.csv_file = open(self.csv_path, 'a', newline='')
        self.csv_writer = csv.writer(self.csv_file)
        if self.csv_file.tell() == 0:
            self.csv_writer.writerow([
                'monotonic_time', 'name', 'voltage_mv', 'current_ma', 'encoder_counts',
                'input_pulses', 'input_pulses_mm', 'target_counts', 'target_deg', 'target_mm',
                'realtime_target_counts', 'realtime_target_deg', 'realtime_target_mm',
                'rpm', 'actual_counts', 'actual_deg', 'actual_mm', 'error_counts', 'error_deg',
                'error_mm', 'motor_flags', 'enabled', 'reached', 'stalled', 'stall_protect',
                'home_flags', 'encoder_ready', 'calibration_ready', 'homing', 'home_failed'
            ])
        self.csv_logging = True
        self.last['csv_logging'] = True

    def _close_csv(self):
        self.csv_logging = False
        self.last['csv_logging'] = False
        if self.csv_file is not None:
            try:
                self.csv_file.flush()
                self.csv_file.close()
            except Exception:
                pass
        self.csv_file = None
        self.csv_writer = None

    def _maybe_write_csv(self, eventtime):
        if not self.csv_logging:
            return
        if self.csv_file is None:
            self._open_csv()
        l = self.last
        self.csv_writer.writerow([
            '%.6f' % eventtime, self.name, l.get('voltage_mv'), l.get('current_ma'),
            l.get('encoder_counts'), l.get('input_pulses'), l.get('input_pulses_mm'),
            l.get('target_counts'), l.get('target_deg'), l.get('target_mm'),
            l.get('realtime_target_counts'), l.get('realtime_target_deg'),
            l.get('realtime_target_mm'), l.get('rpm'),
            l.get('actual_counts'), l.get('actual_deg'), l.get('actual_mm'),
            l.get('error_counts'), l.get('error_deg'), l.get('error_mm'), l.get('motor_flags'),
            l.get('enabled'), l.get('reached'), l.get('stalled'), l.get('stall_protect'),
            l.get('home_flags'), l.get('encoder_ready'), l.get('calibration_ready'),
            l.get('homing'), l.get('home_failed')
        ])
        self.csv_file.flush()

    def get_status(self, eventtime):
        self._prune_error_history(eventtime)
        status = dict(self.last)
        status['error_count'] = self.error_count
        status['last_error'] = self.last_error
        status['error_history'] = [dict(sample) for sample in self.error_history]
        status['error_poll_interval'] = self.error_poll_interval
        status['online'] = bool(
            self.enabled and status.get('online') is True and
            self.last_error_update_time is not None and
            eventtime - self.last_error_update_time <= self.offline_timeout)
        return status

    def cmd_STATUS(self, gcmd):
        l = self.last
        lines = [
            "ZDT Emm42 '%s' addr=%d interface=%s online=%s" % (
                self.name, self.addr, self.can_interface, l.get('online')),
            "V=%s mV  I=%s mA  rpm=%s" % (l.get('voltage_mv'), l.get('current_ma'), l.get('rpm')),
            "target=%s deg / %s mm" % (self._fmt(l.get('target_deg')), self._fmt(l.get('target_mm'))),
            "rt_target=%s deg / %s mm" % (
                self._fmt(l.get('realtime_target_deg')), self._fmt(l.get('realtime_target_mm'))),
            "actual=%s deg / %s mm" % (self._fmt(l.get('actual_deg')), self._fmt(l.get('actual_mm'))),
            "error=%s deg / %s mm" % (self._fmt(l.get('error_deg')), self._fmt(l.get('error_mm'))),
            "pid: Kp=%s Ki=%s Kd=%s write_status=%s" % (
                l.get('pid_kp'), l.get('pid_ki'), l.get('pid_kd'),
                l.get('pid_write_status')),
            "flags: enabled=%s reached=%s stalled=%s stall_protect=%s" % (
                l.get('enabled'), l.get('reached'), l.get('stalled'), l.get('stall_protect')),
            "home: encoder_ready=%s calibration_ready=%s homing=%s home_failed=%s" % (
                l.get('encoder_ready'), l.get('calibration_ready'), l.get('homing'), l.get('home_failed')),
            "errors=%d last_error=%s csv=%s" % (self.error_count, self.last_error, self.csv_logging),
            "tx: id=%s data=%s" % (l.get('last_tx_id'), l.get('last_tx_payload')),
            "rx: id=%s data=%s ignored=%s" % (
                l.get('last_rx_id'), l.get('last_rx_payload'), l.get('ignored_frames')),
            "ignored detail: request_like=%s standard=%s ext_other=%s error=%s last=%s id=%s data=%s" % (
                l.get('request_like_frames'), l.get('standard_frames'),
                l.get('ext_other_frames'), l.get('error_frames'),
                l.get('last_ignored_type'), l.get('last_ignored_id'),
                l.get('last_ignored_payload')),
        ]
        gcmd.respond_info("\n".join(lines))

    def cmd_QUERY(self, gcmd):
        cmd_s = gcmd.get('CMD')
        cmd = _parse_int(cmd_s)
        if cmd < 0 or cmd > 255:
            raise gcmd.error("CMD must be 0..255")
        try:
            extra = _parse_hex_bytes(gcmd.get('DATA', ''))
        except ValueError:
            raise gcmd.error("DATA must be hex bytes, e.g. DATA=6C or DATA=\"6C 00\"")
        if len(extra) > 6:
            raise gcmd.error("DATA too long for a single short frame")
        if cmd == CMD_READ_PID:
            if extra:
                raise gcmd.error('CMD 0x21 does not accept DATA')
            data = self._query_long_sync(
                cmd, response_len=PID_RESPONSE_LEN, timeout=self.query_timeout)
        else:
            data = self._query_sync(cmd, extra, self.query_timeout)
        if data is None:
            raise gcmd.error("No valid response for cmd 0x%02X: %s" % (cmd, self.last_error))
        if not self._record_valid_response(cmd, data, self.reactor.monotonic()):
            raise gcmd.error("Invalid response for cmd 0x%02X" % cmd)
        gcmd.respond_info("0x%02X <= %s" % (cmd, ' '.join('%02X' % b for b in data)))

    def cmd_SNIFF(self, gcmd):
        seconds = gcmd.get_float('SECONDS', 2.0, above=0.0, maxval=10.0)
        max_frames = gcmd.get_int('MAX', 80, minval=1, maxval=500)
        if self.sock is None:
            self._open_socket()
        reactor = self.reactor
        # Take over the socket from the async handler for the capture window so the
        # two don't fight over recv(). reactor.pause() keeps the MCU serviced.
        had_fd = self.fd_handle is not None
        self._unregister_fd()
        captured = []
        try:
            while self._read_raw_frame() is not None:
                pass
            end = reactor.monotonic() + seconds
            idx = 0
            next_send = reactor.monotonic()
            while True:
                now = reactor.monotonic()
                if now >= end or len(captured) >= max_frames:
                    break
                if now >= next_send:
                    cmd = READ_COMMANDS[idx % len(READ_COMMANDS)]
                    idx += 1
                    try:
                        self._send_command(cmd)
                    except Exception:
                        pass
                    next_send = now + 0.05
                got = False
                while len(captured) < max_frames:
                    raw = self._read_raw_frame()
                    if raw is None:
                        break
                    got = True
                    eff, disp_id, dlc, payload = raw
                    captured.append("%s 0x%0*X [%d] %s" % (
                        'EXT' if eff else 'STD', 8 if eff else 3, disp_id, dlc,
                        ' '.join('%02X' % b for b in payload)))
                reactor.pause(reactor.monotonic() + (0.0 if got else 0.02))
        finally:
            if had_fd and self.sock is not None:
                self.fd_handle = reactor.register_fd(self.sock.fileno(), self._handle_rx)
        if not captured:
            gcmd.respond_info(
                "ZDT Emm42 '%s': no frames captured in %.1fs (check wiring, "
                "CAN1_MAP, bitrate, and can_filter)" % (self.name, seconds))
        else:
            gcmd.respond_info("ZDT Emm42 '%s' captured %d frame(s):\n%s" % (
                self.name, len(captured), "\n".join(captured)))

    def cmd_LOG(self, gcmd):
        enable = gcmd.get_int('ENABLE', 1, minval=0, maxval=1)
        if enable:
            path = gcmd.get('PATH', None)
            self._open_csv(path)
            gcmd.respond_info("ZDT Emm42 '%s' CSV logging enabled: %s" % (self.name, self.csv_path))
        else:
            self._close_csv()
            gcmd.respond_info("ZDT Emm42 '%s' CSV logging disabled" % self.name)

    def cmd_POLL(self, gcmd):
        enable = gcmd.get_int('ENABLE', 1, minval=0, maxval=1)
        self._set_polling_enabled(bool(enable))
        gcmd.respond_info("ZDT Emm42 '%s' polling %s" % (self.name, 'enabled' if self.enabled else 'disabled'))

    def _fmt(self, value):
        if value is None:
            return 'None'
        try:
            return '%.6f' % float(value)
        except Exception:
            return str(value)


def load_config_prefix(config):
    return ZdtEmm42(config)


def load_config(config):
    return ZdtEmm42(config)
