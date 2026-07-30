# Pure ZDT EMM42 V5 protocol primitives shared by transport-specific links.

import struct

try:
    from . import closed_loop_motor_protocol as protocol_contract
except ImportError:
    import closed_loop_motor_protocol as protocol_contract

ProtocolError = protocol_contract.ProtocolError


CMD_READ_PID = 0x21
CMD_WRITE_PID = 0x4A
CMD_READ_CONFIG = 0x42
CMD_WRITE_CONFIG = 0x48
CMD_VOLTAGE = 0x24
CMD_CURRENT = 0x27
CMD_ENCODER = 0x31
CMD_INPUT_PULSES = 0x32
CMD_TARGET_POS = 0x33
CMD_REALTIME_TARGET = 0x34
CMD_RPM = 0x35
CMD_REAL_POS = 0x36
CMD_POS_ERROR = 0x37
CMD_MOTOR_FLAGS = 0x3A
CMD_HOME_FLAGS = 0x3B

READ_COMMANDS = (
    CMD_VOLTAGE, CMD_CURRENT, CMD_ENCODER, CMD_INPUT_PULSES,
    CMD_TARGET_POS, CMD_REALTIME_TARGET, CMD_RPM, CMD_REAL_POS,
    CMD_POS_ERROR, CMD_MOTOR_FLAGS, CMD_HOME_FLAGS,
)


PID_MIN_VALUE = 0
PID_MAX_VALUE = 0xFFFFFFFF
PID_RESPONSE_LEN = 15
CONFIG_RESPONSE_LEN = 33
CONFIG_DATA_LEN = 28

DRIVER_CONFIG_LAYOUT = {
    'motor_type': (0, 1),
    'pulse_mode': (1, 1),
    'serial_mode': (2, 1),
    'en_level': (3, 1),
    'dir': (4, 1),
    'microsteps': (5, 1),
    'interpolation': (6, 1),
    'auto_screen_off': (7, 1),
    'open_loop_current_ma': (8, 2),
    'stall_max_current_ma': (10, 2),
    'max_output_mv': (12, 2),
    'serial_baud_index': (14, 1),
    'can_baud_index': (15, 1),
    'device_address': (16, 1),
    'checksum_index': (17, 1),
    'response_mode': (18, 1),
    'stall_protection': (19, 1),
    'stall_rpm': (20, 2),
    'stall_current_ma': (22, 2),
    'stall_time_ms': (24, 2),
    'position_window_tenths': (26, 2),
}


def u16(data, index):
    return (data[index] << 8) | data[index + 1]


def u32(data, index):
    return ((data[index] << 24) | (data[index + 1] << 16) |
            (data[index + 2] << 8) | data[index + 3])


def pack_u32(value):
    value = int(value)
    if value < PID_MIN_VALUE or value > PID_MAX_VALUE:
        raise ValueError('PID value must be in the range 0..0xFFFFFFFF')
    return struct.pack('>I', value)


def signed_value(sign_byte, raw):
    return -int(raw) if int(sign_byte) == 0x01 else int(raw)


def parse_driver_config(data):
    data = bytearray(data)
    if (len(data) != CONFIG_RESPONSE_LEN or data[1] != 0x42 or
            data[2] != CONFIG_RESPONSE_LEN or data[3] != 0x15):
        raise ValueError('invalid 0x42 driver configuration response')
    raw = bytearray(data[4:-1])
    if len(raw) != CONFIG_DATA_LEN:
        raise ValueError('invalid driver configuration payload length')
    values = {}
    for name, (offset, size) in DRIVER_CONFIG_LAYOUT.items():
        value = raw[offset] if size == 1 else u16(raw, offset)
        if name == 'microsteps' and value == 0:
            value = 256
        values[name] = value
    return values, raw


