#!/usr/bin/env python3
# Klipper Multitool - 偏移管理子模块
#
# 职责：
#   - 启动时从 save_variables 加载各热端的 X/Y/Z 校准值
#   - 提供 apply(tool, base_tool) 给主流程在切换完成后调用
#
# 自动基准：
#   - 监听 print_stats.state，进入 printing 时自动清空 base_tool
#   - apply() 被调用时如果 base_tool=-1，自动把当前 tool 设为基准
#   - 不再提供手动 SET_BASE_TOOL 命令；行为完全自动
#
# 持久化字段命名：
#   - {save_prefix}{n}_offset_x
#   - {save_prefix}{n}_offset_y
#   - {save_prefix}{n}_offset_z
#   默认前缀 "t"，即 t0_offset_x、t1_offset_y 等（沿用旧版命名）

PERSIST_BASE_TOOL = 'base_tool'

PRINTING_STATES = ('printing',)


class MultitoolOffsets:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.z_offset_adaptive = config.getboolean(
            'z_offset_adaptive', False)
        self.save_prefix = config.get('save_prefix', 't')

        # 最近一次应用的偏移（仅供前端展示）
        self._applied_x = 0.0
        self._applied_y = 0.0
        self._applied_z = 0.0
        self._applied_tool = -1
        self._applied_base = -1

        # 上一次轮询到的 print_stats.state
        self._last_print_state = None

        # 启动时加载 base_tool（同步给主模块）+ 注册轮询定时器
        self.printer.register_event_handler(
            'klippy:ready', self._on_ready)

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------
    def _on_ready(self):
        sv = self.printer.lookup_object('save_variables', None)
        tc = self.printer.lookup_object('multitool', None)
        if sv is not None and tc is not None:
            v = getattr(sv, 'allVariables', {}) or {}
            try:
                tc.base_tool = int(v.get(PERSIST_BASE_TOOL, -1))
            except (TypeError, ValueError):
                tc.base_tool = -1

        # 注册到主模块的统一 print_stats.state 轮询（不再各自开定时器）
        if tc is not None:
            tc.register_print_state_listener(self._on_print_state_changed)

    # ------------------------------------------------------------------
    # 自动 base_tool：进入 printing 时清空 base_tool=-1（由主模块回调）
    #   - apply() 被调用时检测到 -1 → 用当前 tool 作为基准
    # ------------------------------------------------------------------
    def _on_print_state_changed(self, prev_state, cur_state):
        self._last_print_state = cur_state
        if (cur_state in PRINTING_STATES
                and prev_state not in PRINTING_STATES):
            # 进入打印态，清空基准让首次 pickup 自动接管
            tc = self.printer.lookup_object('multitool', None)
            if tc is not None and tc.base_tool != -1:
                tc.base_tool = -1
                self.gcode.respond_info(
                    "[multitool_offsets] 检测到打印开始，"
                    "自动重置基准热端，将由首次抓取的热端接管")

    # ------------------------------------------------------------------
    # 公共方法：被 multitool 主流程调用
    # ------------------------------------------------------------------
    def apply(self, tool, base_tool=-1):
        """切换完成后调用。设置 SET_GCODE_OFFSET 应用该热端的校准值。"""
        if tool < 0:
            return

        # 自动基准：base_tool 未设置时，把当前 tool 作为基准
        if self.z_offset_adaptive and base_tool < 0:
            tc = self.printer.lookup_object('multitool', None)
            if tc is not None:
                tc.base_tool = tool
                self.gcode.run_script_from_command(
                    "SAVE_VARIABLE VARIABLE=%s VALUE=%d"
                    % (PERSIST_BASE_TOOL, tool))
                self.gcode.respond_info(
                    "[multitool_offsets] 自动设置基准热端 = T%d" % tool)
                base_tool = tool

        ox = self._read_offset(tool, 'x')
        oy = self._read_offset(tool, 'y')
        oz = self._read_offset(tool, 'z')

        applied_z = oz
        if self.z_offset_adaptive and base_tool >= 0:
            base_z = self._read_offset(base_tool, 'z')
            applied_z = oz - base_z
            self.gcode.run_script_from_command(
                "SET_GCODE_OFFSET X=%.4f Y=%.4f Z=%.4f MOVE=0"
                % (ox, oy, applied_z))
            self.gcode.respond_info(
                "T%d 加载校准值(自适应,基准T%d): "
                "X=%.4f Y=%.4f Z=%.4f (ΔZ=%.4f)"
                % (tool, base_tool, ox, oy, oz, applied_z))
        else:
            self.gcode.run_script_from_command(
                "SET_GCODE_OFFSET X=%.4f Y=%.4f Z=%.4f MOVE=0"
                % (ox, oy, oz))
            self.gcode.respond_info(
                "T%d 加载校准值: X=%.4f Y=%.4f Z=%.4f"
                % (tool, ox, oy, oz))

        self._applied_x = ox
        self._applied_y = oy
        self._applied_z = applied_z
        self._applied_tool = tool
        self._applied_base = base_tool

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _read_offset(self, tool, axis):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return 0.0
        v = getattr(sv, 'allVariables', {}) or {}
        key = "%s%d_offset_%s" % (self.save_prefix, tool, axis)
        try:
            return float(v.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # 暴露给前端 / 宏
    # ------------------------------------------------------------------
    def get_status(self, eventtime):
        return {
            'applied_x': self._applied_x,
            'applied_y': self._applied_y,
            'applied_z': self._applied_z,
            'applied_tool': self._applied_tool,
            'applied_base': self._applied_base,
            'adaptive': self.z_offset_adaptive,
        }


def load_config(config):
    return MultitoolOffsets(config)
