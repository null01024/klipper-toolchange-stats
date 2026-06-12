#!/usr/bin/env python3
# Klipper Multitool - 计时统计子模块
#
# 自动行为（无任何手动入口）：
#   1. 主模块调用 tc_begin/stage_begin/stage_end/tc_commit/tc_abort (Python API)
#      在每次换热端时累积 current/print/total 三段统计（成功 commit，失败 abort）
#   2. 监听 print_stats.state：
#        - 进入 printing → 自动重置本次打印 (print 段)
#        - 离开 printing 进入 complete/cancelled/error → 自动输出 SCOPE=all 报告
#   3. 启动后若历史累计有数据，延迟 boot_banner_delay_s 秒后输出一行 banner
#
# 不再注册任何手动 G-code 命令；行为完全由插件自动驱动。
#
# 持久化字段命名沿用旧版 (tc_total_*)，迁移用户改完 section 名
# (multitoolr_stats → multitool_stats) 后历史数据自动延续。

from time import monotonic


# 换热端阶段定义（按发生顺序）
TOOLCHANGE_STAGES = ('release', 'pickup', 'heat_wait')

# print_stats.state 视为"打印中"的状态值
PRINTING_STATES = ('printing',)
# print_stats.state 视为"打印结束/中止"的状态值
ENDED_STATES = ('complete', 'cancelled', 'error', 'standby')


