# Canonical Klipper config entry point for closed-loop motors.

try:
    from . import closed_loop_motor_core as core
except ImportError:
    import closed_loop_motor_core as core


def load_config_prefix(config):
    return core.create_motor(config)


def load_config(config):
    raise config.error(
        '[closed_loop_motor] requires an instance name, for example '
        '[closed_loop_motor z_left]')
