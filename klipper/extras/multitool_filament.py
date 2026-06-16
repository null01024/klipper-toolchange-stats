#!/usr/bin/env python3
# Klipper Multitool - 耗材检查子模块
#
# 职责：
#   - 用 buttons helper 集中注册各通道的耗材检测 pin
#   - 通道电平变化时在控制台输出装载 / 卸载提示 (M118)
#   - 提供 assert_loaded(channel) 给 multitool 主流程在换头前调用：
#     目标工具头通道无耗材时阻止切换
#   - 断料续打：打印中当前热端断料时，自动切到同组下一个有料热端继续打印；
#     同组无可用热端时正常暂停 (见 continuation_groups)
#
# 可选模块：未声明 [multitool_filament] 时主流程探测不到本对象，
#           视为所有工具头都有耗材，不阻塞换头。
#
# 约定：
#   - buttons helper 上报 PRESSED → 视为"耗材已装载"
#   - buttons helper 上报 RELEASED → 视为"耗材已卸载"
#   - 用户在 pin 配置中用 ! / ^ / ~ 控制电平，模块本身不再有 pressed_value 字段
#
# 启动种子化：
#   - buttons helper 内部初值为 RELEASED(0)，仅在电平"变化"时才回调；
#     开机即"已卸载(0)"的通道不会触发回调，_loaded 会一直停在 None。
#   - 启动 boot_grace_s 秒后，MCU 必然已上报过当前电平；此时仍为 None 的
#     通道即代表电平一直保持 RELEASED，落定为"已卸载(False)"，并输出一份
#     状态总览，避免后续 assert_loaded 因 None 而走"未知放行"路径。
#
# 通道数：不再单独配置，直接复用 [multitool] 的 tool_count
#   （通道与工具头一一对应：assert_loaded 的 channel 就是工具头编号）。
#
# 断料续打 (continuation_groups)：
#   - 格式 [1,2],[0],[3]：每个方括号是一个有序续打组。
#   - 打印中当前热端断料 → 在组内从当前热端之后按顺序(环绕)找下一个"有料"
#     的热端，找到就自动 PAUSE → 换头 → RESUME；跳过同样没料的成员。
#   - 当前热端不在任何组 / 组里只有它自己 / 全组都没料 → 正常 PAUSE 暂停。
#   - 仅在配置了 continuation_groups 时启用；不配置则维持"只打印提示"。
#
# 断料后延后续打 (runout_continue_length)：
#   - 断料触发后不立即暂停，而是让打印继续，直到挤出机净送料达到配置长度(mm)，
#     再触发上面的暂停/续打流程，用于消耗料管(传感器→喷嘴)内的残余耗材。
#   - 净送料量 = toolhead 挤出机轴绝对坐标的增量(回抽自动抵消)。
#   - 0 = 关闭(立即触发，向后兼容)。延后期间若手动暂停/补料/换头/打印结束，
#     自动取消倒计时。
#
# 配置示例 (tool_count=4 时需配 pin_0..pin_3)：
#   [multitool_filament]
#   boot_grace_s: 5
#   continuation_groups: [1,2],[0],[3]
#   runout_continue_length: 50      # 断料后再消耗 50mm 耗材才触发续打 (0=立即)
#   sync_active_spool: True         # 换头时同步 Spoolman 当前料盘
#   pin_0: ^multihotend:IO0
#   pin_1: ^multihotend:IO1
#   pin_2: ^multihotend:IO2
#   pin_3: ^multihotend:IO3

import logging
import re

# 断料事件去抖窗口（秒）：一次断料处理后，此时间内不再重复触发，
# 避免开关抖动 / 换头过程中的电平变化引发二次续打。
RUNOUT_EVENT_DELAY = 3.
SPOOL_ID_VAR_TMPL = 'tool_%d_spool_id'


