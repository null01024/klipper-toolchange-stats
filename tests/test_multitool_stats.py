import ast
import shlex
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1] / 'klipper' / 'extras'
sys.path.insert(0, str(MODULE_DIR))

import multitool_stats as stats_module


class FakeGcode:
    def __init__(self, save_variables):
        self.save_variables = save_variables
        self.commands = []
        self.messages = []
        self.fail_variable = None

    def run_script_from_command(self, command):
        self.commands.append(command)
        name, _, rawparams = command.partition(' ')
        if name != 'SAVE_VARIABLE':
            return

        lexer = shlex.shlex(rawparams, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = '#;'
        params = dict(part.split('=', 1) for part in lexer)
        variable = params['VARIABLE']
        if variable == self.fail_variable:
            self.fail_variable = None
            raise RuntimeError('simulated persistence failure')
        self.save_variables.allVariables[variable] = ast.literal_eval(
            params['VALUE'])

    def respond_info(self, message):
        self.messages.append(message)


class FakeSaveVariables:
    def __init__(self, variables=None):
        self.allVariables = dict(variables or {})


class FakePrinter:
    def __init__(self, variables=None):
        self.save_variables = FakeSaveVariables(variables)
        self.gcode = FakeGcode(self.save_variables)
        self.handlers = {}

    def lookup_object(self, name, default=None):
        if name == 'gcode':
            return self.gcode
        if name == 'save_variables':
            return self.save_variables
        return default

    def register_event_handler(self, event, handler):
        self.handlers[event] = handler


class FakeConfig:
    def __init__(self, variables=None):
        self.printer = FakePrinter(variables)

    def get_printer(self):
        return self.printer

    def get(self, _name, default=None):
        return default

    def getfloat(self, name, default, minval=None):
        if name == 'boot_banner_delay_s':
            return 0.0
        return default


class MultitoolDailyStatsTest(unittest.TestCase):
    def make_stats(self, today, variables=None):
        config = FakeConfig(variables)
        stats = stats_module.MultitoolStats(config)
        stats._today = lambda: today
        stats._on_ready()
        return stats, config.printer.gcode

    def test_upgrade_starts_daily_counts_at_zero_without_resetting_total(self):
        stats, _ = self.make_stats(
            date(2026, 7, 21),
            {
                'tc_total_count': 12,
                'tc_total_elapsed': 30.0,
            },
        )

        status = stats.get_status(0.0)

        self.assertEqual(12, status['tc_total']['count'])
        self.assertEqual(
            [0, 0, 0, 0, 0, 0, 0],
            [entry['count'] for entry in status['tc_daily']],
        )
        self.assertEqual('2026-07-15', status['tc_daily'][0]['date'])
        self.assertEqual('2026-07-21', status['tc_daily'][-1]['date'])

    def test_successful_commits_increment_and_persist_the_matching_day(self):
        current_day = [date(2026, 7, 20)]
        stats, gcode = self.make_stats(current_day[0])
        stats._today = lambda: current_day[0]

        with patch.object(stats_module, 'monotonic', side_effect=[10.0, 12.0, 20.0, 23.0, 30.0, 31.0]):
            stats.tc_begin()
            stats.tc_commit()
            stats.tc_begin()
            stats.tc_commit()
            current_day[0] = date(2026, 7, 21)
            stats.tc_begin()
            stats.tc_commit()

        daily = stats.get_status(0.0)['tc_daily']
        self.assertEqual(2, daily[-2]['count'])
        self.assertEqual(1, daily[-1]['count'])
        self.assertEqual(
            daily,
            gcode.save_variables.allVariables['tc_total_daily'],
        )

        reloaded, _ = self.make_stats(
            current_day[0], gcode.save_variables.allVariables)
        self.assertEqual(daily, reloaded.get_status(0.0)['tc_daily'])

    def test_persistence_failure_does_not_abort_and_next_commit_catches_up(self):
        stats, gcode = self.make_stats(date(2026, 7, 21))
        gcode.fail_variable = 'tc_total_daily'

        with self.assertLogs(level='ERROR') as logs, patch.object(
                stats_module, 'monotonic',
                side_effect=[10.0, 12.0, 20.0, 23.0]):
            stats.tc_begin()
            stats.tc_commit()
            stats.tc_begin()
            stats.tc_commit()

        status = stats.get_status(0.0)
        self.assertEqual(2, status['tc_total']['count'])
        self.assertEqual(2, status['tc_daily'][-1]['count'])
        self.assertEqual(
            status['tc_daily'],
            gcode.save_variables.allVariables['tc_total_daily'],
        )
        self.assertEqual(
            2, gcode.save_variables.allVariables['tc_total_count'])
        self.assertTrue(any(
            '统计保存失败，本次换头继续' in message
            for message in gcode.messages
        ))
        self.assertTrue(any(
            'failed to persist statistics' in message
            for message in logs.output
        ))

    def test_abort_and_invalid_persisted_entries_do_not_add_counts(self):
        stats, _ = self.make_stats(
            date(2026, 7, 21),
            {
                'tc_total_daily': [
                    {'date': '2026-07-20', 'count': 3},
                    {'date': '2026-07-19', 'count': -1},
                    {'date': '2026-07-18', 'count': 1.5},
                    {'date': 'not-a-date', 'count': 9},
                    {'date': '2026-07-14', 'count': 7},
                ],
            },
        )

        with patch.object(stats_module, 'monotonic', return_value=10.0):
            stats.tc_begin()
            stats.tc_abort()

        daily = stats.get_status(0.0)['tc_daily']
        self.assertEqual(3, daily[-2]['count'])
        self.assertEqual(0, daily[-1]['count'])
        self.assertEqual(3, sum(entry['count'] for entry in daily))


if __name__ == '__main__':
    unittest.main()
