#!/usr/bin/env python3
# Toolchange Stats for Klipper
#
# 提供两类能力：
#   1. 时间查询 / 通用计时器 (GET_TIME / START_TIMER ...)
#   2. 换热端耗时统计 (TOOLCHANGE_TIMER_* / TOOLCHANGE_STAGE_* / TOOLCHANGE_STATS_*)
#
# 换热端统计的数据模型：
#   current : 当前进行中的一次换热端 (start / 各阶段 elapsed)
#   print   : 本次打印累计 (次数 / 总耗时 / 各阶段耗时)，PRINT_START 时清零
#   total   : 历史累计 (跨打印持久化到 save_variables)
#
# 阶段定义：release(释放旧热端) -> pickup(抓取新热端) -> heat_wait(等待加热)
# 任何阶段都是可选的：未 BEGIN 的阶段在 END 时会被静默跳过。

import time
import datetime


# 换热端阶段定义（按发生顺序）
TOOLCHANGE_STAGES = ('release', 'pickup', 'heat_wait')

# 持久化变量名（保存到 save_variables）
PERSIST_KEYS = {
    'count':     'tc_total_count',
    'elapsed':   'tc_total_elapsed',
    'release':   'tc_total_release',
    'pickup':    'tc_total_pickup',
    'heat_wait': 'tc_total_heat_wait',
}


