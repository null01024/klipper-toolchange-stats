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

import logging

# 启动后给 buttons helper 多少秒来自然上报第一帧；超过后仍未知则记录日志。
BOOT_GRACE_S = 2.0


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
        # None 表示尚未收到 buttons 上报；不能当成 RELEASED 放行。
        self._raw = {'X': None, 'Y': None}

        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([self.x_diag_pin], self._make_callback('X'))
        buttons.register_buttons([self.y_diag_pin], self._make_callback('Y'))

        self.printer.register_event_handler('klippy:ready', self._on_ready)

        self.gcode.register_command(
            'QUERY_XY_GUARD_STATUS', self.cmd_QUERY_XY_GUARD_STATUS,
            desc='查询换热端过程 XY 防撞检测状态')

    def _on_ready(self):
        self.reactor.register_callback(
            self._check_initial_report,
            self.reactor.monotonic() + BOOT_GRACE_S)

    def _check_initial_report(self, _eventtime):
        unknown = [axis for axis in ('X', 'Y') if self._raw[axis] is None]
        if unknown:
            logging.warning(
                "multitool_xy_guard: 启动 %.1fs 后仍未收到 DIAG buttons "
                "状态上报 (axis=%s, x_pin=%s, y_pin=%s)；换头检测窗口内"
                "若状态仍未知，将中断换头。请检查 pin 接线 / 电平修饰符。",
                BOOT_GRACE_S, ','.join(unknown),
                self.x_diag_pin, self.y_diag_pin)

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
        self.gcode.run_script_from_command("M400")
        if self._fault_axis is None:
            if self.settle_ms > 0:
                self.gcode.run_script_from_command("G4 P%d" % self.settle_ms)
            unknown = [axis for axis in ('X', 'Y')
                       if self._raw[axis] is None]
            if unknown:
                msg = ("XY 防撞检测失败 (原因=%s) DIAG状态未知=%s；"
                       "请检查 DIAG pin 接线 / 电平修饰符 (! ^ ~)，"
                       "或确认 buttons helper 是否上报初始状态。"
                       % (reason, ','.join(unknown)))
                self.gcode.respond_info(msg)
                raise self.printer.command_error(msg)
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
    def _raw_text(self, axis):
        raw = self._raw[axis]
        if raw is None:
            return 'UNKNOWN'
        return 'PRESSED' if raw else 'RELEASED'

    def cmd_QUERY_XY_GUARD_STATUS(self, gcmd):
        gcmd.respond_info("====== XY 防撞检测状态 ======")
        gcmd.respond_info("启用: 是")
        gcmd.respond_info("当前检测窗口: %s"
                          % (self._stage if self._armed else "未启用"))
        gcmd.respond_info(
            "当前 DIAG: X=%s Y=%s"
            % (self._raw_text('X'), self._raw_text('Y')))
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
                'x': self._raw_text('X'),
                'y': self._raw_text('Y'),
            },
            'last_fault': {
                'axis': self._last_axis,
                'stage': self._last_stage,
                'time': self._last_time,
            },
        }


def load_config(config):
    return MultitoolXYGuard(config)
