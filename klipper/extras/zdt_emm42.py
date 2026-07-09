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
#   poll_interval: 0.10
#   query_timeout: 0.006
#   rotation_distance: 40
#   microsteps: 32
#   full_steps_per_rotation: 200
#   csv_path: /tmp/zdt_emm42_shadow_a.csv
#
# G-code:
#   ZDT_EMM_STATUS NAME=shadow_a
#   ZDT_EMM_QUERY  NAME=shadow_a CMD=0x36
#   ZDT_EMM_LOG    NAME=shadow_a ENABLE=1
#   ZDT_EMM_LOG    NAME=shadow_a ENABLE=0
#
# Notes:
# - Emm42 must be set to CAN1_MAP, extended CAN frame, and the same bus bitrate as can0.
# - This module uses Linux SocketCAN directly, not Klipper's MCU CAN protocol.
# - This first version polls short read commands only. It intentionally avoids motion commands.

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
CMD_REALTIME_TARGET = 0x34   # addr 34 sign u32 6B
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
    # Klipper gcmd values arrive as strings. Accept decimal, 0xNN, or bare hex like "36".
    if isinstance(value, int):
        return value
    value = str(value).strip()
    if value.lower().startswith("0x"):
        return int(value, 16)
    # Treat pure two-character hex commands such as "36" as hex only when letters exist
    # or the string is explicitly prefixed. Decimal is safer for config values.
    try:
        return int(value, 10)
    except ValueError:
        return int(value, 16)


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
        self.poll_interval = config.getfloat('poll_interval', 0.10, above=0.0)
        self.query_timeout = config.getfloat('query_timeout', 0.006, above=0.0)
        self.rotation_distance = config.getfloat('rotation_distance', 40.0, above=0.0)
        self.microsteps = config.getint('microsteps', 32, minval=1)
        self.full_steps_per_rotation = config.getint('full_steps_per_rotation', 200, minval=1)
        self.auto_start = config.getboolean('auto_start', True)
        self.csv_path = config.get('csv_path', '')

        self.sock = None
        self.timer = None
        self.enabled = False
        self.query_index = 0
        self.last = self._empty_status()
        self.error_count = 0
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
            'ZDT_EMM_LOG', 'NAME', self.name, self.cmd_LOG,
            desc='Enable or disable CSV logging for ZDT Emm42 monitor')
        self.gcode.register_mux_command(
            'ZDT_EMM_POLL', 'NAME', self.name, self.cmd_POLL,
            desc='Enable or disable periodic ZDT Emm42 polling')

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
        s.bind((self.can_interface,))
        self.sock = s

    def _poll_timer(self, eventtime):
        if not self.enabled:
            return self.reactor.NEVER
        try:
            # Poll one command per timer tick to avoid blocking Klipper too long.
            cmd = READ_COMMANDS[self.query_index % len(READ_COMMANDS)]
            self.query_index += 1
            data = self._query(cmd, self.query_timeout)
            if data is not None:
                self._parse_response(cmd, data, eventtime)
                self.last['online'] = True
                self.last['last_update_time'] = eventtime
                self.last_error = "ok"
                self.last['last_error'] = self.last_error
                self._maybe_write_csv(eventtime)
            else:
                self.error_count += 1
                self.last['error_count'] = self.error_count
                if self.error_count % 50 == 1:
                    self.last_error = "no response for cmd 0x%02X" % cmd
                    self.last['last_error'] = self.last_error
        except Exception as e:
            self.error_count += 1
            self.last['error_count'] = self.error_count
            self.last_error = str(e)
            self.last['last_error'] = self.last_error
            logging.exception("zdt_emm42 %s: poll failed", self.name)
        return eventtime + self.poll_interval

    def _can_id(self, packet_no=0):
        # ZDT CAN extended ID: ID_Addr left-shifted by 8 bits; low byte is packet number.
        return ((self.addr & 0xFF) << 8) | (packet_no & 0xFF)

    def _send_payload(self, payload, packet_no=0):
        if self.sock is None:
            self._open_socket()
        if len(payload) > 8:
            raise ValueError("short-frame sender only supports <=8 bytes")
        arb_id = self._can_id(packet_no) | CAN_EFF_FLAG
        frame = struct.pack(CAN_FRAME_FMT, arb_id, len(payload), payload.ljust(8, b'\x00'))
        self.sock.send(frame)

    def _recv_frame(self, timeout):
        if self.sock is None:
            return None
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return None
        frame = self.sock.recv(CAN_FRAME_SIZE)
        if len(frame) < CAN_FRAME_SIZE:
            return None
        can_id, dlc, data = struct.unpack(CAN_FRAME_FMT, frame)
        if can_id & CAN_ERR_FLAG:
            return None
        if not (can_id & CAN_EFF_FLAG):
            # Ignore Klipper standard CAN frames on the same bus.
            return None
        arb_id = can_id & CAN_EFF_MASK
        payload = data[:dlc]
        return arb_id, payload

    def _drain_rx(self):
        if self.sock is None:
            return
        # Remove stale frames from this socket's queue before a synchronous query.
        while True:
            r, _, _ = select.select([self.sock], [], [], 0)
            if not r:
                break
            try:
                self.sock.recv(CAN_FRAME_SIZE)
            except Exception:
                break

    def _query(self, cmd, timeout):
        # Send: addr + cmd + check_byte. Receive matching short response.
        self._drain_rx()
        payload = bytes([self.addr, cmd, self.check_byte])
        self._send_payload(payload, 0)
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0.0:
                return None
            frame = self._recv_frame(remain)
            if frame is None:
                return None
            arb_id, data = frame
            if (arb_id >> 8) != self.addr:
                continue
            if len(data) < 4:
                continue
            # Generic error: addr 00 EE 6B
            if data[0] == self.addr and data[1] == 0x00 and data[2] == 0xEE:
                self.last_error = "device returned EE for cmd 0x%02X" % cmd
                return None
            if data[0] == self.addr and data[1] == cmd:
                if data[-1] != self.check_byte:
                    self.last_error = "bad check byte for cmd 0x%02X" % cmd
                    return None
                return bytearray(data)

    def _parse_response(self, cmd, data, eventtime):
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
            l.get('target_counts'), l.get('target_deg'), l.get('target_mm'), l.get('rpm'),
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
            "actual=%s deg / %s mm" % (self._fmt(l.get('actual_deg')), self._fmt(l.get('actual_mm'))),
            "error=%s deg / %s mm" % (self._fmt(l.get('error_deg')), self._fmt(l.get('error_mm'))),
            "flags: enabled=%s reached=%s stalled=%s stall_protect=%s" % (
                l.get('enabled'), l.get('reached'), l.get('stalled'), l.get('stall_protect')),
            "home: encoder_ready=%s calibration_ready=%s homing=%s home_failed=%s" % (
                l.get('encoder_ready'), l.get('calibration_ready'), l.get('homing'), l.get('home_failed')),
            "errors=%d last_error=%s csv=%s" % (self.error_count, self.last_error, self.csv_logging),
        ]
        gcmd.respond_info("\n".join(lines))

    def cmd_QUERY(self, gcmd):
        cmd_s = gcmd.get('CMD')
        cmd = _parse_int(cmd_s)
        if cmd < 0 or cmd > 255:
            raise gcmd.error("CMD must be 0..255")
        data = self._query(cmd, self.query_timeout)
        if data is None:
            raise gcmd.error("No valid response for cmd 0x%02X: %s" % (cmd, self.last_error))
        self._parse_response(cmd, data, self.reactor.monotonic())
        gcmd.respond_info("0x%02X <= %s" % (cmd, ' '.join('%02X' % b for b in data)))

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
