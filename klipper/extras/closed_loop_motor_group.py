# Canonical Klipper config entry point for closed-loop motor groups.

try:
    from . import closed_loop_motor_core as core
except ImportError:
    import closed_loop_motor_core as core


def load_config_prefix(config):
    return core.create_group(config)


def load_config(config):
    raise config.error(
        '[closed_loop_motor_group] requires an instance name, for example '
        '[closed_loop_motor_group z_axis]')
