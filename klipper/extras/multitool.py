#!/usr/bin/env python3
# Klipper Multitool 主模块
#
# 职责：
#   1. 维护当前热端状态 (current_tool)，与 save_variables 双向同步
#   2. 注册 T0..T{tool_count-1} / UNTOOL / CHANGE_TOOL / QUERY_TOOL_STATUS
#   3. 编排 change_tool 主流程：抬升 / 切 accel / 调用用户钩子 / 落盘 / 等温
#
# 子模块通过 lookup_object 探测，未声明则跳过对应分支：
#   - multitool_clamp   : 钩子前后置自动调用 assert_state
#   - multitool_offsets : 切换完成后调用 apply()
#   - multitool_stats   : 全流程嵌入计时
#
# 用户必须实现两个宏：
#   [gcode_macro multitool_release_tool]   入参 TOOL=<int>
#   [gcode_macro multitool_pickup_tool]    入参 TOOL=<int>
#
# M109 不再由本插件重写。多热端场景下用户可自行写 [gcode_macro M109]
# (rename_existing: M99109)，根据需要把 T 补成 current_tool 后转发给原版。

import logging

PERSIST_CURRENT_TOOL = 'current_tool'

# 等温阈值：目标温度低于此值视为未加热/冷却中，跳过等温（单位 °C）
HEAT_WAIT_MIN_TARGET = 50.

# print_stats.state 统一轮询间隔（秒）。子模块不再各自轮询，
# 由主模块单点轮询后通过 register_print_state_listener 分发。
PRINT_STATE_POLL_S = 1.0


