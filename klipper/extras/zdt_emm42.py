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
#   query_timeout: 0.006
#   rotation_distance: 40
#   microsteps: 16             # MUST match the driver's MStep setting (driver default is 16)
#   full_steps_per_rotation: 200
#   csv_path: /tmp/zdt_emm42_shadow_a.csv
#
# G-code:
#   ZDT_EMM_STATUS NAME=shadow_a
#   ZDT_EMM_QUERY  NAME=shadow_a CMD=0x36
#   ZDT_EMM_QUERY  NAME=shadow_a CMD=0x42 DATA=6C   ; DATA appends extra request bytes
#   ZDT_EMM_SNIFF  NAME=shadow_a SECONDS=2          ; capture raw CAN frames for debugging
#   ZDT_EMM_LOG    NAME=shadow_a ENABLE=1
#   ZDT_EMM_LOG    NAME=shadow_a ENABLE=0
#   ZDT_EMM_POLL   NAME=shadow_a ENABLE=1
#
# Notes:
# - Emm42 must be set to CAN1_MAP, extended CAN frame, and the same bus bitrate as can0.
# - This module uses Linux SocketCAN directly, not Klipper's MCU CAN protocol.
# - Reception is asynchronous via the Klipper reactor (register_fd); the poll timer only
#   sends one short read command per tick and never blocks waiting for the reply.
# - It intentionally avoids motion commands and only issues short read commands.

import csv
import logging
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
CMD_READ_PID = 0x21          # response is >8 bytes; not used by default in CAN short polling
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


def _u16(data, index):
    return (data[index] << 8) | data[index + 1]


