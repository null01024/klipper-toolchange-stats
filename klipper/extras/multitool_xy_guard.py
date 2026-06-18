#!/usr/bin/env python3
# Klipper Multitool - XY 防撞检测子模块
#
# 职责：
#   - 监听 X/Y TMC DIAG 引脚
#   - 仅在换热端 release / pickup 钩子运行期间记录 DIAG 触发
#   - 提供 assert_ok(reason) 给主流程用 command_error 中断当前换头
#   - 可选调试模式：打印期间定时读取 X/Y TMC 负载寄存器并写入 JSON
#
# 约定：
#   - DIAG 触发 -> buttons helper 上报 PRESSED
#   - 若实际电平相反，用户通过 pin 的 ! 修饰符反相
#   - StallGuard 阈值由 [tmc* stepper_x/y] 的驱动参数设置

import json
import logging
import os


PRINTING_STATES = ('printing',)
ENDED_STATES = ('complete', 'cancelled', 'error', 'standby')


class MultitoolXYGuard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()

        self.x_diag_pin = config.get('x_diag_pin')
        self.y_diag_pin = config.get('y_diag_pin')
        self.settle_ms = config.getint('settle_ms', 20, minval=0)
        self.load_debug = config.getboolean('load_debug', False)
        self.load_auto_record = config.getboolean('load_auto_record', True)
        self.load_sample_interval_s = config.getfloat(
            'load_sample_interval_s', 1.0, above=0.)
        self.load_output_path = os.path.expanduser(config.get(
            'load_output_path',
            '~/printer_data/config/multitool/driver.json'))
        self.x_tmc = config.get('x_tmc', 'tmc2209 stepper_x')
        self.y_tmc = config.get('y_tmc', 'tmc2209 stepper_y')
        self.load_register = config.get('load_register', 'SG_RESULT')
        self.load_value_mask = config.getint(
            'load_value_mask', 0x3ff, minval=0)
        self.load_xy_move_threshold = config.getfloat(
            'load_xy_move_threshold', 0.02, minval=0.)
        self.load_queue_time_threshold = config.getfloat(
            'load_queue_time_threshold', 0.05, minval=0.)

        self._armed = False
        self._stage = None
        self._fault_axis = None
        self._fault_stage = None
        self._fault_time = None
        self._last_axis = None
        self._last_stage = None
        self._last_time = None
        self._raw = {'X': False, 'Y': False}
        self._tmc = {'X': None, 'Y': None}
        self._recording = False
        self._record_start = 0.
        self._record_samples = []
        self._record_error = None
        self._record_timer = None
        self._last_sample_pos = None
        self._last_print_state = None

        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([self.x_diag_pin], self._make_callback('X'))
        buttons.register_buttons([self.y_diag_pin], self._make_callback('Y'))

        self.printer.register_event_handler('klippy:ready', self._on_ready)

        self.gcode.register_command(
            'QUERY_XY_GUARD_STATUS', self.cmd_QUERY_XY_GUARD_STATUS,
            desc='查询换热端过程 XY 防撞检测状态')
        self.gcode.register_command(
            'START_XY_LOAD_RECORDING', self.cmd_START_XY_LOAD_RECORDING,
            desc='开始记录 X/Y TMC StallGuard 负载数据')
        self.gcode.register_command(
            'STOP_XY_LOAD_RECORDING', self.cmd_STOP_XY_LOAD_RECORDING,
            desc='停止记录 X/Y TMC StallGuard 负载数据并写入 JSON')

    def _on_ready(self):
        self._tmc['X'] = self.printer.lookup_object(self.x_tmc, None)
        self._tmc['Y'] = self.printer.lookup_object(self.y_tmc, None)
        if self.load_debug and self.load_auto_record:
            self.reactor.register_timer(
                self._poll_print_state,
                self.reactor.monotonic() + 1.0)

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

    def _poll_print_state(self, eventtime):
        try:
            ps = self.printer.lookup_object('print_stats', None)
            cur = None
            if ps is not None:
                cur = ps.get_status(eventtime).get('state')
            prev = self._last_print_state
            if cur != prev:
                self._last_print_state = cur
                if cur in PRINTING_STATES and prev != 'paused':
                    self._start_recording(eventtime, reason='print_start')
                elif self._recording and cur in ENDED_STATES:
                    self._stop_recording(eventtime, reason='print_%s' % cur)
        except Exception:
            logging.exception("multitool_xy_guard: poll print state error")
        return eventtime + 1.0

    def _start_recording(self, eventtime, reason='manual'):
        if not self.load_debug:
            raise self.printer.command_error(
                "[multitool_xy_guard] load_debug 未启用，不能记录 TMC 负载。")
        if self._recording:
            self.gcode.respond_info("[XY 负载记录] 已在记录中。")
            return
        self._recording = True
        self._record_start = eventtime
        self._record_samples = []
        self._record_error = None
        self._last_sample_pos = None
        self.gcode.respond_info(
            "[XY 负载记录] 开始记录 X/Y TMC 负载 (reason=%s, interval=%.3fs)"
            % (reason, self.load_sample_interval_s))
        self._record_timer = self.reactor.register_timer(
            self._sample_load,
            eventtime)

    def _stop_recording(self, eventtime, reason='manual'):
        if not self._recording:
            self.gcode.respond_info("[XY 负载记录] 当前没有记录任务。")
            return
        self._recording = False
        self._record_timer = None
        self._write_recording(eventtime, reason)

    def _sample_load(self, eventtime):
        if not self._recording:
            return self.reactor.NEVER
        try:
            x = self._read_axis_load('X')
            y = self._read_axis_load('Y')
            motion = self._sample_motion()
            tc = self.printer.lookup_object('multitool', None)
            self._record_samples.append({
                't': round(eventtime - self._record_start, 6),
                'x': x,
                'y': y,
                'motion': motion,
                'toolchange': {
                    'active': bool(tc.active) if tc is not None else False,
                    'stage': self._stage,
                    'current_tool': tc.current_tool if tc is not None else -1,
                },
            })
        except Exception as e:
            logging.exception("multitool_xy_guard: sample load error")
            self._record_error = str(e)
        return eventtime + self.load_sample_interval_s

    def _read_axis_load(self, axis):
        tmc = self._tmc.get(axis)
        if tmc is None:
            return {'error': 'missing_tmc_object'}
        mcu_tmc = getattr(tmc, 'mcu_tmc', None)
        if mcu_tmc is None:
            return {'error': 'missing_mcu_tmc'}
        try:
            raw = mcu_tmc.get_register(self.load_register)
        except Exception as e:
            return {'error': str(e)}
        value = raw
        if self.load_value_mask > 0:
            value = raw & self.load_value_mask
        return {
            'raw': raw,
            'value': value,
            'diag': 'PRESSED' if self._raw[axis] else 'RELEASED',
        }

    def _sample_motion(self):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return {'error': 'missing_toolhead'}
        try:
            pos = toolhead.get_position()
            st = toolhead.get_status(self.reactor.monotonic())
        except Exception as e:
            return {'error': str(e)}
        cur = {
            'x': round(pos[0], 6),
            'y': round(pos[1], 6),
            'z': round(pos[2], 6),
            'e': round(pos[3], 6),
        }
        prev = self._last_sample_pos
        self._last_sample_pos = cur
        print_time = float(st.get('print_time', 0.) or 0.)
        estimated_time = float(st.get('estimated_print_time', 0.) or 0.)
        queue_time = max(0., print_time - estimated_time)
        queue_active = queue_time >= self.load_queue_time_threshold
        if prev is None:
            return {
                'pos': cur,
                'delta': {'x': 0., 'y': 0., 'z': 0., 'e': 0., 'xy': 0.},
                'queue_time': round(queue_time, 6),
                'queue_active': queue_active,
                'xy_command_changed': False,
                'moving_xy': queue_active,
            }
        dx = cur['x'] - prev['x']
        dy = cur['y'] - prev['y']
        dz = cur['z'] - prev['z']
        de = cur['e'] - prev['e']
        xy = (dx * dx + dy * dy) ** 0.5
        xy_changed = xy >= self.load_xy_move_threshold
        return {
            'pos': cur,
            'delta': {
                'x': round(dx, 6),
                'y': round(dy, 6),
                'z': round(dz, 6),
                'e': round(de, 6),
                'xy': round(xy, 6),
            },
            'queue_time': round(queue_time, 6),
            'queue_active': queue_active,
            'xy_command_changed': xy_changed,
            'moving_xy': xy_changed or queue_active,
        }

    def _write_recording(self, eventtime, reason):
        dirname = os.path.dirname(self.load_output_path)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname)
        duration = max(0., eventtime - self._record_start)
        payload = {
            'schema': 'klipper-toolchange-stats.driver-load.v1',
            'source': 'multitool_xy_guard',
            'reason': reason,
            'duration_s': round(duration, 6),
            'sample_interval_s': self.load_sample_interval_s,
            'xy_move_threshold': self.load_xy_move_threshold,
            'queue_time_threshold': self.load_queue_time_threshold,
            'register': self.load_register,
            'value_mask': self.load_value_mask,
            'tmc': {'x': self.x_tmc, 'y': self.y_tmc},
            'notes': [
                'TMC2209 SG_RESULT 数值越低通常表示负载越高；阈值建议需结合实机验证。',
                '该记录用于调试 StallGuard，不等同于真实位置反馈。',
            ],
            'error': self._record_error,
            'samples': self._record_samples,
        }
        tmp = self.load_output_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write('\n')
        os.rename(tmp, self.load_output_path)
        self.gcode.respond_info(
            "[XY 负载记录] 已保存 %d 个样本到 %s"
            % (len(self._record_samples), self.load_output_path))

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
        gcmd.respond_info(
            "负载记录: %s, 样本=%d, 输出=%s"
            % ("记录中" if self._recording else
               "已启用" if self.load_debug else "未启用",
               len(self._record_samples), self.load_output_path))

    def cmd_START_XY_LOAD_RECORDING(self, gcmd):
        self._start_recording(self.reactor.monotonic(), reason='manual')

    def cmd_STOP_XY_LOAD_RECORDING(self, gcmd):
        self._stop_recording(self.reactor.monotonic(), reason='manual')

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
            'load_debug': self.load_debug,
            'load_recording': self._recording,
            'load_samples': len(self._record_samples),
            'load_output_path': self.load_output_path,
        }


def load_config(config):
    return MultitoolXYGuard(config)
