#!/usr/bin/env python3
# Klipper Multitool - per-tool RGB status LEDs
#
# Optional module.  It maps one LED index to each tool channel and overlays
# printer/tool state effects on top of each channel's filament color.

import logging
import math
import re

DEFAULT_LED_NAME = 'multitool_rgb'
RGB_COLOR_VAR_TMPL = 'tool_%d_rgb_color'
ANIM_INTERVAL_S = 0.20
DEFAULT_EFFECTS = {
    'idle': 'solid',
    'printing': 'solid',
    'changing': 'chase',
    'heating': 'breathe',
    'paused': 'amber_pulse',
    'runout': 'red_flash',
    'error': 'red_flash',
}
PRINTING_STATES = ('printing',)
PAUSED_STATES = ('paused',)
ERROR_STATES = ('error',)


class MultitoolRgb:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.multitool = self.printer.load_object(config, 'multitool')
        self.tool_count = self.multitool.tool_count

        if config.get('led', None) is not None:
            raise config.error(
                "[multitool_rgb] 已不再支持 led；请直接在 "
                "[multitool_rgb] 中配置 pin。")
        self.led = DEFAULT_LED_NAME
        self._internal_led = self._setup_neopixel(config)
        self.led_indices = self._parse_int_list(
            config.get('led_indices', ''), 'led_indices')
        if not self.led_indices:
            self.led_indices = list(range(1, self.tool_count + 1))
        if len(self.led_indices) != self.tool_count:
            raise config.error(
                "[multitool_rgb] led_indices 数量必须等于 [multitool] "
                "tool_count=%d。" % self.tool_count)
        if len(set(self.led_indices)) != len(self.led_indices):
            raise config.error("[multitool_rgb] led_indices 不能重复。")
        if min(self.led_indices) < 1:
            raise config.error(
                "[multitool_rgb] led_indices 使用 Klipper SET_LED 的 "
                "1-based INDEX，最小值必须 >= 1。")

        self.brightness = config.getfloat(
            'brightness', 0.35, minval=0., maxval=1.)
        self.dim_brightness = config.getfloat(
            'dim_brightness', self.brightness * 0.20, minval=0., maxval=1.)
        self.unloaded_brightness = config.getfloat(
            'unloaded_brightness', 0.0, minval=0., maxval=1.)
        self.spoolman_colors = config.getboolean('spoolman_colors', True)
        self.off_when_disabled = config.getboolean(
            'off_when_disabled', True)
        self.effects_enabled = config.getboolean('effects', True)

        self.fallback_colors = self._parse_colors(
            config.get('fallback_colors', ''), 'fallback_colors')
        if not self.fallback_colors:
            self.fallback_colors = self._default_colors(self.tool_count)
        while len(self.fallback_colors) < self.tool_count:
            self.fallback_colors.append(
                self.fallback_colors[len(self.fallback_colors)
                                     % len(self.fallback_colors)])
        self.fallback_colors = self.fallback_colors[:self.tool_count]

        self.enabled = True
        self.print_state = None
        self._tick = 0
        self._timer = None
        self._manual_colors = [None] * self.tool_count
        self._spoolman_colors = [None] * self.tool_count
        self._last_mode = 'boot'
        self._last_effect = 'solid'
        self._led_checked = False
        self._led_available = True

        self.printer.register_event_handler('klippy:connect', self._on_connect)
        self.printer.register_event_handler('klippy:ready', self._on_ready)

    def _setup_neopixel(self, config):
        if config.get('pin', None) is None:
            raise config.error("[multitool_rgb] pin 不能为空。")
        try:
            import neopixel
        except ImportError:
            raise config.error(
                "[multitool_rgb] 无法加载 Klipper neopixel 模块。")
        led_config = _NeopixelConfigProxy(config, self.led, self.tool_count)
        if hasattr(neopixel, 'load_config_prefix'):
            led_obj = neopixel.load_config_prefix(led_config)
        else:
            led_obj = neopixel.PrinterNeoPixel(led_config)
        self.printer.add_object('neopixel %s' % self.led, led_obj)
        return led_obj

    def _parse_int_list(self, raw, name):
        values = []
        for part in (raw or '').split(','):
            part = part.strip()
            if not part:
                continue
            try:
                values.append(int(part))
            except ValueError:
                raise self.printer.config_error(
                    "[multitool_rgb] %s 含非数字: %r" % (name, part))
        return values

    def _parse_colors(self, raw, name):
        colors = []
        for part in (raw or '').split(','):
            part = part.strip()
            if not part:
                continue
            colors.append(self._parse_color(part, name))
        return colors

    def _parse_color(self, value, name='COLOR'):
        value = str(value).strip()
        if not value:
            raise self.printer.config_error(
                "[multitool_rgb] %s 颜色为空。" % name)
        digits = re.sub(r'[^0-9a-fA-F]', '', value)
        if len(digits) != 6:
            raise self.printer.config_error(
                "[multitool_rgb] %s 颜色必须为 #rrggbb: %r"
                % (name, value))
        return tuple(int(digits[i:i + 2], 16) / 255. for i in (0, 2, 4))

    def _color_hex(self, color):
        vals = [max(0, min(255, int(round(c * 255.)))) for c in color]
        return '#%02x%02x%02x' % tuple(vals)

    def _default_colors(self, count):
        palette = (
            '#ffffff', '#ff8000', '#00aaff', '#55ff55',
            '#ff4fd8', '#ffd400', '#7c4dff', '#00d084',
            '#ff4040', '#40ffe0', '#c0c0c0', '#8040ff',
            '#ffb000', '#00b0ff', '#90ff40', '#ff6090',
        )
        return [self._parse_color(palette[i % len(palette)], 'default')
                for i in range(count)]

    def _on_connect(self):
        existing = self.gcode.gcode_handlers
        conflicts = [n for n in (
            'QUERY_MULTITOOL_RGB',
            'SET_MULTITOOL_RGB_COLOR',
            'SET_MULTITOOL_RGB',
        ) if n in existing]
        if conflicts:
            raise self.printer.command_error(
                "[multitool_rgb] 以下命令已被其他 section 注册：%s"
                % ', '.join(conflicts))
        self.gcode.register_command(
            'QUERY_MULTITOOL_RGB', self.cmd_QUERY_MULTITOOL_RGB,
            desc='查询多工具 RGB 状态灯')
        self.gcode.register_command(
            'SET_MULTITOOL_RGB_COLOR', self.cmd_SET_MULTITOOL_RGB_COLOR,
            desc='设置或清除工具通道 RGB 颜色')
        self.gcode.register_command(
            'SET_MULTITOOL_RGB', self.cmd_SET_MULTITOOL_RGB,
            desc='启停多工具 RGB 状态灯')

    def _on_ready(self):
        self._load_saved_colors()
        self.multitool.register_print_state_listener(
            self._on_print_state_changed)
        self._timer = self.reactor.register_timer(
            self._animate, self.reactor.monotonic() + 0.1)

    def _load_saved_colors(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return
        values = getattr(sv, 'allVariables', {}) or {}
        for tool in range(self.tool_count):
            raw = values.get(RGB_COLOR_VAR_TMPL % tool)
            if raw is None or raw == '':
                continue
            try:
                self._manual_colors[tool] = self._parse_color(
                    raw, RGB_COLOR_VAR_TMPL % tool)
            except Exception:
                logging.exception(
                    "multitool_rgb: invalid saved color for T%d", tool)

    def _on_print_state_changed(self, prev, cur):
        self.print_state = cur
        self._tick = 0
        self._render()

    def cmd_QUERY_MULTITOOL_RGB(self, gcmd):
        mode, effect = self._mode_and_effect()
        gcmd.respond_info("====== 多工具 RGB 状态 ======")
        gcmd.respond_info(
            "LED=%s enabled=%s brightness=%.2f mode=%s effect=%s"
            % (self.led, self.enabled, self.brightness, mode, effect))
        for tool in range(self.tool_count):
            src = self._color_source(tool)
            loaded = self._loaded(tool)
            loaded_cn = '未知' if loaded is None else (
                '已装载' if loaded else '已卸载')
            gcmd.respond_info(
                "T%d INDEX=%d color=%s source=%s loaded=%s"
                % (tool, self.led_indices[tool],
                   self._color_hex(self._base_color(tool)), src, loaded_cn))

    def cmd_SET_MULTITOOL_RGB_COLOR(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=0, maxval=self.tool_count - 1)
        source = gcmd.get('SOURCE', 'manual').strip().lower()
        clear = gcmd.get_int('CLEAR', 0, minval=0, maxval=1)
        save = gcmd.get_int('SAVE', 0, minval=0, maxval=1)
        if clear:
            if source == 'spoolman':
                self._spoolman_colors[tool] = None
            else:
                self._manual_colors[tool] = None
                if save:
                    self._save_manual_color(tool)
            self._render()
            gcmd.respond_info(
                "[multitool_rgb] T%d 已清除 %s 颜色缓存"
                % (tool, source))
            return

        color = self._parse_color(gcmd.get('COLOR'), 'COLOR')
        if source == 'spoolman':
            if self.spoolman_colors:
                self._spoolman_colors[tool] = color
        else:
            self._manual_colors[tool] = color
            if save:
                self._save_manual_color(tool)
        self._render()
        gcmd.respond_info(
            "[multitool_rgb] T%d color=%s source=%s"
            % (tool, self._color_hex(color), source))

    def cmd_SET_MULTITOOL_RGB(self, gcmd):
        enable = gcmd.get_int('ENABLE', 1, minval=0, maxval=1)
        self.enabled = bool(enable)
        if self.enabled:
            self._render()
            gcmd.respond_info("[multitool_rgb] RGB 状态灯已启用。")
        else:
            if self.off_when_disabled and self._ensure_led_available():
                self._set_all_off()
            gcmd.respond_info("[multitool_rgb] RGB 状态灯已停用。")

    def _save_manual_color(self, tool):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return
        color = self._manual_colors[tool]
        if color is None:
            value = '""'
        else:
            value = '"%s"' % self._color_hex(color)
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE='%s'"
            % (RGB_COLOR_VAR_TMPL % tool, value))

    def _animate(self, eventtime):
        self._tick += 1
        self._render()
        return eventtime + ANIM_INTERVAL_S

    def _render(self):
        if not self.enabled:
            return
        if not self._ensure_led_available():
            return
        try:
            colors = self._frame_colors()
            self._send_colors(colors)
        except Exception:
            logging.exception("multitool_rgb: failed to render frame")

    def _ensure_led_available(self):
        if self._led_checked:
            return self._led_available
        self._led_checked = True
        if self._lookup_led_object() is None:
            self._led_available = False
            self.gcode.respond_info(
                "[multitool_rgb] 未找到内部 neopixel 对象 %s。"
                % self.led)
            return self._led_available
        led_obj = self._lookup_led_object()
        try:
            status = led_obj.get_status(self.reactor.monotonic())
            color_data = status.get('color_data')
            if color_data is not None and max(self.led_indices) > len(color_data):
                self._led_available = False
                self.gcode.respond_info(
                    "[multitool_rgb] LED %s 只有 %d 颗，但 led_indices "
                    "最大为 %d；请增大 chain_count 或修正 led_indices。"
                    % (self.led, len(color_data), max(self.led_indices)))
        except Exception:
            logging.debug(
                "multitool_rgb: unable to validate LED chain length",
                exc_info=True)
        return self._led_available

    def _lookup_led_object(self):
        return self.printer.lookup_object('neopixel %s' % self.led, None)

    def _frame_colors(self):
        mode, effect = self._mode_and_effect()
        self._last_mode = mode
        self._last_effect = effect
        if effect == 'off':
            return [(0., 0., 0.) for _ in range(self.tool_count)]
        colors = []
        for tool in range(self.tool_count):
            colors.append(self._tool_color(tool, mode, effect))
        return colors

    def _mode_and_effect(self):
        runout = self._runout_status()
        if runout.get('active'):
            return 'runout', self._effect_for_mode('runout')
        if self.print_state in ERROR_STATES:
            return 'error', self._effect_for_mode('error')
        if self.print_state in PAUSED_STATES:
            return 'paused', self._effect_for_mode('paused')
        if self.multitool.active:
            return 'changing', self._effect_for_mode('changing')
        if self._heating_tool() >= 0:
            return 'heating', self._effect_for_mode('heating')
        if self.print_state in PRINTING_STATES:
            return 'printing', self._effect_for_mode('printing')
        return 'idle', self._effect_for_mode('idle')

    def _effect_for_mode(self, mode):
        if not self.effects_enabled:
            return 'solid'
        return DEFAULT_EFFECTS.get(mode, 'solid')

    def _tool_color(self, tool, mode, effect):
        loaded = self._loaded(tool)
        if loaded is False and mode not in ('runout', 'error'):
            return self._scale((0.18, 0.18, 0.18), self.unloaded_brightness)

        base = self._base_color(tool)
        current = self.multitool.current_tool
        target = self.multitool.change_to_tool
        if mode == 'runout':
            runout = self._runout_status()
            rtool = runout.get('tool', -1)
            if tool == rtool:
                if self.effects_enabled:
                    return self._flash((1., 0., 0.), self.brightness)
                return self._scale((1., 0., 0.), self.brightness)
            if tool == target and target >= 0:
                if not self.effects_enabled:
                    return self._scale(base, self.brightness)
                return self._breathe(base, self.dim_brightness,
                                     self.brightness)
            return self._scale(base, self.dim_brightness)
        if mode == 'error':
            if self.effects_enabled:
                return self._flash((1., 0., 0.), self.brightness)
            return self._scale((1., 0., 0.), self.brightness)
        if mode == 'paused':
            level = self.brightness if tool == current else self.dim_brightness
            if self.effects_enabled:
                return self._pulse((1., 0.55, 0.05), level)
            return self._scale((1., 0.55, 0.05), level)
        if mode == 'changing':
            if effect == 'chase':
                return self._chase_color(tool, base)
            return self._scale(base, self.brightness if tool == target
                               else self.dim_brightness)
        if mode == 'heating':
            htool = self._heating_tool()
            if tool == htool:
                if not self.effects_enabled:
                    return self._scale(base, self.brightness)
                return self._breathe(base, self.dim_brightness,
                                     self.brightness)
            return self._scale(base, self.dim_brightness)
        if mode == 'printing':
            level = self.brightness if tool == current else self.dim_brightness
            return self._scale(base, level)
        return self._scale(base, self.dim_brightness)

    def _base_color(self, tool):
        if self.spoolman_colors and self._spoolman_colors[tool] is not None:
            return self._spoolman_colors[tool]
        if self._manual_colors[tool] is not None:
            return self._manual_colors[tool]
        return self.fallback_colors[tool]

    def _color_source(self, tool):
        if self.spoolman_colors and self._spoolman_colors[tool] is not None:
            return 'spoolman'
        if self._manual_colors[tool] is not None:
            return 'manual'
        return 'fallback'

    def _loaded(self, tool):
        filament = self.printer.lookup_object('multitool_filament', None)
        if filament is None:
            return True
        try:
            loaded = filament.get_loaded_status()
            if tool < len(loaded):
                return loaded[tool]
        except Exception:
            logging.exception("multitool_rgb: failed to read filament status")
        return None

    def _runout_status(self):
        filament = self.printer.lookup_object('multitool_filament', None)
        if filament is None:
            return {'active': False, 'tool': -1}
        try:
            return filament.get_status(
                self.reactor.monotonic()).get('runout') or {}
        except Exception:
            logging.exception("multitool_rgb: failed to read runout status")
            return {'active': False, 'tool': -1}

    def _heating_tool(self):
        tool = self.multitool.change_to_tool
        if tool < 0:
            tool = self.multitool.current_tool
        if tool < 0 or tool >= self.tool_count:
            return -1
        section = 'extruder' if tool == 0 else 'extruder%d' % tool
        extruder = self.printer.lookup_object(section, None)
        if extruder is None:
            return -1
        try:
            heater = extruder.get_heater()
            status = heater.get_status(self.reactor.monotonic())
            target = float(status.get('target', 0.))
            temp = float(status.get('temperature', 0.))
            if target >= 50. and temp < target - 1.5:
                return tool
        except Exception:
            logging.exception("multitool_rgb: failed to read heater status")
        return -1

    def _chase_color(self, tool, base):
        frm = self.multitool.change_from_tool
        to = self.multitool.change_to_tool
        if tool == to:
            return self._scale(base, self.brightness)
        if frm < 0 or to < 0:
            active = self._tick % self.tool_count
            return self._scale(base, self.brightness if tool == active
                               else self.dim_brightness)
        lo, hi = min(frm, to), max(frm, to)
        if lo <= tool <= hi:
            span = hi - lo + 1
            pos = lo + (self._tick % span)
            if frm > to:
                pos = hi - (self._tick % span)
            return self._scale(base, self.brightness if tool == pos
                               else self.dim_brightness)
        return self._scale(base, self.dim_brightness)

    def _scale(self, color, level):
        return tuple(max(0., min(1., c * level)) for c in color)

    def _breathe(self, color, low, high):
        phase = (math.sin(self._tick * 0.55) + 1.) / 2.
        level = low + (high - low) * phase
        return self._scale(color, level)

    def _pulse(self, color, high):
        low = min(self.dim_brightness, high)
        return self._breathe(color, low, high)

    def _flash(self, color, high):
        return self._scale(color, high if self._tick % 2 == 0 else 0.)

    def _send_colors(self, colors):
        for tool, color in enumerate(colors):
            transmit = 1 if tool == len(colors) - 1 else 0
            self._send_led(self.led_indices[tool], color, transmit)

    def _send_led(self, index, color, transmit):
        r, g, b = color
        self.gcode.run_script_from_command(
            "SET_LED LED=%s INDEX=%d RED=%.5f GREEN=%.5f BLUE=%.5f "
            "SYNC=0 TRANSMIT=%d"
            % (self.led, index, r, g, b, transmit))

    def _set_all_off(self):
        for i, index in enumerate(self.led_indices):
            self._send_led(index, (0., 0., 0.),
                           1 if i == len(self.led_indices) - 1 else 0)

    def get_status(self, eventtime):
        mode, effect = self._mode_and_effect()
        return {
            'enabled': self.enabled,
            'led': self.led,
            'led_indices': list(self.led_indices),
            'brightness': self.brightness,
            'effects': self.effects_enabled,
            'mode': mode,
            'effect': effect,
            'colors': [self._color_hex(self._base_color(i))
                       for i in range(self.tool_count)],
            'color_sources': [self._color_source(i)
                              for i in range(self.tool_count)],
        }


def load_config(config):
    return MultitoolRgb(config)


class _NeopixelConfigProxy:
    def __init__(self, config, led_name, default_chain_count):
        self._config = config
        self._name = 'neopixel %s' % led_name
        self._default_chain_count = default_chain_count

    def get_name(self):
        return self._name

    def get(self, option, default=None, **kwargs):
        if option == 'chain_count' and self._config.get(option, None) is None:
            return str(self._default_chain_count)
        return self._config.get(option, default, **kwargs)

    def getint(self, option, default=None, **kwargs):
        if option == 'chain_count' and self._config.get(option, None) is None:
            default = self._default_chain_count
        return self._config.getint(option, default, **kwargs)

    def __getattr__(self, name):
        return getattr(self._config, name)
