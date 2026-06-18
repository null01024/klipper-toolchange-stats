#!/usr/bin/env python3
# Klipper Multitool - XY 防撞检测子模块
#
# 职责：
#   - 监听 X/Y TMC DIAG 引脚
#   - 仅在换热端 release / pickup 钩子运行期间记录 DIAG 触发
#   - 提供 assert_ok(reason) 给主流程用 command_error 中断当前换头
#
# 约定：
#   - DIAG 触发 -> buttons helper 上报 PRESSED
#   - 若实际电平相反，用户通过 pin 的 ! 修饰符反相
#   - StallGuard 阈值由 [tmc* stepper_x/y] 的驱动参数设置


class MultitoolXYGuard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()

        self.x_diag_pin = config.get('x_diag_pin')
        self.y_diag_pin = config.get('y_diag_pin')
        self.settle_ms = config.getint('settle_ms', 20, minval=0)

        self._armed = False
        self._stage = None
        self._fault_axis = None
        self._fault_stage = None
        self._fault_time = None
        self._last_axis = None
        self._last_stage = None
        self._last_time = None
        self._raw = {'X': False, 'Y': False}

        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([self.x_diag_pin], self._make_callback('X'))
        buttons.register_buttons([self.y_diag_pin], self._make_callback('Y'))

        self.gcode.register_command(
            'QUERY_XY_GUARD_STATUS', self.cmd_QUERY_XY_GUARD_STATUS,
            desc='查询换热端过程 XY 防撞检测状态')

    def _make_callback(self, axis):
        def _callback(eventtime, state):
            pressed = bool(state)
            self._raw[axis] = pressed
            if self._armed and pressed and self._fault_axis is None:
                self._set_fault(axis, eventtime)
        return _callback

    def _set_fault(self, axis, eventtime):
        self._fault_axis = axis
        self._fault_stage = self._stage
        self._fault_time = eventtime
        self._last_axis = axis
        self._last_stage = self._stage
        self._last_time = eventtime

    # ------------------------------------------------------------------
    # 公共方法：被 multitool 主流程调用
    # ------------------------------------------------------------------
    def arm(self, stage):
        self._armed = True
        self._stage = stage
        self._fault_axis = None
        self._fault_stage = None
        self._fault_time = None

        eventtime = self.reactor.monotonic()
        for axis in ('X', 'Y'):
            if self._raw.get(axis):
                self._set_fault(axis, eventtime)
                break

    def disarm(self):
        self._armed = False
        self._stage = None
        self._fault_axis = None
        self._fault_stage = None
        self._fault_time = None

    def assert_ok(self, reason=''):
        if self._fault_axis is None:
            if self.settle_ms > 0:
                self.gcode.run_script_from_command("G4 P%d" % self.settle_ms)
            if self._fault_axis is None:
                return

        tc = self.printer.lookup_object('multitool', None)
        cur = tc.current_tool if tc is not None else -1
        cur_cn = "无热端" if cur == -1 else "T%d" % cur
        msg = ("XY 防撞检测失败 (原因=%s) 触发轴=%s 阶段=%s 当前热端=%s"
               % (reason, self._fault_axis, self._fault_stage, cur_cn))
        self.gcode.respond_info(msg)
        raise self.printer.command_error(msg)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def cmd_QUERY_XY_GUARD_STATUS(self, gcmd):
        gcmd.respond_info("====== XY 防撞检测状态 ======")
        gcmd.respond_info("启用: 是")
        gcmd.respond_info("当前检测窗口: %s"
                          % (self._stage if self._armed else "未启用"))
        gcmd.respond_info(
            "当前 DIAG: X=%s Y=%s"
            % (["RELEASED", "PRESSED"][self._raw['X']],
               ["RELEASED", "PRESSED"][self._raw['Y']]))
        if self._last_axis is None:
            gcmd.respond_info("最近触发: 无")
        else:
            gcmd.respond_info(
                "最近触发: 轴=%s 阶段=%s time=%.3f"
                % (self._last_axis, self._last_stage, self._last_time))

    # ------------------------------------------------------------------
    # 暴露给前端 / 宏
    # ------------------------------------------------------------------
    def get_status(self, eventtime):
        return {
            'armed': self._armed,
            'stage': self._stage,
            'raw': {
                'x': 'PRESSED' if self._raw['X'] else 'RELEASED',
                'y': 'PRESSED' if self._raw['Y'] else 'RELEASED',
            },
            'last_fault': {
                'axis': self._last_axis,
                'stage': self._last_stage,
                'time': self._last_time,
            },
        }


def load_config(config):
    return MultitoolXYGuard(config)