def patch_driver_config(raw, updates):
    raw = bytearray(raw)
    if len(raw) != CONFIG_DATA_LEN:
        raise ValueError('driver configuration must contain 28 bytes')
    for name, value in updates.items():
        if name not in DRIVER_CONFIG_LAYOUT:
            raise ValueError("unknown driver configuration field '%s'" % name)
        offset, size = DRIVER_CONFIG_LAYOUT[name]
        value = int(value)
        if name == 'microsteps' and value == 256:
            value = 0
        if size == 1:
            if value < 0 or value > 0xFF:
                raise ValueError("driver field '%s' must fit one byte" % name)
            raw[offset] = value
        else:
            if value < 0 or value > 0xFFFF:
                raise ValueError("driver field '%s' must fit two bytes" % name)
            raw[offset] = (value >> 8) & 0xFF
            raw[offset + 1] = value & 0xFF
    return raw


class ZdtEmm42V5Protocol(protocol_contract.DeviceProtocolAdapter):
    profile = 'zdt.emm42_v5'
    settings_profile = 'zdt.emm42_v5'
    settings_profile_version = 1

    def __init__(self, address, checksum_mode='fixed', check_byte=0x6B):
        self.address = int(address)
        self.checksum_mode = checksum_mode
        self.check_byte = int(check_byte) & 0xFF

    @staticmethod
    def crc8(data):
        crc = 0
        for value in data:
            crc ^= value
            for _ in range(8):
                crc = (((crc << 1) ^ 0x07) if crc & 0x80 else
                       (crc << 1)) & 0xFF
        return crc

    def checksum(self, logical_bytes):
        if self.checksum_mode == 'xor':
            value = 0
            for byte in logical_bytes:
                value ^= byte
            return value & 0xFF
        if self.checksum_mode == 'crc8':
            return self.crc8(logical_bytes)
        return self.check_byte

    def verify_checksum(self, normalized):
        return (len(normalized) >= 2 and
                normalized[-1] == self.checksum(normalized[:-1]))

    def poll_requests(self):
        return tuple(
            protocol_contract.ProtocolRequest(self.address, command)
            for command in READ_COMMANDS)

    def decode(self, frame):
        if not isinstance(frame, protocol_contract.ProtocolFrame):
            raise protocol_contract.ProtocolError(
                'ZDT decoder requires a ProtocolFrame')
        if frame.address != self.address:
            raise protocol_contract.ProtocolError(
                'ZDT response address does not match this device')
        normalized = bytearray([frame.address, frame.command])
        normalized.extend(frame.payload)
        if not self.verify_checksum(normalized):
            raise protocol_contract.ProtocolError(
                'ZDT response checksum is invalid')
        if frame.command == CMD_READ_CONFIG:
            try:
                values, raw = parse_driver_config(normalized)
            except ValueError as exc:
                raise protocol_contract.ProtocolError(str(exc))
            return {
                'kind': 'settings', 'values': values,
                'raw': bytes(raw),
            }
        if frame.command == CMD_READ_PID:
            if len(normalized) != PID_RESPONSE_LEN:
                raise protocol_contract.ProtocolError(
                    'invalid ZDT position PID response length')
            return {
                'kind': 'position_pid',
                'kp': u32(normalized, 2),
                'ki': u32(normalized, 6),
                'kd': u32(normalized, 10),
            }
        if frame.command in (CMD_WRITE_CONFIG, CMD_WRITE_PID):
            if len(normalized) != 4:
                raise protocol_contract.ProtocolError(
                    'invalid ZDT write response length')
            return {
                'kind': ('settings_write' if
                         frame.command == CMD_WRITE_CONFIG else 'pid_write'),
                'status': normalized[2],
            }
        scalar_fields = {
            CMD_VOLTAGE: 'voltage_mv',
            CMD_CURRENT: 'current_ma',
            CMD_ENCODER: 'encoder_counts',
        }
        if frame.command in scalar_fields:
            if len(normalized) != 5:
                raise protocol_contract.ProtocolError(
                    'invalid ZDT scalar response length')
            return {
                'kind': 'scalar',
                'field': scalar_fields[frame.command],
                'value': u16(normalized, 2),
            }
        signed_fields = {
            CMD_INPUT_PULSES: 'input_pulses',
            CMD_TARGET_POS: 'target_counts',
            CMD_REALTIME_TARGET: 'realtime_target_counts',
            CMD_REAL_POS: 'actual_counts',
            CMD_POS_ERROR: 'error_counts',
        }
        if frame.command in signed_fields:
            if len(normalized) != 8:
                raise protocol_contract.ProtocolError(
                    'invalid ZDT signed-position response length')
            return {
                'kind': 'signed_position',
                'field': signed_fields[frame.command],
                'value': signed_value(normalized[2], u32(normalized, 3)),
            }
        if frame.command == CMD_RPM:
            if len(normalized) != 6:
                raise protocol_contract.ProtocolError(
                    'invalid ZDT RPM response length')
            return {
                'kind': 'scalar', 'field': 'rpm',
                'value': signed_value(normalized[2], u16(normalized, 3)),
            }
        if frame.command in (CMD_MOTOR_FLAGS, CMD_HOME_FLAGS):
            if len(normalized) != 4:
                raise protocol_contract.ProtocolError(
                    'invalid ZDT flag response length')
            return {
                'kind': ('motor_flags' if
                         frame.command == CMD_MOTOR_FLAGS else 'home_flags'),
                'flags': normalized[2],
            }
        return {
            'kind': 'response', 'command': frame.command,
            'data': bytes(normalized[2:-1]),
        }

    def decode_normalized(self, normalized):
        normalized = bytes(normalized)
        if len(normalized) < 3:
            raise protocol_contract.ProtocolError(
                'ZDT response is too short')
        return self.decode(protocol_contract.ProtocolFrame(
            normalized[0], normalized[1], normalized[2:]))