class Multitool:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        # ---- 配置字段 ----
        self.tool_count = config.getint('tool_count', minval=1, maxval=16)
        self.z_hop = config.getfloat('z_hop', 0.4, minval=0.)
        self.feed_z = config.getint('feed_z', 600, minval=1)
        self.accel_swap = config.getfloat(
            'accel_swap', 8000., above=0.)
        self.untool_safe_z = config.getfloat(
            'untool_safe_z', 10., minval=0.)

        # ---- 内存状态 ----
        self.current_tool = -1   # -1 表示无热端
        self.base_tool = -1      # Z 偏移自适应基准；由 offsets 模块维护
        self.active = False      # 是否正在切换中

        # print_stats.state 单点轮询：子模块通过
        # register_print_state_listener 注册回调，避免各自重复轮询。
        self._print_state_listeners = []
        self._last_print_state = None

        # ---- 事件处理 ----
        # connect: 所有 section 已加载，注册命令并预检冲突
        # ready  : Klipper 已就绪，从 save_variables 恢复 current_tool
        self.printer.register_event_handler(
            'klippy:connect', self._on_connect)
        self.printer.register_event_handler(
            'klippy:ready', self._on_ready)

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------
    def _on_ready(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is not None:
            v = getattr(sv, 'allVariables', {}) or {}
            try:
                self.current_tool = int(v.get(PERSIST_CURRENT_TOOL, -1))
            except (TypeError, ValueError):
                self.current_tool = -1

        # 启动统一的 print_stats.state 轮询定时器（子模块共享）
        reactor = self.printer.get_reactor()
        reactor.register_timer(
            self._poll_print_state,
            reactor.monotonic() + PRINT_STATE_POLL_S)

    # ------------------------------------------------------------------
    # print_stats.state 单点轮询 + 分发
    #   子模块（offsets / stats）通过 register_print_state_listener 注册
    #   回调 fn(prev_state, cur_state)，避免每个子模块各开一个定时器。
    # ------------------------------------------------------------------
    def register_print_state_listener(self, callback):
        self._print_state_listeners.append(callback)

    def _poll_print_state(self, eventtime):
        try:
            ps = self.printer.lookup_object('print_stats', None)
            if ps is not None:
                cur_state = ps.get_status(eventtime).get('state')
                prev_state = self._last_print_state
                if cur_state != prev_state:
                    self._last_print_state = cur_state
                    for cb in self._print_state_listeners:
                        try:
                            cb(prev_state, cur_state)
                        except Exception:
                            logging.exception(
                                "multitool: print_state listener error")
        except Exception:
            logging.exception("multitool: poll_print_state error")
        return eventtime + PRINT_STATE_POLL_S

    # ------------------------------------------------------------------
    # connect 阶段：注册命令 + 预检冲突
    #   放在 connect (而不是 __init__) 是因为：
    #   1) 必须等所有 [gcode_macro Tn] 加载完成，才能正确探测冲突
    #   2) 若我们在 __init__ 注册 T0，用户的 [gcode_macro T0] 加载时
    #      会反过来覆盖我们的注册，错误现象更隐蔽
    # ------------------------------------------------------------------
    def _on_connect(self):
        # 所有可能冲突的命令名（gcode_handlers 是公共 dict）
        names = ['T%d' % i for i in range(self.tool_count)]
        names += ['UNTOOL', 'CHANGE_TOOL']
        existing = self.gcode.gcode_handlers
        conflicts = [n for n in names if n in existing]
        if conflicts:
            raise self.printer.command_error(
                "[multitool] 以下命令已被其他 section 注册（可能是旧的 "
                "[gcode_macro] 残留），请删除后重启：%s"
                % ', '.join(conflicts))

        # 注册 T0..T{n-1}
        for i in range(self.tool_count):
            self.gcode.register_command(
                'T%d' % i,
                self._make_tool_handler(i),
                desc='切换到 T%d' % i)

        # UNTOOL / CHANGE_TOOL
        self.gcode.register_command(
            'UNTOOL', self.cmd_UNTOOL, desc='卸下当前热端')
        self.gcode.register_command(
            'CHANGE_TOOL', self.cmd_CHANGE_TOOL,
            desc='CHANGE_TOOL T=<int>  -1 表示卸下')

        # 辅助命令
        self.gcode.register_command(
            'QUERY_TOOL_STATUS', self.cmd_QUERY_TOOL_STATUS,
            desc='查询当前热端编号 / 持久化值')

    def _make_tool_handler(self, tool_index):
        def _handler(gcmd):
            self._do_change_tool(gcmd, tool_index)
        return _handler

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------
    def cmd_UNTOOL(self, gcmd):
        self._do_change_tool(gcmd, -1)

    def cmd_CHANGE_TOOL(self, gcmd):
        new_tool = gcmd.get_int('T', minval=-1, maxval=self.tool_count - 1)
        self._do_change_tool(gcmd, new_tool)

    def cmd_QUERY_TOOL_STATUS(self, gcmd):
        sv = self.printer.lookup_object('save_variables', None)
        disk = -1
        if sv is not None:
            v = getattr(sv, 'allVariables', {}) or {}
            try:
                disk = int(v.get(PERSIST_CURRENT_TOOL, -1))
            except (TypeError, ValueError):
                disk = -1

        cur_cn = "无热端" if self.current_tool == -1 else "T%d" % self.current_tool
        disk_cn = "无热端" if disk == -1 else "T%d" % disk
        base_cn = "未设置" if self.base_tool == -1 else "T%d" % self.base_tool

        gcmd.respond_info("====== 当前换热端状态 ======")
        gcmd.respond_info("当前热端: %s (内存 current_tool=%d)"
                          % (cur_cn, self.current_tool))
        gcmd.respond_info("持久化值: %s (myvariables.cfg)" % disk_cn)
        gcmd.respond_info("基准热端: %s" % base_cn)
        gcmd.respond_info("工具数量: %d (T0..T%d)"
                          % (self.tool_count, self.tool_count - 1))

    # ------------------------------------------------------------------
    # 主流程：换热端
    # ------------------------------------------------------------------
    def _do_change_tool(self, gcmd, new_tool):
        # ---- 重入保护 ----
        # G-code 是串行的，但用户钩子宏内部仍可能误触发 T*/CHANGE_TOOL/UNTOOL，
        # 形成嵌套调用。嵌套会破坏 saved_accel / SAVE_GCODE_STATE 名字 /
        # stats 计时等关键状态，且故障现象往往与换头本身相隔很远 (例如
        # max_accel 永久错乱)，调试成本极高。这里在最早期直接拒绝。
        if self.active:
            raise self.printer.command_error(
                "[multitool] 当前正在换头中，禁止重入。"
                "请检查钩子宏 (multitool_release_tool / "
                "multitool_pickup_tool) 是否调用了 T*/CHANGE_TOOL/UNTOOL。")

        old_tool = self.current_tool
        if new_tool == old_tool:
            cur_cn = "无热端" if new_tool == -1 else "T%d" % new_tool
            gcmd.respond_info(
                "目标状态与当前一致 (%s)，无需操作" % cur_cn)
            return

        if new_tool == -1:
            gcmd.respond_info("收到卸载指令：正在卸载 T%d ..." % old_tool)
        else:
            gcmd.respond_info("切换工具: T%d -> T%d" % (old_tool, new_tool))

        clamp = self.printer.lookup_object('multitool_clamp', None)
        offsets = self.printer.lookup_object('multitool_offsets', None)
        stats = self.printer.lookup_object('multitool_stats', None)

        # 备份 accel；try/finally 保证恢复
        toolhead = self.printer.lookup_object('toolhead')
        saved_accel = toolhead.max_accel

        # accel 切换状态：是否已经做了 SET_VELOCITY_LIMIT/SAVE_GCODE_STATE，
        # 入口校验失败时不需要恢复
        prepared = False
        self.active = True
        succeeded = False
        try:
            # ---- 入口校验（在 try 内：与对称性 + 异常恢复一致）----
            if clamp is not None:
                expect = 'clamped' if old_tool != -1 else 'released'
                clamp.assert_state(expect, reason='入口校验')

            # ---- 准备：保存状态 / 切 accel / 抬升 / 清偏移 ----
            self.gcode.run_script_from_command(
                "SAVE_GCODE_STATE NAME=_tc_change_tool")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT ACCEL=%.0f" % self.accel_swap)
            prepared = True
            self.gcode.run_script_from_command("G91")
            self.gcode.run_script_from_command(
                "G0 Z%.3f F%d" % (self.z_hop, self.feed_z))
            self.gcode.run_script_from_command("G90")
            if old_tool == -1:
                # 上次无热端，先抬到安全 Z 再做后续动作
                self.gcode.run_script_from_command(
                    "G0 Z%.3f F%d" % (self.untool_safe_z, self.feed_z))
            if offsets is not None:
                self.gcode.run_script_from_command(
                    "SET_GCODE_OFFSET X=0 Y=0 Z=0")

            if stats is not None:
                stats.tc_begin()

            # ---- 释放旧热端 ----
            if old_tool != -1:
                if stats is not None:
                    stats.stage_begin('release')
                self._invoke_hook('multitool_release_tool', old_tool)
                if stats is not None:
                    stats.stage_end('release')
                if clamp is not None:
                    clamp.assert_state('released', reason='释放后校验')
                self._set_current_tool(-1)

            # ---- 抓取新热端 ----
            if new_tool != -1:
                if stats is not None:
                    stats.stage_begin('pickup')
                self._invoke_hook('multitool_pickup_tool', new_tool)
                if stats is not None:
                    stats.stage_end('pickup')
                if clamp is not None:
                    clamp.assert_state('clamped', reason='抓取后校验')
                self._set_current_tool(new_tool)

                # 等温
                if stats is not None:
                    stats.stage_begin('heat_wait')
                self._wait_heater(new_tool)
                if stats is not None:
                    stats.stage_end('heat_wait')

            # 走到这里说明全流程无异常
            succeeded = True

        finally:
            # 仅在确实做过准备的情况下恢复（入口校验失败时无需恢复）
            if prepared:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=_tc_change_tool MOVE=0")
                self.gcode.run_script_from_command(
                    "SET_VELOCITY_LIMIT ACCEL=%.0f" % saved_accel)
            if stats is not None:
                # 成功才提交计时；失败（钩子抛错 / 夹紧自检失败等）丢弃，
                # 避免把未完成的换头计入次数/耗时，污染统计。
                if succeeded:
                    stats.tc_commit()
                else:
                    stats.tc_abort()
            self.active = False

        # ---- 偏移应用 (在异常路径下不需要) ----
        if new_tool != -1 and offsets is not None:
            offsets.apply(new_tool, base_tool=self.base_tool)

    # ------------------------------------------------------------------
    # 内部：状态写入 + 落盘
    # ------------------------------------------------------------------
    def _set_current_tool(self, tool):
        self.current_tool = tool
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=%d"
            % (PERSIST_CURRENT_TOOL, tool))

    # ------------------------------------------------------------------
    # 内部：调用用户钩子
    #   - 集中做 TOOL 参数合法性校验，钩子里不再需要写校验代码
    #   - 钩子未实现时给出明确报错（区别于 Klipper 默认的 "Unknown command"）
    #   - 钩子内部抛错会冒泡到主流程的 try/finally
    # ------------------------------------------------------------------
    def _invoke_hook(self, name, tool):
        # 校验：tool 必须在 [0, tool_count-1]
        if not isinstance(tool, int) or tool < 0 or tool >= self.tool_count:
            raise self.printer.command_error(
                "[multitool] 调用钩子 %s 时 TOOL=%r 非法 "
                "(应在 0..%d 之间)" % (name, tool, self.tool_count - 1))
        # 校验：钩子宏已注册
        if name not in self.gcode.gcode_handlers:
            raise self.printer.command_error(
                "[multitool] 用户钩子 [gcode_macro %s] 未定义。"
                "请在 printer.cfg 中实现该宏。" % name)
        self.gcode.run_script_from_command("%s TOOL=%d" % (name, tool))

    def _wait_heater(self, tool):
        # 等待热端到达目标温度。
        # 不做 last_temp 提前短路：升温时多走一次 TEMPERATURE_WAIT 没问题，
        # 但降温时短路会误判（last_temp 仍在高位），因此交给 TEMPERATURE_WAIT
        # 自身处理（用 MINIMUM/MAXIMUM 双向收敛）。
        section = 'extruder' if tool == 0 else 'extruder%d' % tool
        extruder = self.printer.lookup_object(section, None)
        if extruder is None:
            return
        heater = extruder.get_heater()
        target = heater.target_temp
        # 目标温度低于该阈值视为"未加热/正在冷却"（standby、关闭、残留小目标等），
        # 等温没有意义，直接跳过。热端正常出料远高于此值。
        if target <= HEAT_WAIT_MIN_TARGET:
            return
        self.gcode.respond_info("T%d 等温中 (target=%.1f)..." % (tool, target))
        self.gcode.run_script_from_command(
            "TEMPERATURE_WAIT SENSOR=%s MINIMUM=%.2f MAXIMUM=%.2f"
            % (section, target - 1.5, target + 1.5))

    # ------------------------------------------------------------------
    # 暴露给前端 / 宏
    # ------------------------------------------------------------------
    def get_status(self, eventtime):
        return {
            'current_tool': self.current_tool,
            'base_tool': self.base_tool,
            'tool_count': self.tool_count,
            'active': self.active,
            'tools': ['T%d' % i for i in range(self.tool_count)],
        }


def load_config(config):
    return Multitool(config)
