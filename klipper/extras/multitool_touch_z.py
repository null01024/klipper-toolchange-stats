#!/usr/bin/env python3
# Klipper Multitool - 独立微动/压力热床 Z 触发校准
#
# 职责：
#   - 使用独立 pin 做 Z 向接触探测，不占用 Klipper 的 [probe]
#   - 记录每个工具的触发 Z，并按 T0 基准保存 t{n}_offset_z
#   - 供涡流扫床 / 涡流 XY 校准场景复用接触式 Z 基准

import logging


class MultitoolTouchZ:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.pin = config.get('pin')
        self.save_prefix = config.get('save_prefix', 't')
        self.default_tool = config.getint('base_tool', 0, minval=0)

        self.speed = config.getfloat('speed', 2.0, above=0.0)
        self.lift_speed = config.getfloat('lift_speed', 5.0, above=0.0)
        self.sample_retract_dist = config.getfloat(
            'sample_retract_dist', 2.0, minval=0.0)
        self.samples = config.getint('samples', 3, minval=1, maxval=20)
        self.samples_result = config.getchoice(
            'samples_result', {'median': 'median', 'average': 'average'},
            'median')
        self.samples_tolerance = config.getfloat(
            'samples_tolerance', 0.05, minval=0.0)
        self.samples_tolerance_retries = config.getint(
            'samples_tolerance_retries', 3, minval=0, maxval=20)
        self.probe_depth = config.getfloat('probe_depth', 5.0, above=0.0)
        self.final_lift_z = config.getfloat('final_lift_z', 2.0, minval=0.0)
        self.clear_xy_offset = config.getboolean('clear_xy_offset', False)

        pins = self.printer.lookup_object('pins')
        self.mcu_endstop = pins.setup_pin('endstop', self.pin)

        self._last_z = None
        self._last_tool = None
        self._tool_z = {}
        self._last_samples = []

        self.printer.register_event_handler(
            'klippy:mcu_identify', self._handle_mcu_identify)

        self.gcode.register_command(
            'TOUCH_Z_PROBE', self.cmd_TOUCH_Z_PROBE,
            desc='使用独立微动/压力热床 pin 探测当前工具 Z 触发坐标')
        self.gcode.register_command(
            'TOUCH_Z_CALIBRATE_TOOL', self.cmd_TOUCH_Z_CALIBRATE_TOOL,
            desc='测量并保存工具相对 T0 的 Z 偏移')
        self.gcode.register_command(
            'QUERY_TOUCH_Z', self.cmd_QUERY_TOUCH_Z,
            desc='查询独立 Z 触发校准结果')
        self.gcode.register_command(
            'CLEAR_TOUCH_Z', self.cmd_CLEAR_TOUCH_Z,
            desc='清除独立 Z 触发校准结果')

    def _handle_mcu_identify(self):
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.mcu_endstop.add_stepper(stepper)

    def _check_can_probe(self):
        toolhead = self.printer.lookup_object('toolhead')
        homed_axes = toolhead.get_status(0.).get('homed_axes', '')
        if 'z' not in homed_axes:
            raise self.printer.command_error(
                "TOUCH_Z_PROBE 需要先完成 Z 归位，避免未定义坐标下探")
        if 'x' not in homed_axes or 'y' not in homed_axes:
            raise self.printer.command_error(
                "TOUCH_Z_PROBE 需要先完成 XY 归位")

    def _single_probe(self, speed, probe_depth):
        toolhead = self.printer.lookup_object('toolhead')
        curpos = toolhead.get_position()
        movepos = list(curpos)
        movepos[2] -= probe_depth
        phoming = self.printer.lookup_object('homing')
        try:
            epos = phoming.probing_move(
                self.mcu_endstop, movepos, speed, check_movement=True)
        except self.printer.command_error as e:
            raise self.printer.command_error(
                "TOUCH_Z_PROBE 未在 %.3fmm 下探范围内触发 pin=%s: %s"
                % (probe_depth, self.pin, str(e)))
        return epos[2]

    def _calc_sample_result(self, samples):
        values = sorted(samples)
        if self.samples_result == 'average':
            return sum(values) / len(values)
        mid = len(values) // 2
        if len(values) & 1:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2.

    def _probe(self, samples, speed, lift_speed, retract_dist, probe_depth,
               tolerance, retries, final_lift_z):
        self._check_can_probe()
        toolhead = self.printer.lookup_object('toolhead')
        results = []
        attempt = 0

        while True:
            results = []
            for i in range(samples):
                z = self._single_probe(speed, probe_depth)
                results.append(z)
                if i != samples - 1 and retract_dist > 0.:
                    curpos = toolhead.get_position()
                    toolhead.manual_move(
                        [None, None, curpos[2] + retract_dist], lift_speed)
                    toolhead.wait_moves()

            spread = max(results) - min(results)
            if samples <= 1 or spread <= tolerance:
                break
            attempt += 1
            if attempt > retries:
                raise self.printer.command_error(
                    "TOUCH_Z_PROBE samples tolerance exceeded: "
                    "range=%.6f tolerance=%.6f samples=%s"
                    % (spread, tolerance,
                       ','.join(['%.6f' % v for v in results])))
            logging.info(
                "multitool_touch_z: retry samples, range=%.6f tolerance=%.6f",
                spread, tolerance)
            if retract_dist > 0.:
                curpos = toolhead.get_position()
                toolhead.manual_move(
                    [None, None, curpos[2] + retract_dist], lift_speed)
                toolhead.wait_moves()

        result = self._calc_sample_result(results)
        if final_lift_z > 0.:
            curpos = toolhead.get_position()
            toolhead.manual_move(
                [None, None, curpos[2] + final_lift_z], lift_speed)
            toolhead.wait_moves()
        self._last_z = result
        self._last_samples = list(results)
        return result, results

    def _save_z_offset(self, tool, z_value):
        self._tool_z[tool] = z_value
        base_z = self._tool_z.get(self.default_tool)
        if base_z is None:
            if tool == self.default_tool:
                base_z = z_value
            else:
                raise self.printer.command_error(
                    "请先校准基准工具 T%d，再校准 T%d"
                    % (self.default_tool, tool))

        offset_z = z_value - base_z
        if tool == self.default_tool:
            offset_z = 0.0
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s%d_offset_z VALUE=%.6f"
            % (self.save_prefix, tool, offset_z))
        return offset_z

    def cmd_TOUCH_Z_PROBE(self, gcmd):
        samples = gcmd.get_int('SAMPLES', self.samples, minval=1, maxval=20)
        speed = gcmd.get_float('SPEED', self.speed, above=0.0)
        lift_speed = gcmd.get_float('LIFT_SPEED', self.lift_speed, above=0.0)
        retract = gcmd.get_float(
            'SAMPLE_RETRACT_DIST', self.sample_retract_dist, minval=0.0)
        depth = gcmd.get_float('PROBE_DEPTH', self.probe_depth, above=0.0)
        tolerance = gcmd.get_float(
            'SAMPLES_TOLERANCE', self.samples_tolerance, minval=0.0)
        retries = gcmd.get_int(
            'SAMPLES_TOLERANCE_RETRIES', self.samples_tolerance_retries,
            minval=0, maxval=20)
        final_lift = gcmd.get_float(
            'FINAL_LIFT_Z', self.final_lift_z, minval=0.0)

        z_value, results = self._probe(
            samples, speed, lift_speed, retract, depth, tolerance, retries,
            final_lift)
        gcmd.respond_info(
            "TOUCH_Z_PROBE: Z=%.6f samples=%s"
            % (z_value, ','.join(['%.6f' % v for v in results])))

    def cmd_TOUCH_Z_CALIBRATE_TOOL(self, gcmd):
        tool_name = gcmd.get('TOOL', None)
        if tool_name is None:
            raise gcmd.error("TOOL 参数必填，例如 TOOL=0")
        try:
            tool = int(tool_name)
        except ValueError:
            raise gcmd.error("TOOL 必须是整数，例如 TOOL=0")
        if tool < 0:
            raise gcmd.error("TOOL 必须 >= 0")
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        do_change = gcmd.get_int('CHANGE_TOOL', 1, minval=0, maxval=1)
        update_eddy = gcmd.get_int('UPDATE_EDDY', 0, minval=0, maxval=1)

        if self.clear_xy_offset:
            self.gcode.run_script_from_command(
                "SET_GCODE_OFFSET X=0 Y=0 Z=0 MOVE=0")
        else:
            self.gcode.run_script_from_command(
                "SET_GCODE_OFFSET Z=0 MOVE=0")

        if do_change:
            self.gcode.run_script_from_command("T%d" % tool)

        self.gcode.run_script_from_command("M400")
        z_value, results = self._probe(
            gcmd.get_int('SAMPLES', self.samples, minval=1, maxval=20),
            gcmd.get_float('SPEED', self.speed, above=0.0),
            gcmd.get_float('LIFT_SPEED', self.lift_speed, above=0.0),
            gcmd.get_float('SAMPLE_RETRACT_DIST', self.sample_retract_dist,
                           minval=0.0),
            gcmd.get_float('PROBE_DEPTH', self.probe_depth, above=0.0),
            gcmd.get_float('SAMPLES_TOLERANCE', self.samples_tolerance,
                           minval=0.0),
            gcmd.get_int('SAMPLES_TOLERANCE_RETRIES',
                         self.samples_tolerance_retries, minval=0, maxval=20),
            gcmd.get_float('FINAL_LIFT_Z', self.final_lift_z, minval=0.0))

        self._last_tool = tool
        self._tool_z[tool] = z_value
        offset_z = None
        if save:
            offset_z = self._save_z_offset(tool, z_value)
        if update_eddy:
            self.gcode.run_script_from_command(
                "SET_TOOL_Z TOOL=%d Z=%.6f" % (tool, z_value))

        msg = "T%d touch Z=%.6f samples=%s" % (
            tool, z_value, ','.join(['%.6f' % v for v in results]))
        if offset_z is not None:
            msg += " saved %s%d_offset_z=%.6f" % (
                self.save_prefix, tool, offset_z)
        if update_eddy:
            msg += " updated SET_TOOL_Z"
        gcmd.respond_info(msg)

    def cmd_QUERY_TOUCH_Z(self, gcmd):
        lines = []
        if self._last_z is None:
            lines.append("last_z=unknown")
        else:
            lines.append("last_tool=%s last_z=%.6f samples=%s" % (
                self._last_tool if self._last_tool is not None else 'none',
                self._last_z,
                ','.join(['%.6f' % v for v in self._last_samples])))
        for tool in sorted(self._tool_z):
            lines.append("T%d Z=%.6f" % (tool, self._tool_z[tool]))
        gcmd.respond_info("\n".join(lines))

    def cmd_CLEAR_TOUCH_Z(self, gcmd):
        tool_name = gcmd.get('TOOL', None)
        if tool_name is None:
            self._tool_z.clear()
            self._last_z = None
            self._last_tool = None
            self._last_samples = []
            gcmd.respond_info("已清除所有 TOUCH_Z 结果")
        else:
            try:
                tool = int(tool_name)
            except ValueError:
                raise gcmd.error("TOOL 必须是整数，例如 TOOL=0")
            if tool < 0:
                raise gcmd.error("TOOL 必须 >= 0")
            self._tool_z.pop(tool, None)
            gcmd.respond_info("已清除 T%d TOUCH_Z 结果" % tool)

    def get_status(self, eventtime):
        tools = {}
        for tool, z_value in self._tool_z.items():
            tools[str(tool)] = {'z': z_value}
        return {
            'last_z': self._last_z,
            'last_tool': self._last_tool,
            'last_samples': list(self._last_samples),
            'tools': tools,
            'base_tool': self.default_tool,
        }


def load_config(config):
    return MultitoolTouchZ(config)
