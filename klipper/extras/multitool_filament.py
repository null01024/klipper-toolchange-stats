#!/usr/bin/env python3
# Klipper Multitool - 耗材检查子模块
#
# 职责：
#   - 用 buttons helper 集中注册各通道的耗材检测 pin
#   - 通道电平变化时在控制台输出装载 / 卸载提示 (M118)
#   - 提供 assert_loaded(channel) 给 multitool 主流程在换头前调用：
#     目标工具头通道无耗材时阻止切换
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
# 配置示例：
#   [multitool_filament]
#   channel_count: 8
#   boot_grace_s: 5
#   pin_0: ^multihotend:IO0
#   pin_1: ^multihotend:IO1
#   ...
#   pin_7: ^multihotend:IO7


class MultitoolFilament:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.channel_count = config.getint('channel_count', minval=1)
        self.boot_grace_s = config.getfloat(
            'boot_grace_s', 5., minval=0.)

        buttons = self.printer.load_object(config, 'buttons')

        # 各通道装载状态：None 表示尚未收到任何上报
        self._loaded = [None] * self.channel_count

        for ch in range(self.channel_count):
            pin = config.get('pin_%d' % ch)
            buttons.register_buttons([pin], self._make_callback(ch))

        self.printer.register_event_handler('klippy:ready', self._on_ready)

    def _on_ready(self):
        # 启动后等 boot_grace_s 秒，让 buttons helper 把当前电平上报完，
        # 再把仍为 None 的通道落定为"已卸载"并输出一份状态总览。
        reactor = self.printer.get_reactor()
        reactor.register_callback(
            self._seed_and_report,
            reactor.monotonic() + self.boot_grace_s)

    def _seed_and_report(self, _eventtime):
        for ch in range(self.channel_count):
            if self._loaded[ch] is None:
                # 启动宽限期内未收到回调 → 电平一直是 RELEASED → 已卸载
                self._loaded[ch] = False
        lines = []
        for ch in range(self.channel_count):
            lines.append("通道%d=%s" % (
                ch, '已装载' if self._loaded[ch] else '已卸载'))
        self.gcode.respond_info(
            "[耗材检查] 启动状态总览: %s" % ', '.join(lines))

    def _make_callback(self, channel):
        def _callback(eventtime, state):
            self._on_button(channel, state)
        return _callback

    def _on_button(self, channel, state):
        loaded = bool(state)
        self._loaded[channel] = loaded
        action = '已装载' if loaded else '已卸载'
        self.gcode.run_script_from_command(
            "M118 通道%d，耗材%s" % (channel, action))

    # ------------------------------------------------------------------
    # 公共方法：被 multitool 主流程在换头前调用
    #   - 通道未配置 (channel >= channel_count) → 视为有耗材，不阻塞
    #   - 状态未知 (None，开机后从未上报过电平变化) → 视为有耗材，仅警告，
    #     不阻塞（buttons helper 仅在电平变化时回调，与 clamp 模块同样的取舍）
    #   - 明确无耗材 (False) → 抛错阻止换头
    # ------------------------------------------------------------------
    def assert_loaded(self, channel, reason=''):
        if channel < 0 or channel >= self.channel_count:
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

    def get_status(self, eventtime):
        return {
            'channel_count': self.channel_count,
            'loaded': list(self._loaded),
        }


def load_config(config):
    return MultitoolFilament(config)