def _u32(data, index):
    return ((data[index] << 24) | (data[index + 1] << 16) |
            (data[index + 2] << 8) | data[index + 3])


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
        self.query_timeout = config.getfloat('query_timeout', 0.006, above=0.0)
        self.rotation_distance = config.getfloat('rotation_distance', 40.0, above=0.0)
        # microsteps must match the driver's MStep setting (driver default is 16).
        self.microsteps = config.getint('microsteps', 16, minval=1)
        self.full_steps_per_rotation = config.getint('full_steps_per_rotation', 200, minval=1)
        self.auto_start = config.getboolean('auto_start', True)
        self.csv_path = config.get('csv_path', '')

        self.sock = None
        self.fd_handle = None
        self.timer = None
        self.enabled = False
        self.query_index = 0
        self.pending_cmd = None
        self.pending_since = 0.0
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
            self.timer = self.reactor.register_timer(self._poll_timer)
            waketime = self.reactor.NOW if self.enabled else self.reactor.NEVER
            self.reactor.update_timer(self.timer, waketime)
            self.last_error = "ok"
        except Exception as e:
            self.enabled = False
            self.last_error = "CAN open failed: %s" % (e,)
            logging.exception("zdt_emm42 %s: CAN open failed", self.name)

    def _handle_disconnect(self):
        self.enabled = False
        self._close_csv()
        self._unregister_fd()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

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
            cmd = READ_COMMANDS[self.query_index % len(READ_COMMANDS)]
            self.query_index += 1
            self._send_command(cmd)
            self.pending_cmd = cmd
            self.pending_since = eventtime
        except Exception as e:
            self.error_count += 1
            self.last['error_count'] = self.error_count
            self.last_error = str(e)
            self.last['last_error'] = self.last_error
            self.pending_cmd = None
            logging.exception("zdt_emm42 %s: poll failed", self.name)
        return eventtime + self.poll_interval

    def _register_no_response(self, cmd):
        self.error_count += 1
        self.last['error_count'] = self.error_count
        if self.error_count % 50 == 1:
            self.last_error = "no response for cmd 0x%02X" % cmd
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
        cmd = self.pending_cmd
        if cmd is None:
            # Unsolicited extended frame from our address (e.g. a reached command).
            self.ignored_frames += 1
            self.last['ignored_frames'] = self.ignored_frames
            return
        data = self._normalize_response(raw, cmd)
        if len(data) < 4:
            return
        # Generic error: addr 00 EE 6B
        if data[0] == self.addr and data[1] == 0x00 and data[2] == 0xEE:
            self.last_error = "device returned EE for cmd 0x%02X" % cmd
            self.error_count += 1
            self.last['error_count'] = self.error_count
            self.pending_cmd = None
            return
        if data[0] == self.addr and data[1] == cmd:
            self.last['last_rx_id'] = "0x%08X" % arb_id
            self.last['last_rx_payload'] = ' '.join('%02X' % b for b in raw)
            if not self._verify_checksum(data):
                self.last_error = "bad check byte for cmd 0x%02X" % cmd
                self.error_count += 1
                self.last['error_count'] = self.error_count
                self.pending_cmd = None
                return
            self._parse_response(cmd, bytearray(data))
            self.last['online'] = True
            self.last['last_update_time'] = eventtime
            self.last_error = "ok"
            self.last['last_error'] = self.last_error
            self.pending_cmd = None
            self._maybe_write_csv(eventtime)

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

    def _query_sync(self, cmd, extra=b'', timeout=None):
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
        if cmd == CMD_VOLTAGE and len(data) >= 5:
            self.last['voltage_mv'] = _u16(data, 2)
        elif cmd == CMD_CURRENT and len(data) >= 5:
            self.last['current_ma'] = _u16(data, 2)
        elif cmd == CMD_ENCODER and len(data) >= 5:
            self.last['encoder_counts'] = _u16(data, 2)
        elif cmd == CMD_INPUT_PULSES and len(data) >= 8:
            pulses = _signed_value(data[2], _u32(data, 3))
            self.last['input_pulses'] = pulses
            ppr = float(self.microsteps * self.full_steps_per_rotation)
            self.last['input_pulses_mm'] = pulses * self.rotation_distance / ppr
        elif cmd == CMD_TARGET_POS and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['target_counts'] = counts
            self.last['target_deg'] = self._counts_to_deg(counts)
            self.last['target_mm'] = self._counts_to_mm(counts)
        elif cmd == CMD_REALTIME_TARGET and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['realtime_target_counts'] = counts
            self.last['realtime_target_deg'] = self._counts_to_deg(counts)
            self.last['realtime_target_mm'] = self._counts_to_mm(counts)
        elif cmd == CMD_RPM and len(data) >= 6:
            self.last['rpm'] = _signed_value(data[2], _u16(data, 3))
        elif cmd == CMD_REAL_POS and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['actual_counts'] = counts
            self.last['actual_deg'] = self._counts_to_deg(counts)
            self.last['actual_mm'] = self._counts_to_mm(counts)
        elif cmd == CMD_POS_ERROR and len(data) >= 8:
            counts = _signed_value(data[2], _u32(data, 3))
            self.last['error_counts'] = counts
            self.last['error_deg'] = self._counts_to_deg(counts)
            self.last['error_mm'] = self._counts_to_mm(counts)
        elif cmd == CMD_MOTOR_FLAGS and len(data) >= 4:
            flags = data[2]
            self.last['motor_flags'] = flags
            self.last['enabled'] = bool(flags & 0x01)
            self.last['reached'] = bool(flags & 0x02)
            self.last['stalled'] = bool(flags & 0x04)
            self.last['stall_protect'] = bool(flags & 0x08)
        elif cmd == CMD_HOME_FLAGS and len(data) >= 4:
            flags = data[2]
            self.last['home_flags'] = flags
            self.last['encoder_ready'] = bool(flags & 0x01)
            self.last['calibration_ready'] = bool(flags & 0x02)
            self.last['homing'] = bool(flags & 0x04)
            self.last['home_failed'] = bool(flags & 0x08)
        self.last['csv_logging'] = self.csv_logging

    def _counts_to_deg(self, counts):
        return counts * 360.0 / 65536.0

    def _counts_to_mm(self, counts):
        return counts * self.rotation_distance / 65536.0

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
        status = dict(self.last)
        status['error_count'] = self.error_count
        status['last_error'] = self.last_error
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
        data = self._query_sync(cmd, extra, self.query_timeout)
        if data is None:
            raise gcmd.error("No valid response for cmd 0x%02X: %s" % (cmd, self.last_error))
        self._parse_response(cmd, data)
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
        self.enabled = bool(enable)
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._poll_timer)
        if not self.enabled:
            self.pending_cmd = None
        self.reactor.update_timer(self.timer, self.reactor.NOW if self.enabled else self.reactor.NEVER)
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
