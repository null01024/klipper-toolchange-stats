# Legacy configuration entry point. Group behavior lives in the generic
# closed-loop motor strategy module.

try:
    from . import closed_loop_motor_core as core
    from .closed_loop_motor_corexy_group import ClosedLoopCoreXYGroup
except ImportError:
    import closed_loop_motor_core as core
    from closed_loop_motor_corexy_group import ClosedLoopCoreXYGroup


ZdtEmm42XYGroup = ClosedLoopCoreXYGroup


def load_config_prefix(config):
    return core.register_legacy_group(config, 'corexy')
