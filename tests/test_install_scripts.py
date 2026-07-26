import os
import shlex
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / 'install.sh'
STACK_SCRIPT = REPO_ROOT / 'install_toolchanger_stack.sh'


class InstallScriptTest(unittest.TestCase):
    def run_bash(self, body, *, input_text=None, env=None):
        command = f'source {shlex.quote(str(INSTALL_SCRIPT))}\n{body}'
        test_env = os.environ.copy()
        test_env.pop('INSTALL_MODE', None)
        if env:
            test_env.update(env)
        return subprocess.run(
            ['bash', '-c', command],
            input=input_text,
            text=True,
            capture_output=True,
            env=test_env,
            check=False,
        )

    def test_install_mode_requires_nonempty_interactive_choice(self):
        result = self.run_bash(
            'INSTALL_MODE=""\nask_install_mode\nprintf "MODE=%s\\n" "$INSTALL_MODE"',
            input_text='\n1\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('不能留空', result.stdout)
        self.assertIn('MODE=plugins', result.stdout)

    def test_install_mode_accepts_configure_choice(self):
        result = self.run_bash(
            'INSTALL_MODE=""\nask_install_mode\nprintf "MODE=%s\\n" "$INSTALL_MODE"',
            input_text='2\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MODE=configure', result.stdout)

    def test_install_mode_accepts_explicit_environment_value(self):
        result = self.run_bash(
            'ask_install_mode\nprintf "MODE=%s\\n" "$INSTALL_MODE"',
            env={'INSTALL_MODE': 'plugins'},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MODE=plugins', result.stdout)

    def test_install_mode_rejects_invalid_environment_value(self):
        result = self.run_bash(
            'ask_install_mode',
            env={'INSTALL_MODE': 'invalid'},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('plugins 或 configure', result.stderr)

    def test_install_mode_fails_cleanly_without_input(self):
        result = self.run_bash(
            'INSTALL_MODE=""\nask_install_mode',
            input_text='',
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('非交互运行时请设置 INSTALL_MODE', result.stderr)

    def install_flow_calls(self, mode):
        function_names = (
            'preflight_checks sync_repo ask_tool_calibration_scheme '
            'ask_frontend_choice link_extension link_moonraker_components '
            'patch_moonraker_lane_data_conf install_tool_calibration_python '
            'clean_orphan_links copy_config install_tool_calibration_config '
            'ask_toolchange_scheme generate_multihotend_config '
            'install_cxchanger_config patch_fresh_install_tool_count_configs '
            'patch_multitool_hooks_for_cxchanger patch_printer_cfg '
            'restart_klipper restart_moonraker_if_needed '
            'install_frontend_if_requested print_configure_completion '
            'print_plugins_completion'
        )
        body = f'''
for fn in {function_names}; do
    eval "${{fn}}() {{ printf '%s\\n' 'CALL:${{fn}}'; }}"
done
INSTALL_MODE={shlex.quote(mode)}
run_install
printf 'FRESH=%s\\n' "$FRESH_INSTALL"
'''
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_plugins_flow_skips_all_configuration_functions(self):
        output = self.install_flow_calls('plugins')

        for name in (
            'patch_moonraker_lane_data_conf',
            'install_tool_calibration_python',
            'copy_config',
            'install_tool_calibration_config',
            'ask_toolchange_scheme',
            'generate_multihotend_config',
            'patch_fresh_install_tool_count_configs',
            'patch_printer_cfg',
            'print_configure_completion',
        ):
            self.assertNotIn(f'CALL:{name}', output)
        self.assertIn('CALL:link_extension', output)
        self.assertIn('CALL:link_moonraker_components', output)
        self.assertIn('CALL:print_plugins_completion', output)
        self.assertIn('FRESH=0', output)

    def test_configure_flow_runs_complete_configuration(self):
        output = self.install_flow_calls('configure')

        for name in (
            'ask_tool_calibration_scheme',
            'patch_moonraker_lane_data_conf',
            'install_tool_calibration_python',
            'copy_config',
            'install_tool_calibration_config',
            'ask_toolchange_scheme',
            'generate_multihotend_config',
            'patch_fresh_install_tool_count_configs',
            'patch_printer_cfg',
            'print_configure_completion',
        ):
            self.assertIn(f'CALL:{name}', output)
        self.assertNotIn('CALL:print_plugins_completion', output)
        self.assertIn('FRESH=1', output)

    def test_frontend_installer_receives_moonraker_skip_by_mode(self):
        body = f'''
bash() {{
    printf 'STACK_ENV=%s:%s\\n' "$SKIP_PLUGIN_INSTALL" "$SKIP_MOONRAKER_CONFIG"
}}
INSTALL_PATH={shlex.quote(str(REPO_ROOT))}
FRONTEND_CHOICE=1
INSTALL_MODE=plugins
install_frontend_if_requested
INSTALL_MODE=configure
install_frontend_if_requested
'''
        result = self.run_bash(body)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('STACK_ENV=1:1'), 1)
        self.assertEqual(result.stdout.count('STACK_ENV=1:0'), 1)


class ToolchangerStackScriptTest(unittest.TestCase):
    def run_bash(self, body):
        command = f'source {shlex.quote(str(STACK_SCRIPT))}\n{body}'
        return subprocess.run(
            ['bash', '-c', command],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_nested_plugin_install_explicitly_uses_plugins_mode(self):
        result = self.run_bash('''
bash() {
    printf 'NESTED_MODE=%s\\n' "$INSTALL_MODE"
}
SKIP_PLUGIN_INSTALL=0
run_plugin_installer
''')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NESTED_MODE=plugins', result.stdout)

    def test_moonraker_patch_is_skipped_when_requested(self):
        result = self.run_bash('''
preflight_checks() { :; }
check_existing_fluidd() { :; }
run_plugin_installer() { :; }
install_or_update_fluidd_toolchanger() { :; }
patch_moonraker_conf() { printf 'CALL:patch_moonraker_conf\\n'; }
SKIP_MOONRAKER_CONFIG=1
main
''')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('CALL:patch_moonraker_conf', result.stdout)
        self.assertIn('不修改 moonraker.conf', result.stdout)


if __name__ == '__main__':
    unittest.main()