class ZdtCanLinkCodec(protocol_contract.LinkCodec):
    profile = 'zdt.emm42_v5.can'

    def __init__(self, protocol, payload_includes_address=False):
        self.protocol = protocol
        self.payload_includes_address = bool(payload_includes_address)

    def encode_short(self, command, extra=b''):
        logical = bytearray([
            self.protocol.address & 0xFF, int(command) & 0xFF])
        logical.extend(extra)
        check = self.protocol.checksum(logical)
        payload = (bytes(logical) if self.payload_includes_address else
                   bytes(logical[1:])) + bytes([check])
        return [(0, payload)]

    def encode(self, request):
        if not isinstance(request, protocol_contract.ProtocolRequest):
            raise protocol_contract.ProtocolError(
                'ZDT CAN encoder requires a ProtocolRequest')
        if request.address != self.protocol.address:
            raise protocol_contract.ProtocolError(
                'ZDT request address does not match this CAN codec')
        if request.metadata.get('long'):
            return self.encode_long(request.command, request.payload)
        return self.encode_short(request.command, request.payload)

    def feed(self, unit):
        try:
            frame_id, payload = unit
        except (TypeError, ValueError):
            raise protocol_contract.ProtocolError(
                'ZDT CAN unit must be a (frame_id, payload) pair')
        payload = bytes(payload)
        if not payload:
            return []
        address = (int(frame_id) >> 8) & 0xFF
        packet_no = int(frame_id) & 0xFF
        offset = (1 if self.payload_includes_address and
                  payload[0] == address and len(payload) > 1 else 0)
        command = payload[offset]
        return [protocol_contract.ProtocolFrame(
            address, command, payload[offset + 1:],
            {'packet_no': packet_no})]

    def encode_long(self, command, extra=b''):
        if self.payload_includes_address:
            raise ValueError(
                'long CAN commands require payload_includes_address=False')
        command = int(command) & 0xFF
        logical = bytearray([self.protocol.address & 0xFF, command])
        logical.extend(extra)
        tail = bytearray(extra)
        tail.append(self.protocol.checksum(logical))
        packets = []
        packet_no = 0
        while tail:
            chunk = tail[:7]
            del tail[:7]
            packets.append((packet_no, bytes([command]) + bytes(chunk)))
            packet_no += 1
        return packets

    def normalize_response(self, data, command):
        data = bytearray(data)
        address = self.protocol.address
        if (len(data) >= 2 and data[0] == address and
                data[1] in (int(command) & 0xFF, 0x00)):
            return data
        return bytearray([address]) + data

    def normalize_long_packet(self, data, command):
        data = bytearray(data)
        if (len(data) >= 2 and data[0] == self.protocol.address and
                data[1] in (int(command) & 0xFF, 0x00)):
            return data[1:]
        return data
