# Legacy configuration entry point. Group behavior lives in the generic
# closed-loop motor strategy module.

try:
    from . import closed_loop_motor_core as core
    from .closed_loop_motor_three_z_group import ClosedLoopThreeZGroup
except ImportError:
    import closed_loop_motor_core as core
    from closed_loop_motor_three_z_group import ClosedLoopThreeZGroup


ZdtEmm42Group = ClosedLoopThreeZGroup


def load_config_prefix(config):
    return core.register_legacy_group(config, 'three_z')


def load_config(config):
    return core.register_legacy_group(config, 'three_z')
