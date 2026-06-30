#!/usr/bin/env python3
# Klipper Multitool - XY 防撞检测子模块
#
# 职责：
#   - 监听 X/Y TMC DIAG 状态
#   - 在换热端 release / pickup 钩子前后读取 DIAG 状态
#   - 提供 assert_ok(reason) 给主流程用 command_error 中断当前换头
#
# 约定：
#   - 直接读取 [tmc2209 stepper_x/y] 的 IOIN.diag
#   - TMC UART 读取会等待 MCU 返回，只能在 gcode 命令上下文执行；
#     不要在 klippy:ready 或 reactor timer callback 中读取寄存器。
#   - StallGuard 阈值由 [tmc* stepper_x/y] 的驱动参数设置

import logging


class MultitoolXYGuard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()

        self.x_tmc_name = config.get('x_tmc', 'tmc2209 stepper_x')
        self.y_tmc_name = config.get('y_tmc', 'tmc2209 stepper_y')
        self._validate_tmc_config(config, 'X', self.x_tmc_name)
        self._validate_tmc_config(config, 'Y', self.y_tmc_name)
        self.settle_ms = config.getint('settle_ms', 20, minval=0)
        self.poll_ms = config.getint('poll_ms', 20, minval=5)
        self.action = config.get('action', 'pause')
        if self.action != 'pause':
            raise config.error(
                "multitool_xy_guard action 目前只支持 pause")

        self._armed = False
        self._stage = None
        self._fault_axis = None
        self._fault_stage = None
        self._fault_time = None
        self._fault_tool = -1
        self._last_axis = None
        self._last_stage = None
        self._last_time = None
        self._last_tool = -1
        self._tmc_error = None
        self._tmc_restore = {}
        self._action_sent = False
        self._raw = {'X': False, 'Y': False}
        self._tmc = {}

        self.printer.register_event_handler('klippy:ready', self._on_ready)

        self.gcode.register_command(
            'QUERY_XY_GUARD_STATUS', self.cmd_QUERY_XY_GUARD_STATUS,
            desc='查询换热端过程 XY 防撞检测状态')

    def _validate_tmc_config(self, config, axis, name):
        if not config.has_section(name):
            raise config.error(
                "multitool_xy_guard: 未找到 %s 轴 TMC 配置段 [%s]"
                % (axis, name))
        tmc_config = config.getsection(name)
        if tmc_config.get('diag_pin', None) is None:
            raise config.error(
                "multitool_xy_guard: [%s] 必须配置 diag_pin" % (name,))
        if tmc_config.get('driver_SGTHRS', None) is None:
            raise config.error(
                "multitool_xy_guard: [%s] 必须配置 driver_SGTHRS" % (name,))

    def _on_ready(self):
        self._tmc = {
            'X': self._lookup_tmc('X', self.x_tmc_name),
            'Y': self._lookup_tmc('Y', self.y_tmc_name),
        }

    def _lookup_tmc(self, axis, name):
        obj = self.printer.lookup_object(name, None)
        if obj is None:
            raise self.printer.config_error(
                "multitool_xy_guard: 未找到 %s 轴 TMC 对象 [%s]"
                % (axis, name))
        mcu_tmc = getattr(obj, 'mcu_tmc', None)
        fields = getattr(obj, 'fields', None)
        if mcu_tmc is None or fields is None:
            raise self.printer.config_error(
                "multitool_xy_guard: [%s] 不暴露 mcu_tmc/fields，"
                "无法读取 IOIN.diag" % (name,))
        if fields.lookup_register('diag', None) != 'IOIN':
            raise self.printer.config_error(
                "multitool_xy_guard: [%s] 不支持 IOIN.diag" % (name,))
        return {'name': name, 'obj': obj, 'mcu_tmc': mcu_tmc, 'fields': fields}

    def _set_fault(self, axis, eventtime):
        self._fault_axis = axis
        self._fault_stage = self._stage
        self._fault_time = eventtime
        self._fault_tool = self._current_tool()
        self._last_axis = axis
        self._last_stage = self._stage
        self._last_time = eventtime
        self._last_tool = self._fault_tool
        self._handle_fault_action()

    def _current_tool(self):
        tc = self.printer.lookup_object('multitool', None)
        if tc is None:
            return -1
        return tc.current_tool

    def _tool_text(self, tool):
        return "无热端" if tool == -1 else "T%d" % tool

    def _fault_text(self, reason):
        return ("XY 防撞检测失败 (原因=%s) 触发轴=%s 阶段=%s 当前热端=%s"
                % (reason, self._fault_axis, self._fault_stage,
                   self._tool_text(self._fault_tool)))

    def _handle_fault_action(self):
        if self._action_sent:
            return
        self._action_sent = True
        msg = self._fault_text('StallGuard触发')
        self.gcode.respond_info(msg)
        if self.action == 'pause':
            try:
                self.gcode.run_script_from_command("PAUSE")
            except Exception:
                logging.exception(
                    "multitool_xy_guard: 触发后执行 PAUSE 失败")

    # ------------------------------------------------------------------
    # 公共方法：被 multitool 主流程调用
    # ------------------------------------------------------------------
    def _read_tmc_diag(self, axis):
        info = self._tmc[axis]
        reg = info['mcu_tmc'].get_register('IOIN')
        return bool(info['fields'].get_field('diag', reg, 'IOIN'))

    def _set_tmc_field(self, axis, field_name, value):
        info = self._tmc[axis]
        fields = info['fields']
        reg_name = fields.lookup_register(field_name, None)
        if reg_name is None:
            return False
        key = (axis, field_name)
        if key not in self._tmc_restore:
            self._tmc_restore[key] = fields.get_field(field_name)
        reg_val = fields.set_field(field_name, value)
        info['mcu_tmc'].set_register(reg_name, reg_val)
        return True

    def _prepare_tmc_stallguard(self):
        self._tmc_restore = {}
        for axis in ('X', 'Y'):
            # TMC2209 StallGuard 需要 spreadCycle，且 TCOOLTHRS 非 0 才会工作。
            self._set_tmc_field(axis, 'en_spreadcycle', 1)
            self._set_tmc_field(axis, 'en_pwm_mode', 0)
            self._set_tmc_field(axis, 'tpwmthrs', 0)
            self._set_tmc_field(axis, 'tcoolthrs', 0xfffff)
            self._set_tmc_field(axis, 'thigh', 0)

    def _restore_tmc_stallguard(self):
        if not self._tmc_restore:
            return
        for axis, field_name in reversed(list(self._tmc_restore.keys())):
            value = self._tmc_restore[(axis, field_name)]
            info = self._tmc[axis]
            fields = info['fields']
            reg_name = fields.lookup_register(field_name, None)
            if reg_name is None:
                continue
            reg_val = fields.set_field(field_name, value)
            info['mcu_tmc'].set_register(reg_name, reg_val)
        self._tmc_restore = {}

    def _sample_tmc(self):
        eventtime = self.reactor.monotonic()
        for axis in ('X', 'Y'):
            pressed = self._read_tmc_diag(axis)
            self._raw[axis] = pressed
            if self._armed and pressed and self._fault_axis is None:
                self._set_fault(axis, eventtime)

    def _start_tmc_window(self):
        self._tmc_error = None
        self._action_sent = False
        self._prepare_tmc_stallguard()
        self._sample_tmc()

    def _stop_tmc_window(self):
        self._restore_tmc_stallguard()

    def arm(self, stage):
        self._armed = True
        self._stage = stage
        self._fault_axis = None
        self._fault_stage = None
        self._fault_time = None
        self._fault_tool = -1

        try:
            self._start_tmc_window()
        except Exception:
            self._armed = False
            self._stop_tmc_window()
            raise
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
        self._fault_tool = -1
        self._stop_tmc_window()

    def assert_ok(self, reason=''):
        self.gcode.run_script_from_command("M400")
        try:
            self._sample_tmc()
        except Exception as e:
            self._tmc_error = str(e)
        if self._tmc_error is not None:
            msg = ("XY 防撞检测失败 (原因=%s) TMC DIAG 查询错误: %s"
                   % (reason, self._tmc_error))
            self.gcode.respond_info(msg)
            raise self.printer.command_error(msg)
        if self._fault_axis is None:
            if self.settle_ms > 0:
                self.gcode.run_script_from_command("G4 P%d" % self.settle_ms)
            if self._fault_axis is None:
                return

        msg = self._fault_text(reason)
        self.gcode.respond_info(msg)
        raise self.printer.command_error(msg)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def _raw_text(self, axis):
        raw = self._raw[axis]
        return 'PRESSED' if raw else 'RELEASED'

    def cmd_QUERY_XY_GUARD_STATUS(self, gcmd):
        if self._tmc:
            try:
                self._sample_tmc()
            except Exception as e:
                self._tmc_error = str(e)
                logging.exception("multitool_xy_guard: TMC DIAG 查询失败")
        gcmd.respond_info("====== XY 防撞检测状态 ======")
        gcmd.respond_info("启用: 是")
        gcmd.respond_info(
            "TMC: X=%s Y=%s mode=gcode-snapshot poll=%dms action=%s"
            % (self.x_tmc_name, self.y_tmc_name,
               self.poll_ms, self.action))
        gcmd.respond_info("当前检测窗口: %s"
                          % (self._stage if self._armed else "未启用"))
        gcmd.respond_info(
            "当前 DIAG: X=%s Y=%s"
            % (self._raw_text('X'), self._raw_text('Y')))
        if self._tmc_error is not None:
            gcmd.respond_info("最近 TMC 查询错误: %s" % self._tmc_error)
        if self._last_axis is None:
            gcmd.respond_info("最近触发: 无")
        else:
            gcmd.respond_info(
                "最近触发: 轴=%s 阶段=%s 当前热端=%s time=%.3f"
                % (self._last_axis, self._last_stage,
                   self._tool_text(self._last_tool), self._last_time))

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
                'tool': self._last_tool,
            },
        }


def load_config(config):
    return MultitoolXYGuard(config)