class ToolchangeStats:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        # 通用命名计时器
        self.timers = {}
        # 换热端统计三段式数据
        self._reset_current()
        self._print = self._empty_stats()
        self._total = self._empty_stats()
        # 启动后从 save_variables 加载历史累计
        self.printer.register_event_handler('klippy:ready', self._on_ready)
        # 注册命令
        self._register_commands()

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
            # 每个阶段的开始时间戳，0 表示未在计时
            'stage_start': {s: 0.0 for s in TOOLCHANGE_STAGES},
            # 每个阶段的本次耗时
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
                self._total['count'] = int(v.get(PERSIST_KEYS['count'], 0))
                self._total['elapsed'] = float(
                    v.get(PERSIST_KEYS['elapsed'], 0.0))
                for s in TOOLCHANGE_STAGES:
                    self._total['stages'][s] = float(
                        v.get(PERSIST_KEYS[s], 0.0))
            except (TypeError, ValueError):
                # 持久化数据损坏时，保留为空状态
                self._total = self._empty_stats()
        # 启动后主动在控制台展示一次完整的历史累计报告
        self._report_total_on_ready()

    def _report_total_on_ready(self):
        data = self._total
        lines = ['=== 换热端统计 (历史累计) ===',
                 '换热端次数: %d' % data['count'],
                 '总耗时:   %.3f 秒' % data['elapsed']]
        if data['count'] > 0:
            lines.append('平均耗时: %.3f 秒'
                         % (data['elapsed'] / data['count']))
        for s in TOOLCHANGE_STAGES:
            v = data['stages'][s]
            avg = (v / data['count']) if data['count'] else 0.0
            lines.append('阶段 %-9s: 累计=%.3fs 平均=%.3fs' % (s, v, avg))
        for line in lines:
            self.gcode.respond_info(line)

    def _save_total(self):
        # 一次性写回所有持久化变量
        cmds = [
            "SAVE_VARIABLE VARIABLE=%s VALUE=%d"
            % (PERSIST_KEYS['count'], self._total['count']),
            "SAVE_VARIABLE VARIABLE=%s VALUE=%.6f"
            % (PERSIST_KEYS['elapsed'], self._total['elapsed']),
        ]
        for s in TOOLCHANGE_STAGES:
            cmds.append("SAVE_VARIABLE VARIABLE=%s VALUE=%.6f"
                        % (PERSIST_KEYS[s], self._total['stages'][s]))
        for c in cmds:
            self.gcode.run_script_from_command(c)

    # ------------------------------------------------------------------
    # 暴露给 G-code 模板的状态
    # ------------------------------------------------------------------
    def get_status(self, _eventtime):
        now = datetime.datetime.now()
        return {
            'timestamp': time.time(),
            'time': now.strftime('%H:%M:%S'),
            'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
            'timers': self.timers.copy(),
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
    # 命令注册
    # ------------------------------------------------------------------
    def _register_commands(self):
        gc = self.gcode
        # 时间查询
        gc.register_command('GET_TIME', self.cmd_GET_TIME,
                            desc='获取当前时间 (HH:MM:SS)')
        gc.register_command('GET_DATE', self.cmd_GET_DATE,
                            desc='获取当前日期时间 (YYYY-MM-DD HH:MM:SS)')
        gc.register_command('GET_TIMESTAMP', self.cmd_GET_TIMESTAMP,
                            desc='获取 Unix 时间戳')
        # 通用计时器
        gc.register_command('START_TIMER', self.cmd_START_TIMER,
                            desc='START_TIMER NAME=xxx')
        gc.register_command('STOP_TIMER', self.cmd_STOP_TIMER,
                            desc='STOP_TIMER NAME=xxx')
        gc.register_command('GET_ELAPSED', self.cmd_GET_ELAPSED,
                            desc='GET_ELAPSED NAME=xxx')
        # 换热端统计
        gc.register_command('TOOLCHANGE_TIMER_BEGIN', self.cmd_TC_BEGIN,
                            desc='开始一次换热端计时')
        gc.register_command('TOOLCHANGE_TIMER_END', self.cmd_TC_END,
                            desc='结束一次换热端，自动累加到本次打印 + 历史累计并保存')
        gc.register_command('TOOLCHANGE_STAGE_BEGIN', self.cmd_TC_STAGE_BEGIN,
                            desc='TOOLCHANGE_STAGE_BEGIN STAGE=release|pickup|heat_wait')
        gc.register_command('TOOLCHANGE_STAGE_END', self.cmd_TC_STAGE_END,
                            desc='TOOLCHANGE_STAGE_END STAGE=...')
        gc.register_command('TOOLCHANGE_STATS_RESET_PRINT',
                            self.cmd_TC_RESET_PRINT,
                            desc='重置本次打印的换热端统计 (PRINT_START 时调用)')
        gc.register_command('TOOLCHANGE_STATS_RESET_TOTAL',
                            self.cmd_TC_RESET_TOTAL,
                            desc='重置历史累计换热端统计 (谨慎使用)')
        gc.register_command('TOOLCHANGE_STATS_REPORT', self.cmd_TC_REPORT,
                            desc='打印换热端统计 [SCOPE=current|print|total|all]')
        gc.register_command('TOOLCHANGE_STATS_HELP', self.cmd_TC_HELP,
                            desc='显示换热端统计扩展的全部命令')

    # ------------------------------------------------------------------
    # 时间查询命令
    # ------------------------------------------------------------------
    def cmd_GET_TIME(self, gcmd):
        gcmd.respond_info('当前时间: %s'
                          % datetime.datetime.now().strftime('%H:%M:%S'))

    def cmd_GET_DATE(self, gcmd):
        gcmd.respond_info('当前日期时间: %s'
                          % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def cmd_GET_TIMESTAMP(self, gcmd):
        gcmd.respond_info('当前时间戳: %d' % int(time.time()))

    # ------------------------------------------------------------------
    # 通用计时器命令
    # ------------------------------------------------------------------
    def cmd_START_TIMER(self, gcmd):
        name = gcmd.get('NAME', 'default')
        self.timers[name] = time.time()
        gcmd.respond_info("计时器 '%s' 已启动" % name)

    def cmd_STOP_TIMER(self, gcmd):
        name = gcmd.get('NAME', 'default')
        if name not in self.timers:
            gcmd.respond_info("计时器 '%s' 不存在" % name)
            return
        elapsed = time.time() - self.timers.pop(name)
        gcmd.respond_info("计时器 '%s': %.3f 秒" % (name, elapsed))

    def cmd_GET_ELAPSED(self, gcmd):
        name = gcmd.get('NAME', 'default')
        if name not in self.timers:
            gcmd.respond_info("计时器 '%s' 不存在" % name)
            return
        elapsed = time.time() - self.timers[name]
        gcmd.respond_info("计时器 '%s': %.3f 秒" % (name, elapsed))

    # ------------------------------------------------------------------
    # 换热端统计命令
    # ------------------------------------------------------------------
    def cmd_TC_BEGIN(self, gcmd):
        if self._current['active']:
            gcmd.respond_info('警告: 上一次换热端计时未结束，已自动重置')
        self._reset_current()
        self._current['active'] = True
        self._current['start'] = time.time()

    def cmd_TC_STAGE_BEGIN(self, gcmd):
        stage = gcmd.get('STAGE')
        if stage not in TOOLCHANGE_STAGES:
            raise gcmd.error("未知阶段 '%s'，支持: %s"
                             % (stage, ','.join(TOOLCHANGE_STAGES)))
        if not self._current['active']:
            raise gcmd.error('请先调用 TOOLCHANGE_TIMER_BEGIN')
        self._current['stage_start'][stage] = time.time()

    def cmd_TC_STAGE_END(self, gcmd):
        stage = gcmd.get('STAGE')
        if stage not in TOOLCHANGE_STAGES:
            raise gcmd.error("未知阶段 '%s'" % stage)
        start = self._current['stage_start'][stage]
        if start <= 0:
            # 阶段未开始计时（如本次没有 release 动作），静默跳过
            return
        self._current['stages'][stage] = time.time() - start
        self._current['stage_start'][stage] = 0.0

    def cmd_TC_END(self, gcmd):
        if not self._current['active']:
            gcmd.respond_info('警告: 没有进行中的换热端计时，已忽略 END')
            return
        # 异常收尾：如还有阶段未 END，按当前时刻收尾，避免数据丢失
        now = time.time()
        for s in TOOLCHANGE_STAGES:
            ss = self._current['stage_start'][s]
            if ss > 0:
                self._current['stages'][s] = now - ss
                self._current['stage_start'][s] = 0.0

        elapsed = now - self._current['start']
        self._current['elapsed'] = elapsed
        self._current['active'] = False
        stages = self._current['stages']

        # 累加到本次打印
        self._print['count'] += 1
        self._print['elapsed'] += elapsed
        for s in TOOLCHANGE_STAGES:
            self._print['stages'][s] += stages[s]

        # 累加到历史累计 + 持久化
        self._total['count'] += 1
        self._total['elapsed'] += elapsed
        for s in TOOLCHANGE_STAGES:
            self._total['stages'][s] += stages[s]
        self._save_total()

        gcmd.respond_info(
            "换热端 #%d 耗时 %.3fs (释放=%.3fs 抓取=%.3fs 等温=%.3fs)"
            % (self._print['count'], elapsed,
               stages['release'], stages['pickup'], stages['heat_wait']))

    def cmd_TC_RESET_PRINT(self, gcmd):
        self._print = self._empty_stats()
        self._reset_current()
        gcmd.respond_info('本次打印换热端统计已重置')

    def cmd_TC_RESET_TOTAL(self, gcmd):
        self._total = self._empty_stats()
        self._save_total()
        gcmd.respond_info('历史累计换热端统计已重置')

    def cmd_TC_REPORT(self, gcmd):
        scope = gcmd.get('SCOPE', 'all').lower()
        if scope not in ('current', 'print', 'total', 'all'):
            raise gcmd.error("SCOPE 必须为 current|print|total|all")
        if scope == 'current':
            self._report_current(gcmd)
            return
        if scope in ('print', 'all'):
            self._report_block(gcmd, '本次打印', self._print)
        if scope in ('total', 'all'):
            self._report_block(gcmd, '历史累计', self._total)

    def cmd_TC_HELP(self, gcmd):
        for line in (
            '=== toolchange_stats 命令一览 ===',
            '-- 时间查询 --',
            'GET_TIME / GET_DATE / GET_TIMESTAMP',
            '-- 通用计时器 --',
            'START_TIMER NAME=xxx / STOP_TIMER NAME=xxx / GET_ELAPSED NAME=xxx',
            '-- 换热端计时 (一般由 change_tool 宏自动调用) --',
            'TOOLCHANGE_TIMER_BEGIN / TOOLCHANGE_TIMER_END',
            'TOOLCHANGE_STAGE_BEGIN STAGE=release|pickup|heat_wait',
            'TOOLCHANGE_STAGE_END   STAGE=...',
            '-- 统计管理 --',
            'TOOLCHANGE_STATS_RESET_PRINT  (PRINT_START 调用)',
            'TOOLCHANGE_STATS_RESET_TOTAL  (清零历史，慎用)',
            'TOOLCHANGE_STATS_REPORT [SCOPE=current|print|total|all]',
            '================================',
        ):
            gcmd.respond_info(line)

    def _report_current(self, gcmd):
        c = self._current
        gcmd.respond_info('=== 当前换热端状态 ===')
        gcmd.respond_info('进行中: %s' % ('是' if c['active'] else '否'))
        gcmd.respond_info('上次总耗时: %.3f 秒' % c['elapsed'])
        for s in TOOLCHANGE_STAGES:
            gcmd.respond_info('阶段 %-9s: %.3f 秒' % (s, c['stages'][s]))

    def _report_block(self, gcmd, title, data):
        gcmd.respond_info('=== 换热端统计 (%s) ===' % title)
        gcmd.respond_info('换热端次数: %d' % data['count'])
        gcmd.respond_info('总耗时:   %.3f 秒' % data['elapsed'])
        if data['count'] > 0:
            gcmd.respond_info('平均耗时: %.3f 秒'
                              % (data['elapsed'] / data['count']))
        for s in TOOLCHANGE_STAGES:
            v = data['stages'][s]
            avg = (v / data['count']) if data['count'] else 0.0
            gcmd.respond_info('阶段 %-9s: 累计=%.3fs 平均=%.3fs'
                              % (s, v, avg))


def load_config(config):
    return ToolchangeStats(config)