class MultitoolStats:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.persist_prefix = config.get('persist_keys_prefix', 'tc_total_')
        self.boot_banner_delay_s = config.getfloat(
            'boot_banner_delay_s', 5.0, minval=0.)

        # 持久化键
        self._persist_keys = {
            'count':     self.persist_prefix + 'count',
            'elapsed':   self.persist_prefix + 'elapsed',
            'release':   self.persist_prefix + 'release',
            'pickup':    self.persist_prefix + 'pickup',
            'heat_wait': self.persist_prefix + 'heat_wait',
        }

        # 三段式数据
        self._reset_current()
        self._print = self._empty_stats()
        self._total = self._empty_stats()

        self.printer.register_event_handler('klippy:ready', self._on_ready)

    # ------------------------------------------------------------------
    # 数据结构辅助
    # ------------------------------------------------------------------
    def _empty_stats(self):
        return {
            'count': 0,
            'elapsed': 0.0,
            'stages': {s: 0.0 for s in TOOLCHANGE_STAGES},
        }

    def _reset_current(self):
        self._current = {
            'active': False,
            'start': 0.0,
            'elapsed': 0.0,
            'stage_start': {s: 0.0 for s in TOOLCHANGE_STAGES},
            'stages': {s: 0.0 for s in TOOLCHANGE_STAGES},
        }

    # ------------------------------------------------------------------
    # 启动加载 / 持久化
    # ------------------------------------------------------------------
    def _on_ready(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is not None:
            v = getattr(sv, 'allVariables', {}) or {}
            try:
                self._total['count'] = int(
                    v.get(self._persist_keys['count'], 0))
                self._total['elapsed'] = float(
                    v.get(self._persist_keys['elapsed'], 0.0))
                for s in TOOLCHANGE_STAGES:
                    self._total['stages'][s] = float(
                        v.get(self._persist_keys[s], 0.0))
            except (TypeError, ValueError):
                self._total = self._empty_stats()

        if self._total['count'] > 0 and self.boot_banner_delay_s > 0.:
            reactor = self.printer.get_reactor()
            reactor.register_callback(
                self._delayed_banner,
                reactor.monotonic() + self.boot_banner_delay_s)

        # 注册到主模块的统一 print_stats.state 轮询（不再各自开定时器）
        tc = self.printer.lookup_object('multitool', None)
        if tc is not None:
            tc.register_print_state_listener(self._on_print_state_changed)

    # ------------------------------------------------------------------
    # 自动跟随 print_stats.state（由主模块统一轮询后回调）
    #   - 进入 printing → 自动重置本次打印统计
    #   - 离开 printing → 自动输出报告
    # ------------------------------------------------------------------
    def _on_print_state_changed(self, prev, cur):
        # 进入打印态：清空本次打印统计。
        # 'paused' -> 'printing' 是续打/手动恢复，不算新打印开始，
        # 否则会在续打/恢复后误清空本次打印统计。
        if (cur in PRINTING_STATES and prev not in PRINTING_STATES
                and prev != 'paused'):
            self._print = self._empty_stats()
            self._reset_current()
            self.gcode.respond_info(
                "[multitool_stats] 检测到打印开始，自动重置本次统计")

        # 离开打印态进入结束态：输出报告。
        # 注意只认 ENDED_STATES，'paused' 不算结束，避免暂停时误触发报告。
        if prev in PRINTING_STATES and cur in ENDED_STATES:
            if self._print['count'] > 0:
                self.gcode.respond_info(
                    "[multitool_stats] 检测到打印结束 (state=%s)，"
                    "输出本次统计：" % cur)
                self._auto_report()

    def _auto_report(self):
        self._report_block('本次打印', self._print)
        self._report_block('历史累计', self._total)

    def _report_block(self, title, data):
        self.gcode.respond_info('=== 换热端统计 (%s) ===' % title)
        self.gcode.respond_info('换热端次数: %d' % data['count'])
        self.gcode.respond_info('总耗时:   %.3f 秒' % data['elapsed'])
        if data['count'] > 0:
            self.gcode.respond_info(
                '平均耗时: %.3f 秒' % (data['elapsed'] / data['count']))
        for s in TOOLCHANGE_STAGES:
            v = data['stages'][s]
            avg = (v / data['count']) if data['count'] else 0.0
            self.gcode.respond_info(
                '阶段 %-9s: 累计=%.3fs 平均=%.3fs' % (s, v, avg))

    def _delayed_banner(self, _eventtime):
        count = self._total['count']
        elapsed = self._total['elapsed']
        avg = (elapsed / count) if count else 0.0
        self.gcode.respond_info(
            "[multitool_stats] 历史累计: %d 次换热端, "
            "总耗时 %.1fs, 平均 %.3fs/次"
            % (count, elapsed, avg))

    def _save_total(self):
        keys = self._persist_keys
        cmds = [
            "SAVE_VARIABLE VARIABLE=%s VALUE=%d"
            % (keys['count'], self._total['count']),
            "SAVE_VARIABLE VARIABLE=%s VALUE=%.6f"
            % (keys['elapsed'], self._total['elapsed']),
        ]
        for s in TOOLCHANGE_STAGES:
            cmds.append("SAVE_VARIABLE VARIABLE=%s VALUE=%.6f"
                        % (keys[s], self._total['stages'][s]))
        for c in cmds:
            self.gcode.run_script_from_command(c)

    # ------------------------------------------------------------------
    # 暴露给 G-code 模板的状态
    # ------------------------------------------------------------------
    def get_status(self, _eventtime):
        return {
            'tc_current': {
                'active': self._current['active'],
                'elapsed': self._current['elapsed'],
                'stages': self._current['stages'].copy(),
            },
            'tc_print': {
                'count': self._print['count'],
                'elapsed': self._print['elapsed'],
                'stages': self._print['stages'].copy(),
            },
            'tc_total': {
                'count': self._total['count'],
                'elapsed': self._total['elapsed'],
                'stages': self._total['stages'].copy(),
            },
        }

    # ------------------------------------------------------------------
    # Python API：供 multitool 主模块直接调用（避免 G-code 往返）
    # ------------------------------------------------------------------
    def tc_begin(self):
        if self._current['active']:
            self.gcode.respond_info(
                '警告: 上一次换热端计时未结束，已自动重置')
        self._reset_current()
        self._current['active'] = True
        self._current['start'] = monotonic()

    def stage_begin(self, stage):
        if stage not in TOOLCHANGE_STAGES:
            return
        if not self._current['active']:
            return
        self._current['stage_start'][stage] = monotonic()

    def stage_end(self, stage):
        if stage not in TOOLCHANGE_STAGES:
            return
        start = self._current['stage_start'][stage]
        if start <= 0:
            return
        self._current['stages'][stage] = monotonic() - start
        self._current['stage_start'][stage] = 0.0

    def tc_commit(self):
        # 换头成功完成时调用：把本次计时累加进 print/total 并立即落盘。
        # 与 tc_abort 二选一，由主模块在 try/finally 中按成功/失败分别调用。
        if not self._current['active']:
            return
        now = monotonic()
        for s in TOOLCHANGE_STAGES:
            ss = self._current['stage_start'][s]
            if ss > 0:
                self._current['stages'][s] = now - ss
                self._current['stage_start'][s] = 0.0

        elapsed = now - self._current['start']
        self._current['elapsed'] = elapsed
        self._current['active'] = False
        stages = self._current['stages']

        self._print['count'] += 1
        self._print['elapsed'] += elapsed
        for s in TOOLCHANGE_STAGES:
            self._print['stages'][s] += stages[s]

        self._total['count'] += 1
        self._total['elapsed'] += elapsed
        for s in TOOLCHANGE_STAGES:
            self._total['stages'][s] += stages[s]

        # 每次成功换头立即落盘：保证异常退出也不丢历史累计，代价是写盘较频繁。
        self._save_total()

        self.gcode.respond_info(
            "换热端 #%d 耗时 %.3fs (释放=%.3fs 抓取=%.3fs 等温=%.3fs)"
            % (self._print['count'], elapsed,
               stages['release'], stages['pickup'], stages['heat_wait']))

    def tc_abort(self):
        # 换头失败（钩子抛错 / 夹紧自检失败等）时调用：丢弃本次计时，
        # 不计入 count/elapsed，避免污染平均耗时。
        self._reset_current()


def load_config(config):
    return MultitoolStats(config)