class MultitoolFilament:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()

        # 通道数直接复用 [multitool] 的 tool_count：通道与工具头一一对应，
        # 不再单独配置 channel_count。load_object 确保主模块已加载。
        self.multitool = self.printer.load_object(config, 'multitool')
        self.tool_count = self.multitool.tool_count
        self.boot_grace_s = config.getfloat(
            'boot_grace_s', 5., minval=0.)
        self.runout_event_delay = config.getfloat(
            'runout_event_delay', RUNOUT_EVENT_DELAY, minval=0.)
        self.sync_active_spool = config.getboolean('sync_active_spool', True)
        self._spoolman_sync_warned = False

        # ---- 断料后延后续打 ----
        # runout_continue_length: 断料触发后，先让打印继续，直到挤出机净送料
        #   达到该长度(mm)再触发暂停/续打，用于消耗料管(传感器→喷嘴)内的残余
        #   耗材。0 = 关闭(立即触发，向后兼容)。
        # runout_continue_poll_s: 延后期间轮询挤出机位置的间隔(秒)。
        self.runout_continue_length = config.getfloat(
            'runout_continue_length', 0., minval=0.)
        self.continue_poll_s = config.getfloat(
            'runout_continue_poll_s', 0.3, minval=0.05)

        # ---- 续打组解析 ----
        # self.groups: list[list[int]]   原始组定义（保留顺序）
        # self._group_of: dict[int, list[int]]  tool -> 其所在组
        self.groups, self._group_of = self._parse_groups(
            config.get('continuation_groups', '').strip())
        # 仅在配置了续打组时启用断料处理（向后兼容：不配置则只打印提示）
        self.runout_enabled = bool(self.groups)

        # ---- 断料处理运行时守卫 ----
        self._handling_runout = False
        self._min_event_systime = 0.

        # ---- 延后续打运行时状态 ----
        # _continue_timer: 轮询挤出机位置的 reactor 定时器(None=未启动)
        # _continue_baseline: 触发瞬间的挤出机轴绝对坐标 (toolhead E)
        # _continue_tool: 触发时正在打印的热端编号，用于中途换头检测
        self._continue_timer = None
        self._continue_baseline = 0.
        self._continue_tool = -1

        buttons = self.printer.load_object(config, 'buttons')

        # 各通道装载状态：None 表示尚未收到任何上报
        self._loaded = [None] * self.tool_count
        # 各通道 Spoolman 料盘 ID：0 表示未分配
        self._spool_ids = [0] * self.tool_count

        for ch in range(self.tool_count):
            pin = config.get('pin_%d' % ch)
            buttons.register_buttons([pin], self._make_callback(ch))

        self.printer.register_event_handler('klippy:ready', self._on_ready)
        self.printer.register_event_handler('klippy:connect', self._on_connect)

    # ------------------------------------------------------------------
    # 续打组解析 + 校验
    #   输入形如 "[1,2],[0],[3]"，输出 (groups, group_of)
    #   - 空字符串 → 不启用续打（返回空）
    #   - 索引须在 0..tool_count-1
    #   - 同一 tool 不能出现在多个组（语义二义）
    # ------------------------------------------------------------------
    def _parse_groups(self, raw):
        if not raw:
            return [], {}
        groups = []
        seen = {}
        for m in re.finditer(r'\[([^\]]*)\]', raw):
            body = m.group(1).strip()
            if not body:
                continue
            members = []
            for part in body.split(','):
                part = part.strip()
                if not part:
                    continue
                try:
                    idx = int(part)
                except ValueError:
                    raise self.printer.config_error(
                        "[multitool_filament] continuation_groups 含非数字: %r"
                        % part)
                if idx < 0 or idx >= self.tool_count:
                    raise self.printer.config_error(
                        "[multitool_filament] continuation_groups 中的热端 %d "
                        "越界 (应在 0..%d)" % (idx, self.tool_count - 1))
                if idx in seen:
                    raise self.printer.config_error(
                        "[multitool_filament] 热端 %d 出现在多个续打组中，"
                        "语义二义，请检查 continuation_groups。" % idx)
                seen[idx] = True
                members.append(idx)
            if members:
                groups.append(members)
        group_of = {}
        for g in groups:
            for t in g:
                group_of[t] = g
        return groups, group_of

    def _on_connect(self):
        self.gcode.register_command(
            'QUERY_FILAMENT_STATUS', self.cmd_QUERY_FILAMENT_STATUS,
            desc='查询各通道耗材装载状态与续打组')
        self.gcode.register_command(
            'CHECK_PRINT_FILAMENT', self.cmd_CHECK_PRINT_FILAMENT,
            desc='打印前检查 TOOLS 指定通道是否都有耗材，缺料则报错中止')
        self.gcode.register_command(
            'SET_TOOL_SPOOL_ID', self.cmd_SET_TOOL_SPOOL_ID,
            desc='设置工具通道的 Spoolman 料盘 ID')

    def _on_ready(self):
        self._load_spool_ids()
        # 启动后等 boot_grace_s 秒，让 buttons helper 把当前电平上报完，
        # 再把仍为 None 的通道落定为"已卸载"并输出一份状态总览。
        reactor = self.printer.get_reactor()
        reactor.register_callback(
            self._seed_and_report,
            reactor.monotonic() + self.boot_grace_s)
        if self.sync_active_spool:
            reactor.register_callback(
                self._sync_active_spool_event,
                reactor.monotonic() + self.boot_grace_s)

    def _load_spool_ids(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return
        values = getattr(sv, 'allVariables', {}) or {}
        for tool in range(self.tool_count):
            try:
                self._spool_ids[tool] = int(
                    values.get(SPOOL_ID_VAR_TMPL % tool, 0) or 0)
            except (TypeError, ValueError):
                self._spool_ids[tool] = 0

    def _persist_spool_id(self, tool):
        if self.printer.lookup_object('save_variables', None) is None:
            return
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=%d"
            % (SPOOL_ID_VAR_TMPL % tool, self._spool_ids[tool]))

    def _sync_active_spool_event(self, _eventtime):
        self.on_tool_changed(self.multitool.current_tool)

    def on_tool_changed(self, tool):
        if not self.sync_active_spool:
            return
        spool_id = None
        if 0 <= tool < self.tool_count:
            mapped_id = self._spool_ids[tool]
            if mapped_id > 0:
                spool_id = mapped_id
        self._set_spoolman_active_spool(spool_id)

    def _set_spoolman_active_spool(self, spool_id):
        webhooks = self.printer.lookup_object('webhooks', None)
        if webhooks is None:
            if not self._spoolman_sync_warned:
                self._spoolman_sync_warned = True
                self.gcode.respond_info(
                    "[耗材检查] 无法同步 Spoolman 当前料盘：webhooks 不可用。")
            return
        try:
            webhooks.call_remote_method(
                'spoolman_set_active_spool', spool_id=spool_id)
        except Exception:
            logging.exception(
                "multitool_filament: failed to set active Spoolman spool")
            if not self._spoolman_sync_warned:
                self._spoolman_sync_warned = True
                self.gcode.respond_info(
                    "[耗材检查] 无法同步 Spoolman 当前料盘；请确认 Moonraker 已启用 [spoolman]。")

    def _seed_and_report(self, _eventtime):
        for ch in range(self.tool_count):
            if self._loaded[ch] is None:
                # 启动宽限期内未收到回调 → 电平一直是 RELEASED → 已卸载
                self._loaded[ch] = False
        lines = []
        for ch in range(self.tool_count):
            lines.append("通道%d=%s" % (
                ch, '已装载' if self._loaded[ch] else '已卸载'))
        self.gcode.respond_info(
            "[耗材检查] 启动状态总览: %s" % ', '.join(lines))

    def _make_callback(self, channel):
        def _callback(eventtime, state):
            self._on_button(eventtime, channel, state)
        return _callback

    def _on_button(self, eventtime, channel, state):
        loaded = bool(state)
        self._loaded[channel] = loaded
        action = '已装载' if loaded else '已卸载'
        self.gcode.run_script_from_command(
            "M118 通道%d，耗材%s" % (channel, action))
        # 断料续打触发判定：仅当
        #   - 启用了续打 (配置了 continuation_groups)
        #   - 该通道变为"已卸载"
        #   - 该通道正是当前正在打印的热端
        #   - 当前不在换头中、也不在上一次断料处理中
        #   - 已过去抖窗
        #   - print_stats.state == 'printing'
        # 全部满足时，延迟到 gcode 上下文执行 _runout_event_handler
        # （button 回调里不能直接跑 PAUSE/CHANGE_TOOL 这类长脚本）。
        if (self.runout_enabled and not loaded
                and channel == self.multitool.current_tool
                and not self.multitool.active
                and not self._handling_runout
                and eventtime >= self._min_event_systime
                and self._is_printing(eventtime)):
            # 配了延后长度 → 先继续打印消耗料管残料，达到长度再触发；
            # 否则维持原行为：立即触发暂停/续打。
            if self.runout_continue_length > 0.:
                self._begin_continue(eventtime, channel)
            else:
                self._handling_runout = True
                self.reactor.register_callback(self._runout_event_handler)

    def _is_printing(self, eventtime):
        ps = self.printer.lookup_object('print_stats', None)
        if ps is None:
            return False
        return ps.get_status(eventtime).get('state') == 'printing'

    # ------------------------------------------------------------------
    # 延后续打：读取挤出机轴绝对坐标 (toolhead E)。
    #   gcode_move 在 G92 后只调整 base_position，不重置 toolhead 命令坐标，
    #   故该值在打印过程中连续累积；其增量即净送料量(回抽自动抵消)。
    # ------------------------------------------------------------------
    def _extruder_axis_pos(self):
        toolhead = self.printer.lookup_object('toolhead')
        return toolhead.get_position()[3]

    # ------------------------------------------------------------------
    # 启动延后续打：记录基准 E 与当前热端，置守卫，开轮询定时器。
    #   守卫 _handling_runout=True 占位，阻止延后期间重复触发。
    # ------------------------------------------------------------------
    def _begin_continue(self, eventtime, channel):
        if self._continue_timer is not None:
            return
        self._handling_runout = True
        self._continue_tool = channel
        self._continue_baseline = self._extruder_axis_pos()
        self.gcode.respond_info(
            "[断料续打] 通道%d 断料，继续使用 %.1fmm 耗材后再触发续打/暂停..."
            % (channel, self.runout_continue_length))
        self._continue_timer = self.reactor.register_timer(
            self._continue_poll, eventtime + self.continue_poll_s)

    # ------------------------------------------------------------------
    # 延后续打轮询：达到长度则触发续打；中途状态变化则取消。
    # ------------------------------------------------------------------
    def _continue_poll(self, eventtime):
        try:
            cur = self.multitool.current_tool
            # 中止条件：已暂停/换头中/热端变了/已补料/打印结束
            if (self.multitool.active
                    or cur != self._continue_tool
                    or self._loaded[self._continue_tool]
                    or not self._is_printing(eventtime)):
                self._cancel_continue()
                return self.reactor.NEVER

            consumed = self._extruder_axis_pos() - self._continue_baseline
            if consumed >= self.runout_continue_length:
                self.gcode.respond_info(
                    "[断料续打] 已消耗约 %.1fmm，触发续打/暂停。" % consumed)
                self._continue_timer = None
                # 守卫仍为 True，交给 _runout_event_handler 在 finally 复位
                self.reactor.register_callback(self._runout_event_handler)
                return self.reactor.NEVER
        except Exception:
            logging.exception("multitool_filament: continue poll error")
            self._cancel_continue()
            return self.reactor.NEVER
        return eventtime + self.continue_poll_s

    # ------------------------------------------------------------------
    # 取消延后续打：注销定时器、复位守卫与去抖窗。
    # ------------------------------------------------------------------
    def _cancel_continue(self):
        self._continue_timer = None
        self._handling_runout = False
        self._min_event_systime = (
            self.reactor.monotonic() + self.runout_event_delay)
        self.gcode.respond_info(
            "[断料续打] 延后续打已取消 (补料/暂停/换头/打印结束)。")

    # ------------------------------------------------------------------
    # 在 cur 所在续打组里，从 cur 之后按顺序(环绕)找下一个"有料"的热端。
    #   - cur 不在任何组 / 组里只有它自己 / 找不到有料成员 → 返回 None
    # ------------------------------------------------------------------
    def _find_next_loaded(self, cur):
        group = self._group_of.get(cur)
        if not group:
            return None
        n = len(group)
        idx = group.index(cur)
        for i in range(1, n):
            cand = group[(idx + i) % n]
            if cand == cur:
                continue
            if self._loaded[cand]:
                return cand
        return None

    # ------------------------------------------------------------------
    # 读取某热端 extruder 的目标温度；查不到 extruder 返回 None。
    # ------------------------------------------------------------------
    def _heater_target(self, tool):
        section = 'extruder' if tool == 0 else 'extruder%d' % tool
        extruder = self.printer.lookup_object(section, None)
        if extruder is None:
            return None
        return extruder.get_heater().target_temp

    # ------------------------------------------------------------------
    # 生成一条把当前 toolhead 速度限制写回的 SET_VELOCITY_LIMIT 命令。
    #   RESTORE_GCODE_STATE 不恢复这些 toolhead 层限制，续打结尾用它兜底，
    #   防止 before/after 钩子改了限制残留到续打后的打印。
    #   参数名跨版本：cruise ratio 按 get_status 实际键名选择。
    # ------------------------------------------------------------------
    def _velocity_limit_restore_cmd(self, eventtime):
        toolhead = self.printer.lookup_object('toolhead')
        st = toolhead.get_status(eventtime)
        parts = [
            "VELOCITY=%.6f" % st['max_velocity'],
            "ACCEL=%.6f" % st['max_accel'],
            "SQUARE_CORNER_VELOCITY=%.6f" % st['square_corner_velocity'],
        ]
        if 'minimum_cruise_ratio' in st:
            parts.append(
                "MINIMUM_CRUISE_RATIO=%.6f" % st['minimum_cruise_ratio'])
        elif 'max_accel_to_decel' in st:
            parts.append(
                "ACCEL_TO_DECEL=%.6f" % st['max_accel_to_decel'])
        return "SET_VELOCITY_LIMIT " + " ".join(parts)

    # ------------------------------------------------------------------
    # 断料事件处理（reactor 回调，运行在 gcode 上下文之外，可安全 run_script）
    #   无论能否续打，第一步都先暂停；再判断分支。
    # ------------------------------------------------------------------
    def _runout_event_handler(self, eventtime):
        try:
            cur = self.multitool.current_tool
            # 抖窗期间状态可能已变（已暂停 / 已换头 / 当前热端变了）→ 放弃
            if (cur < 0 or self.multitool.active
                    or self._loaded[cur]
                    or not self._is_printing(eventtime)):
                return

            # 尽快停料：先发 pause 标志位，再跑 PAUSE 宏
            pause_resume = self.printer.lookup_object('pause_resume', None)
            if pause_resume is not None:
                pause_resume.send_pause_command()

            next_tool = self._find_next_loaded(cur)
            if next_tool is None:
                self.gcode.run_script(
                    "PAUSE\n"
                    "M118 [断料续打] 通道%d 断料，且同组无可续打热端，"
                    "已暂停打印。\n"
                    "M400" % cur)
                return

            self.gcode.respond_info(
                "[断料续打] 通道%d 断料，自动续打 → T%d" % (cur, next_tool))
            # 把旧热端目标温度复制到新热端，使随后的 CHANGE_TOOL 自动等温
            old_target = self._heater_target(cur)
            offsets = self.printer.lookup_object('multitool_offsets', None)
            # 续打前(钩子运行前)抓取当前速度限制，结尾兜底写回
            restore_vlimit = self._velocity_limit_restore_cmd(eventtime)
            script = ["PAUSE"]
            if old_target is not None:
                script.append("M104 T%d S%.1f" % (next_tool, old_target))
            if self._has_macro('multitool_filament_before_swap'):
                script.append(
                    "multitool_filament_before_swap FROM=%d TO=%d"
                    % (cur, next_tool))
            script.append("CHANGE_TOOL T=%d" % next_tool)
            # 换头完成后关闭旧热端
            script.append("M104 T%d S0" % cur)
            if self._has_macro('multitool_filament_after_swap'):
                script.append(
                    "multitool_filament_after_swap FROM=%d TO=%d"
                    % (cur, next_tool))
            script.append("RESUME")
            # 兜底：恢复断料前的加速度/速度限制（RESTORE_GCODE_STATE 不管这些）
            script.append(restore_vlimit)
            script.append("M400")
            self.gcode.run_script("\n".join(script))
            # RESUME 内部的 RESTORE_GCODE_STATE 会把 gcode 偏移还原成暂停时
            # (旧热端)的值，覆盖掉 CHANGE_TOOL 为新热端设好的偏移。这里在
            # RESUME 之后重新应用一次新热端偏移加以抵消。
            if offsets is not None:
                offsets.apply(next_tool, base_tool=self.multitool.base_tool)
        except Exception:
            # 失败时打印已处于暂停态，记录日志即可，不再抛出。
            logging.exception("multitool_filament: runout handler error")
        finally:
            self._handling_runout = False
            self._min_event_systime = (
                self.reactor.monotonic() + self.runout_event_delay)

    def _has_macro(self, name):
        return name.upper() in self.gcode.gcode_handlers

    # ------------------------------------------------------------------
    # 解析打印前检查的通道列表
    #   输入形如 "0,1,2," (切片器常带尾随逗号)，输出去重保序的 int 列表。
    #   - 忽略空段 / 空白
    #   - 非数字 / 越界 (不在 0..tool_count-1) → 抛 command_error
    # ------------------------------------------------------------------
    def _parse_tool_list(self, raw):
        tools = []
        seen = set()
        for part in (raw or '').split(','):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part)
            except ValueError:
                raise self.printer.command_error(
                    "[耗材检查] CHECK_PRINT_FILAMENT TOOLS 含非数字: %r" % part)
            if idx < 0 or idx >= self.tool_count:
                raise self.printer.command_error(
                    "[耗材检查] CHECK_PRINT_FILAMENT TOOLS 中的通道 %d 越界 "
                    "(应在 0..%d)" % (idx, self.tool_count - 1))
            if idx not in seen:
                seen.add(idx)
                tools.append(idx)
        return tools

    # ------------------------------------------------------------------
    # 打印前耗材检查：由用户在 PRINT_START 中调用
    #   CHECK_PRINT_FILAMENT TOOLS=0,1,2
    #   - 逐通道输出状态总览
    #   - 任一所需通道明确无耗材 (False) → 抛 command_error 中止 PRINT_START
    #   - 状态未知 (None) 沿用 assert_loaded 哲学：仅警告，不阻塞
    # ------------------------------------------------------------------
    def cmd_CHECK_PRINT_FILAMENT(self, gcmd):
        tools = self._parse_tool_list(gcmd.get('TOOLS', ''))
        if not tools:
            gcmd.respond_info(
                "[打印前耗材检查] 未指定本次打印使用的通道 (TOOLS 为空)，"
                "跳过检查。")
            return

        lines = []
        missing = []
        unknown = []
        for ch in tools:
            st = self._loaded[ch]
            cn = '未知' if st is None else ('已装载' if st else '已卸载')
            lines.append("通道%d=%s" % (ch, cn))
            if st is None:
                unknown.append(ch)
            elif not st:
                missing.append(ch)
        gcmd.respond_info(
            "[打印前耗材检查] 本次使用通道: %s" % ', '.join(lines))

        if unknown:
            gcmd.respond_info(
                "[打印前耗材检查] 警告: 通道 %s 启动后未收到状态上报，"
                "按有耗材处理。若与实际不符请检查 pin 接线 / 电平修饰符 "
                "(! ^ ~)。" % ', '.join('%d' % c for c in unknown))

        if missing:
            msg = ("[打印前耗材检查] 通道 %s 无耗材，已中止打印。"
                   "请装料后重新开始。"
                   % ', '.join('%d' % c for c in missing))
            gcmd.respond_info(msg)
            raise self.printer.command_error(msg)

        gcmd.respond_info("[打印前耗材检查] 所有所需通道均已装料，检查通过。")

    def cmd_QUERY_FILAMENT_STATUS(self, gcmd):
        gcmd.respond_info("====== 耗材检查状态 ======")
        for ch in range(self.tool_count):
            st = self._loaded[ch]
            cn = '未知' if st is None else ('已装载' if st else '已卸载')
            gcmd.respond_info(
                "通道%d: %s, Spoolman ID=%d"
                % (ch, cn, self._spool_ids[ch]))
        if self.runout_enabled:
            groups_cn = ', '.join(
                '[%s]' % ','.join('T%d' % t for t in g) for g in self.groups)
            gcmd.respond_info("续打组: %s" % groups_cn)
            if self.runout_continue_length > 0.:
                gcmd.respond_info(
                    "延后续打: 断料后再消耗 %.1fmm 耗材才触发"
                    % self.runout_continue_length)
            else:
                gcmd.respond_info("延后续打: 关闭 (断料立即触发)")
        else:
            gcmd.respond_info("续打组: 未配置 (断料仅提示，不自动续打/暂停)")

    def cmd_SET_TOOL_SPOOL_ID(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=0, maxval=self.tool_count - 1)
        spool_id = gcmd.get_int('SPOOL_ID', minval=0)
        self._spool_ids[tool] = spool_id
        self._persist_spool_id(tool)
        if spool_id:
            gcmd.respond_info(
                "[耗材检查] 通道%d 已关联 Spoolman ID=%d"
                % (tool, spool_id))
        else:
            gcmd.respond_info("[耗材检查] 通道%d 已清除 Spoolman 料盘关联" % tool)
        if tool == self.multitool.current_tool:
            self.on_tool_changed(tool)

    # ------------------------------------------------------------------
    # 公共方法：被 multitool 主流程在换头前调用
    #   - 通道未配置 (channel >= tool_count) → 视为有耗材，不阻塞
    #   - 状态未知 (None，开机后从未上报过电平变化) → 视为有耗材，仅警告，
    #     不阻塞（buttons helper 仅在电平变化时回调，与 clamp 模块同样的取舍）
    #   - 明确无耗材 (False) → 抛错阻止换头
    # ------------------------------------------------------------------
    def assert_loaded(self, channel, reason=''):
        if channel < 0 or channel >= self.tool_count:
            return

        loaded = self._loaded[channel]
        if loaded is None:
            self.gcode.respond_info(
                "[耗材检查] 警告: 通道%d 启动后未收到状态上报，"
                "按有耗材处理以放行换头。若与实际不符请检查 pin 接线 / "
                "电平修饰符 (! ^ ~)。" % channel)
            return

        if not loaded:
            msg = ("耗材检查失败 (原因=%s)：通道%d 无耗材，禁止切换到该工具头。"
                   % (reason, channel))
            self.gcode.respond_info(msg)
            raise self.printer.command_error(msg)

    def _runout_status(self):
        tool = self._continue_tool
        if tool < 0 and self._handling_runout:
            tool = self.multitool.current_tool

        remaining = 0.
        if (self._handling_runout and self._continue_timer is not None
                and self.runout_continue_length > 0.):
            try:
                consumed = self._extruder_axis_pos() - self._continue_baseline
                remaining = max(0., self.runout_continue_length - consumed)
            except Exception:
                logging.exception(
                    "multitool_filament: failed to compute runout status")
                remaining = 0.

        return {
            'active': self._handling_runout,
            'tool': tool if self._handling_runout else -1,
            'remaining_mm': remaining,
            'continue_length': self.runout_continue_length,
        }

    def get_status(self, eventtime):
        return {
            'tool_count': self.tool_count,
            'loaded': list(self._loaded),
            'spool_ids': list(self._spool_ids),
            'runout_enabled': self.runout_enabled,
            'continuation_groups': [list(g) for g in self.groups],
            'runout_continue_length': self.runout_continue_length,
            'runout': self._runout_status(),
            'sync_active_spool': self.sync_active_spool,
        }


def load_config(config):
    return MultitoolFilament(config)
