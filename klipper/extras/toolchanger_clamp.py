#!/usr/bin/env python3
# Klipper Toolchanger - 夹紧检测子模块
#
# 职责：
#   - 用 buttons helper 注册一个输入 pin，监听夹紧开关电平
#   - 提供 assert_state(expect, reason) 给 toolchanger 主流程在
#     钩子前后置自动调用
#   - 提供 QUERY_CLAMP_STATUS 给用户排查
#
# 约定：
#   - 物理"已夹紧" → buttons helper 上报 PRESSED → 内部 state = 'clamped'
#   - 物理"未夹紧" → buttons helper 上报 RELEASED → 内部 state = 'released'
#   - 用户在 pin 配置中用 ! / ^ / ~ 控制电平，模块本身不再有 pressed_value 字段
#
# 初始状态种子化：
#   - buttons helper 仅在电平"变化"时才回调；如果开机后开关电平一直保持，
#     _state 会一直是 None，导致首次换头入口校验直接卡死。
#   - 启动时记录一个时间戳，assert_state 在 _state 仍为 None 时：
#       * 按"期望状态"做一次种子化（视为机器一直保持期望态），首次换头放行
#       * 同时 respond_info 警告，提示用户检查接线/电平修饰符
#       * 任何一次真实电平变化都会立即覆盖种子值，因此不会掩盖后续异常

import logging

# 启动后给 buttons helper 多少秒来自然上报第一帧；超过则进入种子化路径
BOOT_GRACE_S = 2.0


class ToolchangerClamp:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.pin = config.get('pin')
        self.settle_ms = config.getint('settle_ms', 50, minval=0)

        # 内部状态：'clamped' / 'released'，None 表示尚未收到任何上报
        self._state = None
        # 是否走过种子化路径（仅用于避免重复打印警告 + 排错可见性）
        self._seeded = False
        # ready 时间戳；assert_state 在此前不种子化
        self._ready_time = None

        # 注册输入引脚（^/~/! 由 pins.lookup_pin 自动处理）
        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([self.pin], self._on_button)

        self.printer.register_event_handler('klippy:ready', self._on_ready)

        # 命令
        self.gcode.register_command(
            'QUERY_CLAMP_STATUS', self.cmd_QUERY_CLAMP_STATUS,
            desc='查询当前夹紧开关状态')

    def _on_ready(self):
        reactor = self.printer.get_reactor()
        self._ready_time = reactor.monotonic()
        # 启动一段宽限期后还是 None 就 logging 一条，方便日志侧排查
        reactor.register_callback(
            self._check_initial_report,
            self._ready_time + BOOT_GRACE_S)

    def _check_initial_report(self, _eventtime):
        if self._state is None:
            logging.warning(
                "toolchanger_clamp: 启动 %.1fs 后仍未收到 buttons 状态上报"
                " (pin=%s)；首次 assert_state 将按期望状态做种子化。"
                " 若与实际机械状态不符，请检查 pin 接线 / 电平修饰符。",
                BOOT_GRACE_S, self.pin)

    # ------------------------------------------------------------------
    # buttons 回调：state=1 → PRESSED → clamped
    # ------------------------------------------------------------------
    def _on_button(self, eventtime, state):
        self._state = 'clamped' if state else 'released'
        # 任何一次真实回调都让"种子化"标记失效（之后的状态都是真实的）
        self._seeded = False

    # ------------------------------------------------------------------
    # 公共方法：被 toolchanger 主流程调用
    # ------------------------------------------------------------------
    def assert_state(self, expect, reason=''):
        if expect not in ('clamped', 'released'):
            raise self.printer.command_error(
                "_clamp.assert_state 参数错误: expect=%s" % expect)

        # 等运动队列清空 + 去抖
        self.gcode.run_script_from_command("M400")
        if self.settle_ms > 0:
            self.gcode.run_script_from_command("G4 P%d" % self.settle_ms)

        actual = self._state
        if actual is None:
            # buttons helper 启动后只在电平变化时才回调；若开机后从未变化过，
            # _state 会保持 None。此时按"期望状态"做一次种子化，避免首次换头
            # 直接卡死；同时打印警告提醒用户检查接线/电平。
            self._state = expect
            self._seeded = True
            actual = expect
            self.gcode.respond_info(
                "[夹紧自检] 警告: 启动后未收到 buttons 状态上报 (pin=%s)，"
                "按期望状态='%s' 做一次种子化以放行首次换头。"
                "若与实际不符请检查 pin 接线 / 电平修饰符 (! ^ ~)。"
                % (self.pin, expect))

        if actual != expect:
            tc = self.printer.lookup_object('toolchanger', None)
            cur = tc.current_tool if tc is not None else -1
            expect_cn = "已夹紧" if expect == 'clamped' else "已释放"
            actual_cn = "已夹紧" if actual == 'clamped' else "已释放"
            msg = ("夹紧自检失败 (原因=%s) 期望=%s 实际=%s 当前热端=T%d"
                   % (reason, expect_cn, actual_cn, cur))
            self.gcode.respond_info(msg)
            raise self.printer.command_error(msg)
        else:
            expect_cn = "已夹紧" if expect == 'clamped' else "已释放"
            tag = "(种子化)" if self._seeded else ""
            self.gcode.respond_info(
                "[夹紧自检] 通过%s (原因=%s) 期望=%s 实际=%s"
                % (tag, reason, expect_cn, expect_cn))

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def cmd_QUERY_CLAMP_STATUS(self, gcmd):
        raw = ('PRESSED' if self._state == 'clamped'
               else 'RELEASED' if self._state == 'released'
               else 'UNKNOWN')
        cn = ('已夹紧' if self._state == 'clamped'
              else '已释放' if self._state == 'released'
              else '未知')
        seeded = " (种子化, 尚未收到真实上报)" if self._seeded else ""
        gcmd.respond_info("夹紧开关: %s%s (raw=%s)" % (cn, seeded, raw))

    # ------------------------------------------------------------------
    # 暴露给前端 / 宏
    # ------------------------------------------------------------------
    def get_status(self, eventtime):
        return {
            'state': self._state if self._state is not None else 'unknown',
            'raw_state': ('PRESSED' if self._state == 'clamped'
                          else 'RELEASED' if self._state == 'released'
                          else 'UNKNOWN'),
            'seeded': self._seeded,
        }


def load_config(config):
    return ToolchangerClamp(config)
