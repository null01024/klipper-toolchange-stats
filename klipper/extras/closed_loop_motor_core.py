# Shared registry and public identity helpers for closed-loop motors.

import math


SCHEMA_VERSION = 1
MOTOR_OBJECT_TYPE = 'closed_loop_motor'
GROUP_OBJECT_TYPE = 'closed_loop_motor_group'

ZDT_VENDOR = 'zdt'
ZDT_MODEL = 'emm42_v5'
CAN_TRANSPORT = 'can'

SUPPORTED_COMBINATIONS = (
    (ZDT_VENDOR, ZDT_MODEL, CAN_TRANSPORT),
)

MOTOR_CAPABILITIES = (
    'telemetry',
    'position_error',
    'motor_state',
    'homing_state',
    'pid_read',
    'pid_write',
    'settings_read',
    'settings_write',
    'settings_store',
    'autotune_single',
    'autotune_group',
    'csv_logging',
    'raw_query',
    'transport_sniff',
)

GROUP_MEMBER_API = (
    'closed_loop_identity',
    'closed_loop_name',
    'closed_loop_object_name',
    'closed_loop_capabilities',
    'transport_address',
    'closed_loop_status',
    'closed_loop_last_error',
    'closed_loop_autotune_active',
    'refresh_position_error_status',
    'refresh_motor_state',
    'read_position_pid',
    'write_position_pid',
    'begin_position_capture',
    'set_position_capture_phase',
    'end_position_capture',
    'consume_position_capture_violation',
    'prepare_group_capture',
    'restore_group_capture',
    'position_pid_bounds',
    'position_pid_steps',
    'request_autotune_cancel',
    'latest_position_error_sample',
    'validate_group_autotune_configuration',
)


def motor_object_name(name):
    return '%s %s' % (MOTOR_OBJECT_TYPE, name)


def validate_group_member(member, group_type='three_z'):
    required_api = GROUP_MEMBER_API + (
        ('corexy_test_route',) if group_type == 'corexy' else ())
    missing = [name for name in required_api
               if not callable(getattr(member, name, None))]
    if missing:
        raise ValueError(
            'adapter does not implement group capabilities: %s' %
            ', '.join(missing))
    capabilities = set(member.closed_loop_capabilities())
    required = {'position_error', 'pid_read', 'pid_write',
                'autotune_group'}
    missing_capabilities = sorted(required - capabilities)
    if missing_capabilities:
        raise ValueError(
            'adapter does not support group capabilities: %s' %
            ', '.join(missing_capabilities))


def normalize_position_error_sample(sample):
    """Validate the adapter-neutral position sample schema.

    Time and angular error are required. Linear error is optional because an
    adapter may not know the mechanism's rotation distance.
    """
    if not isinstance(sample, dict):
        return None
    try:
        eventtime = float(sample['time'])
        error_deg = float(sample['error_deg'])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(eventtime) or not math.isfinite(error_deg):
        return None
    normalized = dict(sample)
    normalized['time'] = eventtime
    normalized['error_deg'] = error_deg
    try:
        error_mm = float(sample.get('error_mm'))
    except (TypeError, ValueError):
        error_mm = None
    normalized['error_mm'] = (
        error_mm if error_mm is not None and math.isfinite(error_mm)
        else None)
    return normalized


def _identity(config):
    vendor = str(config.get('vendor', '')).strip().lower()
    model = str(config.get('model', '')).strip().lower()
    transport = str(config.get('transport', '')).strip().lower()
    identity = (vendor, model, transport)
    if identity not in SUPPORTED_COMBINATIONS:
        supported = ', '.join('/'.join(value)
                              for value in SUPPORTED_COMBINATIONS)
        raise config.error(
            "closed_loop_motor: unsupported vendor/model/transport "
            "combination '%s/%s/%s' (implemented: %s)" %
            (vendor or '?', model or '?', transport or '?', supported))
    return identity


def create_motor(config):
    vendor, model, transport = _identity(config)
    if (vendor, model, transport) == (
            ZDT_VENDOR, ZDT_MODEL, CAN_TRANSPORT):
        try:
            from . import closed_loop_motor_zdt_emm42_v5_can as adapter
        except ImportError:
            import closed_loop_motor_zdt_emm42_v5_can as adapter
        return adapter.ZdtEmm42V5Can(config)
    raise config.error('closed_loop_motor: no adapter factory registered')


def create_group(config):
    group_type = str(config.get('group_type', '')).strip().lower()
    if group_type == 'three_z':
        try:
            from . import closed_loop_motor_three_z_group as group_module
        except ImportError:
            import closed_loop_motor_three_z_group as group_module
        return group_module.ClosedLoopThreeZGroup(config)
    if group_type == 'corexy':
        try:
            from . import closed_loop_motor_corexy_group as group_module
        except ImportError:
            import closed_loop_motor_corexy_group as group_module
        return group_module.ClosedLoopCoreXYGroup(config)
    raise config.error(
        "closed_loop_motor_group: group_type must be 'three_z' or "
        "'corexy'")
