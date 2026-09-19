#!/bin/bash
# Klipper multitool-stats 安装/更新脚本
# 用法 (远程):
#   wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
# 用法 (远程 + GitHub HTTP 下载代理):
#   GH_PROXY=https://v6.gh-proxy.org/ wget -O - https://v6.gh-proxy.org/https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | GH_PROXY=https://v6.gh-proxy.org/ bash
# 用法 (本地):
#   bash ~/klipper-toolchange-stats/install.sh
# 用法 (非交互，仅安装插件及可选网页):
#   INSTALL_MODE=plugins bash ~/klipper-toolchange-stats/install.sh
# 用法 (非交互，自动完成换热端配置):
#   INSTALL_MODE=configure bash ~/klipper-toolchange-stats/install.sh

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
MOONRAKER_PATH="${MOONRAKER_PATH:-${HOME}/moonraker}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
REPO_URL="${REPO_URL:-https://github.com/null01024/klipper-toolchange-stats.git}"
GH_PROXY="${GH_PROXY:-}"
INSTALL_MODE="${INSTALL_MODE:-}"
TOOLCHANGE_SCHEME="custom"
TOOL_CALIBRATION_SCHEME="none"
TOOL_HARDWARE_MODE=""
DOCK_FAN_MODE=""
MULTIHOTEND_BOARD=""
MULTIHOTEND_TOOL_COUNT=""
PROFILE_ROOT="${INSTALL_PATH}/profiles"
BOARD_PROFILE_DIR="${PROFILE_ROOT}/boards"
TOOLCHANGE_PROFILE_DIR="${PROFILE_ROOT}/toolchange"
BOARD_PROFILE_FILES=()
BOARD_PROFILE_NAMES=()
BOARD_PROFILE_MAX_TOOLS=()
TOOLCHANGE_PROFILE_FILES=()
TOOLCHANGE_PROFILE_NAMES=()
TOOLCHANGE_PROFILE_DESCRIPTIONS=()
DISCOVERED_PROFILE_FILES=()
PROFILE_GENERATE_NOTES=()
PROFILE_COMPLETION_NOTES=()
MULTIHOTEND_BOARD_PROFILE=""
MULTIHOTEND_BOARD_NAME=""
MULTIHOTEND_BOARD_MAX_TOOLS=""
MULTIHOTEND_BOARD_CONFIG_NOTE=""
MULTIHOTEND_BOARD_BODY_START=""
MULTIHOTEND_BOARD_GENERATE_NOTES=()
MULTIHOTEND_BOARD_COMPLETION_NOTES=()
TOOLCHANGE_PROFILE=""
TOOLCHANGE_NAME="自定义"
TOOLCHANGE_DESCRIPTION="自定义换头/换热端移动路径。"
TOOLCHANGE_PROFILE_HARDWARE_MODE="prompt"
TOOLCHANGE_RELEASE_MACRO=""
TOOLCHANGE_PICKUP_MACRO=""
TOOLCHANGE_BODY_START=""
TOOLCHANGE_COMPLETION_NOTES=()
FRONTEND_CHOICE=0
FRONTEND_NAME=""
FRONTEND_SOURCE_PATH=""
FRONTEND_TARGET_PATH=""
MOONRAKER_COMPONENT_INSTALLED=0
MOONRAKER_CONF_CHANGED=0
TOOLS_CALIBRATE_URL="${TOOLS_CALIBRATE_URL:-https://raw.githubusercontent.com/viesturz/klipper-toolchanger/main/klipper/extras/tools_calibrate.py}"
TOOL_EDDY_CALIBRATION_URL="${TOOL_EDDY_CALIBRATION_URL:-https://raw.githubusercontent.com/chengxg/tool_eddy_calibration/master/tool_eddy_calibration.py}"
CALIBRATION_EDDY_CFG_URL="${CALIBRATION_EDDY_CFG_URL:-https://raw.githubusercontent.com/chengxg/tool_eddy_calibration/master/config/calibration-eddy.cfg}"

# 配置在 printer.cfg 中的 include 行（写在文件最顶部）
INCLUDE_LINE="[include multitool/*.cfg]"
CONFIG_SUBDIR="multitool"
# 需要部署到用户配置目录的 cfg 列表（空格分隔，已存在则不覆盖）
CONFIG_FILES="multitool_config.cfg"
DEPLOYED_CONFIG_FILES="${CONFIG_FILES}"
CONFIG_WORK_PATH=""
CONFIG_STAGING_PATH=""
CONFIG_BACKUP_PATH=""

set -eu
export LC_ALL=C

RED="\033[0;31m"
RESET="\033[0m"

function cleanup_config_staging {
    local staging="${CONFIG_STAGING_PATH:-}"

    if [ -z "${staging}" ]; then
        return 0
    fi
    case "${staging}" in
        "${CONFIG_PATH}/.${CONFIG_SUBDIR}.install."*)
            rm -rf -- "${staging}"
            ;;
    esac
    CONFIG_STAGING_PATH=""
    CONFIG_WORK_PATH=""
}

function die {
    printf "${RED}[ERROR] %s${RESET}\n" "$*" >&2
    cleanup_config_staging
    exit 1
}

function config_work_path {
    if [ -n "${CONFIG_WORK_PATH}" ]; then
        printf "%s\n" "${CONFIG_WORK_PATH}"
    else
        printf "%s\n" "${CONFIG_PATH}/${CONFIG_SUBDIR}"
    fi
}

function require_command {
    local cmd="${1}"
    local hint="${2:-}"
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        if [ -n "${hint}" ]; then
            die "未找到命令 ${cmd}。${hint}"
        fi
        die "未找到命令 ${cmd}，请先安装后重新运行。"
    fi
}

function read_answer {
    local __var="${1}"
    local __answer=""
    if [ -r /dev/tty ]; then
        if { read -r __answer < /dev/tty; } 2>/dev/null; then
            printf -v "${__var}" "%s" "${__answer}"
            return
        fi
    fi
    if ! read -r __answer; then
        __answer=""
    fi
    printf -v "${__var}" "%s" "${__answer}"
}

function read_required_answer {
    local __var="${1}"
    local __answer=""
    if [ -r /dev/tty ]; then
        if { read -r __answer < /dev/tty; } 2>/dev/null; then
            printf -v "${__var}" "%s" "${__answer}"
            return 0
        fi
    fi
    if read -r __answer; then
        printf -v "${__var}" "%s" "${__answer}"
        return 0
    fi
    return 1
}

function ask_install_mode {
    local answer

    case "${INSTALL_MODE}" in
        plugins)
            echo "[MODE] 仅安装/更新插件及可选网页。"
            echo
            return
            ;;
        configure)
            echo "[MODE] 自动完成换热端配置。"
            echo
            return
            ;;
        "") ;;
        *) die "未知 INSTALL_MODE: ${INSTALL_MODE}。可用值为 plugins 或 configure。" ;;
    esac

    while true; do
        cat <<EOF
请选择安装模式：
  1. 仅安装/更新插件及可选网页（不修改 Klipper/Moonraker 配置）
  2. 自动完成换热端配置
EOF
        printf "请输入 1 或 2（必须选择）: "
        if ! read_required_answer answer; then
            die "无法读取安装模式。非交互运行时请设置 INSTALL_MODE=plugins 或 INSTALL_MODE=configure。"
        fi
        case "${answer}" in
            1)
                INSTALL_MODE="plugins"
                echo
                return
                ;;
            2)
                INSTALL_MODE="configure"
                echo
                return
                ;;
            *) echo "请输入 1 或 2，不能留空。" ;;
        esac
    done
}

function prompt_int_default {
    local prompt="${1}"
    local default="${2}"
    local min="${3}"
    local max="${4}"
    local answer answer_number
    while true; do
        printf "%s" "${prompt}" >&2
        read_answer answer
        if [ -z "${answer}" ]; then
            answer="${default}"
        fi
        case "${answer}" in
            *[!0-9]*|"")
                echo "请输入 ${min}..${max} 之间的数字。" >&2
                ;;
            *)
                if [ "${#answer}" -gt 6 ]; then
                    echo "请输入 ${min}..${max} 之间的数字。" >&2
                    continue
                fi
                answer_number=$((10#${answer}))
                if [ "${answer_number}" -ge "${min}" ] && [ "${answer_number}" -le "${max}" ]; then
                    printf "%s\n" "${answer_number}"
                    return
                fi
                echo "请输入 ${min}..${max} 之间的数字。" >&2
                ;;
        esac
    done
}

function profile_error {
    local file="${1}"
    shift
    die "无效 profile (${file}): $*"
}

function reset_profile_values {
    PROFILE_FORMAT_VERSION=""
    PROFILE_NAME=""
    PROFILE_MAX_TOOLS=""
    PROFILE_CONFIG_NOTE=""
    PROFILE_DESCRIPTION=""
    PROFILE_HARDWARE_MODE=""
    PROFILE_RELEASE_MACRO=""
    PROFILE_PICKUP_MACRO=""
    PROFILE_BODY_START=0
    PROFILE_BODY_LINE_COUNT=0
    PROFILE_GENERATE_NOTES=()
    PROFILE_COMPLETION_NOTES=()
}

function validate_profile_text_file {
    local file="${1}"
    local last_byte

    [ -f "${file}" ] || profile_error "${file}" "不是普通文件。"
    [ ! -L "${file}" ] || profile_error "${file}" "不允许使用符号链接。"
    [ -r "${file}" ] || profile_error "${file}" "文件不可读。"
    [ -s "${file}" ] || profile_error "${file}" "文件为空。"

    if LC_ALL=C grep -q '[[:cntrl:]]' "${file}"; then
        profile_error "${file}" "只允许 LF 换行，且不能包含制表符或其它控制字符。"
    fi
    last_byte="$(tail -c 1 -- "${file}")"
    [ -z "${last_byte}" ] || profile_error "${file}" "文件末尾必须包含 LF 换行。"
}

function validate_profile_metadata_value {
    local file="${1}"
    local key="${2}"
    local value="${3}"

    [ -n "${value}" ] || profile_error "${file}" "字段 ${key} 不能为空。"
    case "${value}" in
        " "*|*" ") profile_error "${file}" "字段 ${key} 不能包含首尾空格。" ;;
    esac
}

function validate_board_profile_body {
    local file="${1}"
    local body_start="${2}"
    local max_tools="${3}"
    local validation_error

    if validation_error="$(awk -v start="${body_start}" -v max_tools="${max_tools}" '
        function fail(message) {
            print message
            failed = 1
            exit 1
        }
        function trim(value) {
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            return value
        }
        NR < start { next }
        {
            line = $0
            if (line ~ /^[[:space:]]*$/ || line ~ /^[[:space:]]*#/) {
                next
            }
            if (line ~ /^[[:space:]]*aliases:[[:space:]]*(#.*)?$/) {
                if (aliases_seen) {
                    fail("正文只能包含一个 aliases: 声明。")
                }
                aliases_seen = 1
                next
            }
            if (!aliases_seen) {
                fail("正文第一个有效行必须是 aliases:。")
            }

            sub(/[[:space:]]*#.*/, "", line)
            count = split(line, entries, ",")
            for (i = 1; i <= count; i++) {
                entry = trim(entries[i])
                if (entry == "") {
                    continue
                }
                equal_at = index(entry, "=")
                if (equal_at <= 1) {
                    fail("aliases 条目必须使用 NAME=PIN 格式: " entry)
                }
                alias_name = trim(substr(entry, 1, equal_at - 1))
                if (alias_name !~ /^[A-Za-z_][A-Za-z0-9_]*$/) {
                    fail("非法 alias 名称: " alias_name)
                }
                if (alias_name in seen) {
                    fail("重复 alias: " alias_name)
                }
                seen[alias_name] = 1
            }
        }
        END {
            if (failed) {
                exit 1
            }
            if (!aliases_seen) {
                fail("正文缺少 aliases: 声明。")
            }
            for (i = 0; i < max_tools; i++) {
                heater = "T" i "H"
                sensor = "T" i "S"
                if (!(heater in seen)) {
                    fail("缺少必需 alias: " heater)
                }
                if (!(sensor in seen)) {
                    fail("缺少必需 alias: " sensor)
                }
            }
            for (alias_name in seen) {
                if (alias_name ~ /^T[0-9]+[HS]$/) {
                    tool = alias_name
                    sub(/^T/, "", tool)
                    sub(/[HS]$/, "", tool)
                    suffix = substr(alias_name, length(alias_name), 1)
                    canonical = "T" (tool + 0) suffix
                    if (alias_name != canonical) {
                        fail("工具 alias 必须使用规范编号: " alias_name)
                    }
                    if ((tool + 0) >= max_tools) {
                        fail("alias 超出 max_tools 范围: " alias_name)
                    }
                }
            }
        }
    ' "${file}")"; then
        return
    fi
    profile_error "${file}" "${validation_error:-aliases 正文校验失败。}"
}

function validate_macro_name {
    local file="${1}"
    local field="${2}"
    local value="${3}"
    local normalized="${3,,}"

    case "${value}" in
        ""|[0-9]*|*[!A-Za-z0-9_]*)
            profile_error "${file}" "字段 ${field} 必须是合法的 G-Code 宏名称。"
            ;;
    esac
    case "${normalized}" in
        multitool_release_tool|multitool_pickup_tool)
            profile_error "${file}" "字段 ${field} 不能使用公共钩子宏名称 ${value}。"
            ;;
    esac
}

function validate_toolchange_profile_body {
    local file="${1}"
    local body_start="${2}"
    local release_macro="${3}"
    local pickup_macro="${4}"
    local validation_error

    if validation_error="$(awk \
            -v start="${body_start}" \
            -v release_macro="${release_macro}" \
            -v pickup_macro="${pickup_macro}" '
        function fail(message) {
            print message
            failed = 1
            exit 1
        }
        function trim(value) {
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            return value
        }
        NR < start { next }
        {
            line = $0
            stripped = trim(line)

            if (index(stripped, "# BEGIN MULTITOOL_TOOL_TEMPLATE") > 0 &&
                    stripped != "# BEGIN MULTITOOL_TOOL_TEMPLATE") {
                fail("工具重复块开始标记必须独占一行。")
            }
            if (index(stripped, "# END MULTITOOL_TOOL_TEMPLATE") > 0 &&
                    stripped != "# END MULTITOOL_TOOL_TEMPLATE") {
                fail("工具重复块结束标记必须独占一行。")
            }
            if (stripped == "# BEGIN MULTITOOL_TOOL_TEMPLATE") {
                if (repeat_started) {
                    fail("工具重复块只能出现一次。")
                }
                repeat_started = 1
                in_repeat = 1
                next
            }
            if (stripped == "# END MULTITOOL_TOOL_TEMPLATE") {
                if (!in_repeat || repeat_ended) {
                    fail("工具重复块结束标记没有匹配的开始标记。")
                }
                repeat_ended = 1
                in_repeat = 0
                next
            }
            if (index(line, "@@TOOL@@") > 0) {
                if (!in_repeat) {
                    fail("@@TOOL@@ 只能出现在工具重复块内。")
                }
                repeat_has_token = 1
            }

            if (stripped == "[gcode_macro " release_macro "]") {
                if (in_repeat) {
                    fail("release_macro 不能定义在工具重复块内。")
                }
                release_count++
            }
            if (stripped == "[gcode_macro " pickup_macro "]") {
                if (in_repeat) {
                    fail("pickup_macro 不能定义在工具重复块内。")
                }
                pickup_count++
            }
            lowered = tolower(stripped)
            if (lowered == "[gcode_macro multitool_release_tool]" ||
                    lowered == "[gcode_macro multitool_pickup_tool]") {
                fail("方案正文不能重新定义公共换头钩子。")
            }
        }
        END {
            if (failed) {
                exit 1
            }
            if (in_repeat || repeat_started != repeat_ended) {
                fail("工具重复块标记不完整。")
            }
            if (repeat_started && !repeat_has_token) {
                fail("工具重复块中缺少 @@TOOL@@。")
            }
            if (release_count != 1) {
                fail("正文必须恰好定义一次 [gcode_macro " release_macro "]。")
            }
            if (pickup_count != 1) {
                fail("正文必须恰好定义一次 [gcode_macro " pickup_macro "]。")
            }
        }
    ' "${file}")"; then
        return
    fi
    profile_error "${file}" "${validation_error:-换头方案正文校验失败。}"
}

function parse_profile {
    local profile_type="${1}"
    local file="${2}"
    local line key value
    local line_number=0
    local in_body=0

    reset_profile_values
    validate_profile_text_file "${file}"

    while IFS= read -r line || [ -n "${line}" ]; do
        line_number=$((line_number + 1))
        if [ "${in_body}" -eq 1 ]; then
            [ "${line}" != "---" ] || profile_error "${file}" "只能包含一个 --- 分隔行。"
            PROFILE_BODY_LINE_COUNT=$((PROFILE_BODY_LINE_COUNT + 1))
            continue
        fi

        case "${line}" in
            ""|\#*) continue ;;
            ---)
                in_body=1
                PROFILE_BODY_START=$((line_number + 1))
                continue
                ;;
            *=*) ;;
            *) profile_error "${file}" "第 ${line_number} 行必须是 key=value、注释或 ---。" ;;
        esac

        key="${line%%=*}"
        value="${line#*=}"
        case "${key}" in
            [a-z_]* ) ;;
            *) profile_error "${file}" "第 ${line_number} 行包含非法字段名: ${key}" ;;
        esac
        case "${key}" in
            *[!a-z0-9_]*) profile_error "${file}" "第 ${line_number} 行包含非法字段名: ${key}" ;;
        esac
        validate_profile_metadata_value "${file}" "${key}" "${value}"

        case "${key}" in
            format_version)
                [ -z "${PROFILE_FORMAT_VERSION}" ] || profile_error "${file}" "字段 format_version 重复。"
                PROFILE_FORMAT_VERSION="${value}"
                ;;
            name)
                [ -z "${PROFILE_NAME}" ] || profile_error "${file}" "字段 name 重复。"
                PROFILE_NAME="${value}"
                ;;
            completion_note)
                PROFILE_COMPLETION_NOTES+=("${value}")
                ;;
            max_tools)
                [ "${profile_type}" = "board" ] || profile_error "${file}" "换头方案不支持字段 max_tools。"
                [ -z "${PROFILE_MAX_TOOLS}" ] || profile_error "${file}" "字段 max_tools 重复。"
                PROFILE_MAX_TOOLS="${value}"
                ;;
            config_note)
                [ "${profile_type}" = "board" ] || profile_error "${file}" "换头方案不支持字段 config_note。"
                [ -z "${PROFILE_CONFIG_NOTE}" ] || profile_error "${file}" "字段 config_note 重复。"
                PROFILE_CONFIG_NOTE="${value}"
                ;;
            generate_note)
                [ "${profile_type}" = "board" ] || profile_error "${file}" "换头方案不支持字段 generate_note。"
                PROFILE_GENERATE_NOTES+=("${value}")
                ;;
            description)
                [ "${profile_type}" = "toolchange" ] || profile_error "${file}" "PCB profile 不支持字段 description。"
                [ -z "${PROFILE_DESCRIPTION}" ] || profile_error "${file}" "字段 description 重复。"
                PROFILE_DESCRIPTION="${value}"
                ;;
            hardware_mode)
                [ "${profile_type}" = "toolchange" ] || profile_error "${file}" "PCB profile 不支持字段 hardware_mode。"
                [ -z "${PROFILE_HARDWARE_MODE}" ] || profile_error "${file}" "字段 hardware_mode 重复。"
                PROFILE_HARDWARE_MODE="${value}"
                ;;
            release_macro)
                [ "${profile_type}" = "toolchange" ] || profile_error "${file}" "PCB profile 不支持字段 release_macro。"
                [ -z "${PROFILE_RELEASE_MACRO}" ] || profile_error "${file}" "字段 release_macro 重复。"
                PROFILE_RELEASE_MACRO="${value}"
                ;;
            pickup_macro)
                [ "${profile_type}" = "toolchange" ] || profile_error "${file}" "PCB profile 不支持字段 pickup_macro。"
                [ -z "${PROFILE_PICKUP_MACRO}" ] || profile_error "${file}" "字段 pickup_macro 重复。"
                PROFILE_PICKUP_MACRO="${value}"
                ;;
            *) profile_error "${file}" "未知字段: ${key}" ;;
        esac
    done < "${file}"

    [ "${in_body}" -eq 1 ] || profile_error "${file}" "缺少 --- 分隔行。"
    [ "${PROFILE_BODY_LINE_COUNT}" -gt 0 ] || profile_error "${file}" "--- 后缺少正文。"
    [ "${PROFILE_FORMAT_VERSION}" = "1" ] || profile_error "${file}" "format_version 必须为 1。"
    [ -n "${PROFILE_NAME}" ] || profile_error "${file}" "缺少字段 name。"

    case "${profile_type}" in
        board)
            case "${PROFILE_MAX_TOOLS}" in
                [1-9]|1[0-6]) ;;
                *) profile_error "${file}" "max_tools 必须是 1..16 的整数。" ;;
            esac
            [ -n "${PROFILE_CONFIG_NOTE}" ] || profile_error "${file}" "缺少字段 config_note。"
            validate_board_profile_body "${file}" "${PROFILE_BODY_START}" "${PROFILE_MAX_TOOLS}"
            ;;
        toolchange)
            [ -n "${PROFILE_DESCRIPTION}" ] || profile_error "${file}" "缺少字段 description。"
            case "${PROFILE_HARDWARE_MODE}" in
                prompt|shared_extruder|multi_toolhead) ;;
                *) profile_error "${file}" "hardware_mode 必须为 prompt、shared_extruder 或 multi_toolhead。" ;;
            esac
            validate_macro_name "${file}" "release_macro" "${PROFILE_RELEASE_MACRO}"
            validate_macro_name "${file}" "pickup_macro" "${PROFILE_PICKUP_MACRO}"
            [ "${PROFILE_RELEASE_MACRO,,}" != "${PROFILE_PICKUP_MACRO,,}" ] \
                || profile_error "${file}" "release_macro 与 pickup_macro 不能相同。"
            validate_toolchange_profile_body \
                "${file}" "${PROFILE_BODY_START}" \
                "${PROFILE_RELEASE_MACRO}" "${PROFILE_PICKUP_MACRO}"
            ;;
        *) die "未知 profile 类型: ${profile_type}" ;;
    esac
}

function find_profile_files {
    local directory="${1}"
    local label="${2}"
    local restore_nullglob=0
    local file base

    DISCOVERED_PROFILE_FILES=()
    [ -d "${directory}" ] || die "缺少 ${label} profile 目录: ${directory}"
    [ ! -L "${directory}" ] || die "${label} profile 目录不能是符号链接: ${directory}"
    [ -r "${directory}" ] || die "${label} profile 目录不可读: ${directory}"

    if shopt -q nullglob; then
        restore_nullglob=1
    else
        shopt -s nullglob
    fi
    DISCOVERED_PROFILE_FILES=("${directory}"/*.conf)
    if [ "${restore_nullglob}" -eq 0 ]; then
        shopt -u nullglob
    fi

    [ "${#DISCOVERED_PROFILE_FILES[@]}" -gt 0 ] || die "${label} profile 目录中没有 .conf 文件: ${directory}"
    for file in "${DISCOVERED_PROFILE_FILES[@]}"; do
        base="$(basename "${file}")"
        case "${base}" in
            *[!A-Za-z0-9._-]*) profile_error "${file}" "文件名只能包含字母、数字、点、下划线和连字符。" ;;
        esac
        [ -f "${file}" ] || profile_error "${file}" "不是普通文件。"
        [ ! -L "${file}" ] || profile_error "${file}" "不允许使用符号链接。"
        [ -r "${file}" ] || profile_error "${file}" "文件不可读。"
    done
}

function discover_board_profiles {
    local file existing_name

    BOARD_PROFILE_FILES=()
    BOARD_PROFILE_NAMES=()
    BOARD_PROFILE_MAX_TOOLS=()
    find_profile_files "${BOARD_PROFILE_DIR}" "PCB"
    for file in "${DISCOVERED_PROFILE_FILES[@]}"; do
        parse_profile board "${file}"
        for existing_name in "${BOARD_PROFILE_NAMES[@]}"; do
            [ "${existing_name}" != "${PROFILE_NAME}" ] \
                || profile_error "${file}" "PCB 名称重复: ${PROFILE_NAME}"
        done
        BOARD_PROFILE_FILES+=("${file}")
        BOARD_PROFILE_NAMES+=("${PROFILE_NAME}")
        BOARD_PROFILE_MAX_TOOLS+=("${PROFILE_MAX_TOOLS}")
    done
}

function discover_toolchange_profiles {
    local file existing_name

    TOOLCHANGE_PROFILE_FILES=()
    TOOLCHANGE_PROFILE_NAMES=()
    TOOLCHANGE_PROFILE_DESCRIPTIONS=()
    find_profile_files "${TOOLCHANGE_PROFILE_DIR}" "换头方案"
    for file in "${DISCOVERED_PROFILE_FILES[@]}"; do
        parse_profile toolchange "${file}"
        [ "${PROFILE_NAME}" != "自定义" ] || profile_error "${file}" "名称 自定义 由安装脚本保留。"
        for existing_name in "${TOOLCHANGE_PROFILE_NAMES[@]}"; do
            [ "${existing_name}" != "${PROFILE_NAME}" ] \
                || profile_error "${file}" "换头方案名称重复: ${PROFILE_NAME}"
        done
        TOOLCHANGE_PROFILE_FILES+=("${file}")
        TOOLCHANGE_PROFILE_NAMES+=("${PROFILE_NAME}")
        TOOLCHANGE_PROFILE_DESCRIPTIONS+=("${PROFILE_DESCRIPTION}")
    done
}

function prepare_profile_catalogs {
    if [ "${INSTALL_MODE}" != "configure" ]; then
        return
    fi
    discover_board_profiles
    discover_toolchange_profiles
}

function select_board_profile_file {
    local file="${1}"
    local base

    parse_profile board "${file}"
    base="$(basename "${file}")"
    MULTIHOTEND_BOARD="${base%.conf}"
    MULTIHOTEND_BOARD_PROFILE="${file}"
    MULTIHOTEND_BOARD_NAME="${PROFILE_NAME}"
    MULTIHOTEND_BOARD_MAX_TOOLS="${PROFILE_MAX_TOOLS}"
    MULTIHOTEND_BOARD_CONFIG_NOTE="${PROFILE_CONFIG_NOTE}"
    MULTIHOTEND_BOARD_BODY_START="${PROFILE_BODY_START}"
    MULTIHOTEND_BOARD_GENERATE_NOTES=("${PROFILE_GENERATE_NOTES[@]}")
    MULTIHOTEND_BOARD_COMPLETION_NOTES=("${PROFILE_COMPLETION_NOTES[@]}")
}

function select_custom_toolchange_scheme {
    TOOLCHANGE_SCHEME="custom"
    TOOLCHANGE_PROFILE=""
    TOOLCHANGE_NAME="自定义"
    TOOLCHANGE_DESCRIPTION="自定义换头/换热端移动路径。"
    TOOLCHANGE_PROFILE_HARDWARE_MODE="prompt"
    TOOLCHANGE_RELEASE_MACRO=""
    TOOLCHANGE_PICKUP_MACRO=""
    TOOLCHANGE_BODY_START=""
    TOOLCHANGE_COMPLETION_NOTES=()
}

function select_toolchange_profile_file {
    local file="${1}"
    local base

    parse_profile toolchange "${file}"
    base="$(basename "${file}")"
    TOOLCHANGE_SCHEME="${base%.conf}"
    TOOLCHANGE_PROFILE="${file}"
    TOOLCHANGE_NAME="${PROFILE_NAME}"
    TOOLCHANGE_DESCRIPTION="${PROFILE_DESCRIPTION}"
    TOOLCHANGE_PROFILE_HARDWARE_MODE="${PROFILE_HARDWARE_MODE}"
    TOOLCHANGE_RELEASE_MACRO="${PROFILE_RELEASE_MACRO}"
    TOOLCHANGE_PICKUP_MACRO="${PROFILE_PICKUP_MACRO}"
    TOOLCHANGE_BODY_START="${PROFILE_BODY_START}"
    TOOLCHANGE_COMPLETION_NOTES=("${PROFILE_COMPLETION_NOTES[@]}")
}

function emit_profile_body {
    local file="${1}"
    local body_start="${2}"
    tail -n "+${body_start}" -- "${file}"
}

function ask_frontend_choice {
    local answer
    while true; do
        cat <<EOF
请选择是否安装/更新配套前端：
  0. 不安装/更新前端
  1. Mainsail
请输入 0,1 [0]: 
EOF
        read_answer answer
        if [ -z "${answer}" ]; then
            answer=0
        fi
        case "${answer}" in
            0|1)
                FRONTEND_CHOICE="${answer}"
                break
                ;;
            *)
                echo "请输入 0..1 之间的数字。"
                ;;
        esac
    done
    echo
}

function resolve_frontend_paths {
    case "${FRONTEND_CHOICE}" in
        0)
            FRONTEND_NAME=""
            FRONTEND_SOURCE_PATH=""
            FRONTEND_TARGET_PATH=""
            ;;
        1)
            FRONTEND_NAME="Mainsail"
            FRONTEND_SOURCE_PATH="${INSTALL_PATH}/mainsail"
            FRONTEND_TARGET_PATH="${HOME}/mainsail"
            ;;
        *)
            die "未知前端选择: ${FRONTEND_CHOICE}"
            ;;
    esac
}

function validate_frontend_if_requested {
    local source_real target_real target_parent

    resolve_frontend_paths
    if [ "${FRONTEND_CHOICE}" -eq 0 ]; then
        return
    fi

    [ -d "${FRONTEND_SOURCE_PATH}" ] || die "本地 ${FRONTEND_NAME} 产物目录不存在: ${FRONTEND_SOURCE_PATH}"
    [ -r "${FRONTEND_SOURCE_PATH}" ] || die "本地 ${FRONTEND_NAME} 产物目录不可读: ${FRONTEND_SOURCE_PATH}"
    [ -f "${FRONTEND_SOURCE_PATH}/index.html" ] || die "本地 ${FRONTEND_NAME} 产物缺少 index.html: ${FRONTEND_SOURCE_PATH}"

    [ -d "${FRONTEND_TARGET_PATH}" ] || die "未检测到已安装的 ${FRONTEND_NAME}: ${FRONTEND_TARGET_PATH}。请先安装原版 ${FRONTEND_NAME}。"
    [ -f "${FRONTEND_TARGET_PATH}/index.html" ] || die "现有 ${FRONTEND_NAME} 目录缺少 index.html: ${FRONTEND_TARGET_PATH}"

    source_real="$(cd "${FRONTEND_SOURCE_PATH}" && pwd -P)"
    target_real="$(cd "${FRONTEND_TARGET_PATH}" && pwd -P)"
    [ "${source_real}" != "${target_real}" ] || die "本地产物目录不能与部署目录相同: ${source_real}"

    target_parent="$(dirname "${FRONTEND_TARGET_PATH}")"
    [ -w "${target_parent}" ] || die "当前用户无权写入前端父目录: ${target_parent}"
    echo "[PRE-CHECK] ${FRONTEND_NAME} 本地产物和现有安装均有效。"
}

function proxy_url {
    local url="${1}"
    local proxy="${GH_PROXY}"

    case "${url}" in
        http://*|https://*) ;;
        *) printf "%s\n" "${url}"; return ;;
    esac

    if [ -z "${proxy}" ]; then
        printf "%s\n" "${url}"
        return
    fi

    proxy="${proxy%/}"
    case "${url}" in
        "${proxy}/"*) printf "%s\n" "${url}" ;;
        *) printf "%s/%s\n" "${proxy}" "${url}" ;;
    esac
}

function download_url {
    local url="${1}"
    local dest="${2}"
    local proxied_url tmp_file dest_dir

    proxied_url="$(proxy_url "${url}")"
    dest_dir="$(dirname "${dest}")"
    mkdir -p "${dest_dir}" || die "无法创建下载目标目录: ${dest_dir}"
    [ -w "${dest_dir}" ] || die "当前用户无权写入下载目标目录: ${dest_dir}"
    tmp_file="$(mktemp "${dest}.tmp.XXXXXX")" || die "创建下载临时文件失败: ${dest}"

    if command -v curl >/dev/null 2>&1; then
        if ! curl -fsSL "${proxied_url}" -o "${tmp_file}"; then
            rm -f "${tmp_file}"
            die "下载失败: ${url}"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if ! wget -qO "${tmp_file}" "${proxied_url}"; then
            rm -f "${tmp_file}"
            die "下载失败: ${url}"
        fi
    else
        rm -f "${tmp_file}"
        die "未找到 curl 或 wget，无法下载: ${url}"
    fi

    mv "${tmp_file}" "${dest}" || die "写入下载文件失败: ${dest}"
}

function preflight_checks {
    if [ "$EUID" -eq 0 ]; then
        die "不要以 root 身份运行此脚本！请使用普通用户执行，脚本需要时会调用 sudo。"
    fi

    require_command git "请先安装 git，例如：sudo apt install git"
    require_command sudo "请先安装 sudo，或使用具备 sudo 权限的普通用户。"
    require_command systemctl "未检测到 systemctl，请确认这是 systemd 环境。"
    require_command readlink

    if sudo systemctl list-units --full -all -t service --no-legend 2>/dev/null | grep -qF 'klipper.service'; then
        printf "[PRE-CHECK] 已检测到 Klipper 服务，继续...\n\n"
    else
        die "未找到 klipper.service，请先安装 Klipper，或确认服务名是否为 klipper.service。"
    fi
    if [ ! -d "${KLIPPER_PATH}/klippy/extras" ]; then
        die "未找到 Klipper 源码目录: ${KLIPPER_PATH}。如果路径不同，请用 KLIPPER_PATH=... 覆盖。"
    fi
    if [ "${INSTALL_MODE}" = "configure" ]; then
        if [ ! -d "${CONFIG_PATH}" ]; then
            die "未找到 Klipper 配置目录: ${CONFIG_PATH}。如果你的配置目录不在默认位置，请用 CONFIG_PATH=... 覆盖。"
        fi
        [ -w "${CONFIG_PATH}" ] || die "当前用户无权写入 Klipper 配置目录: ${CONFIG_PATH}"
    fi
}

function sync_repo {
    local installdirname installbasename proxied_repo_url clone_attempt
    installdirname="$(dirname "${INSTALL_PATH}")"
    installbasename="$(basename "${INSTALL_PATH}")"
    proxied_repo_url="$(proxy_url "${REPO_URL}")"
    if [ ! -d "${installdirname}" ]; then
        mkdir -p "${installdirname}" || die "无法创建安装父目录: ${installdirname}"
    fi
    [ -w "${installdirname}" ] || die "当前用户无权写入安装父目录: ${installdirname}"

    if [ ! -d "${INSTALL_PATH}" ]; then
        echo "[DOWNLOAD] 正在克隆仓库..."
        if [ -n "${GH_PROXY}" ] && [ "${proxied_repo_url}" != "${REPO_URL}" ]; then
            echo "           via ${GH_PROXY}"
        fi
        for clone_attempt in 1 2; do
            if git -C "${installdirname}" clone "${proxied_repo_url}" "${installbasename}"; then
                chmod +x "${INSTALL_PATH}/install.sh" || die "无法设置 install.sh 可执行权限: ${INSTALL_PATH}/install.sh"
                printf "[DOWNLOAD] 克隆完成！\n\n"
                break
            fi
            if [ "${clone_attempt}" -eq 2 ]; then
                die "克隆 git 仓库失败: ${REPO_URL}"
            fi
            echo "[DOWNLOAD] 克隆失败，删除旧的克隆目录后重试一次..."
            rm -rf "${INSTALL_PATH}" || die "删除旧的克隆目录失败: ${INSTALL_PATH}"
        done
        return
    fi

    if [ ! -d "${INSTALL_PATH}/.git" ]; then
        die "${INSTALL_PATH} 已存在，但不是 Git 仓库，无法自动更新。请手动检查该目录，或更换 INSTALL_PATH 后重新运行。"
    fi

    local current_branch status_output remote_url fetch_remote
    if ! current_branch="$(git -C "${INSTALL_PATH}" branch --show-current)"; then
        die "读取当前 Git 分支失败: ${INSTALL_PATH}"
    fi
    if [ -z "${current_branch}" ]; then
        die "${INSTALL_PATH} 当前处于 detached HEAD，无法安全自动更新。请切回普通分支后重新运行，例如：git -C ${INSTALL_PATH} switch main"
    fi

    if ! status_output="$(git -C "${INSTALL_PATH}" status --porcelain)"; then
        die "读取 Git 工作区状态失败: ${INSTALL_PATH}"
    fi
    if [ -n "${status_output}" ]; then
        die "${INSTALL_PATH} 存在未提交修改，已中止自动更新。请先提交、stash 或清理本地改动后重新运行。"
    fi

    echo "[UPDATE] 本地已存在仓库，正在更新当前分支 ${current_branch}..."
    remote_url="$(git -C "${INSTALL_PATH}" config --get remote.origin.url || true)"
    fetch_remote="${remote_url:-origin}"
    if [ -n "${remote_url}" ]; then
        fetch_remote="$(proxy_url "${remote_url}")"
    fi
    if [ -n "${GH_PROXY}" ] && [ "${fetch_remote}" != "${remote_url:-origin}" ]; then
        echo "         via ${GH_PROXY}"
    fi
    if ! git -C "${INSTALL_PATH}" fetch "${fetch_remote}" "${current_branch}:refs/remotes/origin/${current_branch}"; then
        die "拉取远端分支 origin/${current_branch} 失败。请检查网络、代理或远端分支是否存在。"
    fi

    if ! git -C "${INSTALL_PATH}" merge --ff-only "origin/${current_branch}"; then
        die "当前分支 ${current_branch} 无法 fast-forward 到 origin/${current_branch}。本地分支可能已与远端分叉，请手动 merge/rebase 后重新运行。"
    fi

    chmod +x "${INSTALL_PATH}/install.sh" || die "无法设置 install.sh 可执行权限: ${INSTALL_PATH}/install.sh"
    printf "[UPDATE] 更新完成！\n\n"
}

function link_extension {
    echo "[INSTALL] 链接扩展到 Klipper..."
    local files file
    files=("${INSTALL_PATH}"/klipper/extras/*.py)
    [ -e "${files[0]}" ] || die "未找到可安装的 Klipper extras 文件: ${INSTALL_PATH}/klipper/extras/*.py"
    [ -w "${KLIPPER_PATH}/klippy/extras" ] || die "当前用户无权写入 Klipper extras 目录: ${KLIPPER_PATH}/klippy/extras"

    for file in "${files[@]}"; do
        local base target
        base="$(basename "${file}")"
        case "${base}" in
            tools_calibrate.py|tool_eddy_calibration.py|closed_loop_motor*.py)
                continue
                ;;
        esac
        target="${KLIPPER_PATH}/klippy/extras/${base}"

        # 如果目标已存在且不是本仓库的软链，直接覆盖为本仓库链接。
        if [ -e "${target}" ] || [ -L "${target}" ]; then
            local resolved
            resolved="$(readlink "${target}" 2>/dev/null || true)"
            if [ "${resolved}" != "${file}" ]; then
                echo "  -> [WARN] ${base} 已存在 (${resolved:-非软链})，将覆盖为本仓库链接"
            fi
        fi

        ln -sfnT "${file}" "${target}" || die "创建 Klipper extras 软链接失败: ${target} -> ${file}"
        echo "  -> ${base}"
    done
}

function link_moonraker_components {
    local src_dir="${INSTALL_PATH}/moonraker/components"
    local dst_dir="${MOONRAKER_PATH}/moonraker/components"

    if [ ! -d "${src_dir}" ]; then
        return
    fi
    if [ ! -d "${dst_dir}" ]; then
        echo "[INSTALL] 未找到 Moonraker components 目录，跳过 Orca lane_data 组件。"
        echo "          如 Moonraker 不在默认路径，请用 MOONRAKER_PATH=... 重新运行。"
        return
    fi
    [ -w "${dst_dir}" ] || die "当前用户无权写入 Moonraker components 目录: ${dst_dir}"

    echo "[INSTALL] 链接 Moonraker 组件..."
    local files file base target resolved
    files=("${src_dir}"/*.py)
    [ -e "${files[0]}" ] || return
    for file in "${files[@]}"; do
        base="$(basename "${file}")"
        target="${dst_dir}/${base}"
        if [ -e "${target}" ] || [ -L "${target}" ]; then
            resolved="$(readlink "${target}" 2>/dev/null || true)"
            if [ "${resolved}" != "${file}" ]; then
                echo "  -> [WARN] ${base} 已存在 (${resolved:-非软链})，将覆盖为本仓库链接"
            fi
        fi
        ln -sfnT "${file}" "${target}" || die "创建 Moonraker 组件软链接失败: ${target} -> ${file}"
        echo "  -> ${base}"
        MOONRAKER_COMPONENT_INSTALLED=1
    done
}

function discover_moonraker_conf {
    if [ -n "${MOONRAKER_CONF:-}" ]; then
        [ -f "${MOONRAKER_CONF}" ] && printf "%s\n" "${MOONRAKER_CONF}"
        return
    fi

    local candidate
    for candidate in \
        "${CONFIG_PATH}/moonraker.conf" \
        "${HOME}/printer_data/config/moonraker.conf" \
        "${HOME}/moonraker.conf"
    do
        if [ -f "${candidate}" ]; then
            printf "%s\n" "${candidate}"
            return
        fi
    done
}

function patch_moonraker_lane_data_conf {
    local conf

    if [ "${MOONRAKER_COMPONENT_INSTALLED}" -ne 1 ]; then
        return
    fi
    conf="$(discover_moonraker_conf || true)"
    if [ -z "${conf}" ]; then
        echo "[MOONRAKER] 未找到 moonraker.conf，跳过自动启用 multitool_lane_data。"
        return
    fi
    [ -r "${conf}" ] || die "当前用户无权读取 moonraker.conf: ${conf}"
    [ -w "${conf}" ] || die "当前用户无权写入 moonraker.conf: ${conf}"
    if grep -q '^[[:space:]]*\[multitool_lane_data\][[:space:]]*$' "${conf}"; then
        echo "[MOONRAKER] multitool_lane_data 已启用。"
        return
    fi
    {
        printf "\n"
        printf "[multitool_lane_data]\n"
    } >> "${conf}" || die "写入 moonraker.conf 失败: ${conf}"
    MOONRAKER_CONF_CHANGED=1
    echo "[MOONRAKER] 已启用 multitool_lane_data: ${conf}"
}

function install_tool_calibration_python {
    local target
    [ -w "${KLIPPER_PATH}/klippy/extras" ] || die "当前用户无权写入 Klipper extras 目录: ${KLIPPER_PATH}/klippy/extras"

    case "${TOOL_CALIBRATION_SCHEME}" in
        none)
            echo "[INSTALL] 对刀方案：无对刀，跳过对刀 Python 插件。"
            ;;
        touch)
            target="${KLIPPER_PATH}/klippy/extras/tools_calibrate.py"
            echo "[INSTALL] 下载微动对刀插件 tools_calibrate.py..."
            download_url "${TOOLS_CALIBRATE_URL}" "${target}"
            echo "  -> tools_calibrate.py"
            ;;
        eddy)
            target="${KLIPPER_PATH}/klippy/extras/tool_eddy_calibration.py"
            echo "[INSTALL] 下载涡流对刀插件 tool_eddy_calibration.py..."
            download_url "${TOOL_EDDY_CALIBRATION_URL}" "${target}"
            echo "  -> tool_eddy_calibration.py"
            ;;
        *)
            die "未知对刀方案: ${TOOL_CALIBRATION_SCHEME}"
            ;;
    esac
}

function clean_orphan_links {
    # 清理本仓库遗留的孤儿软链：
    #   指向本仓库 extras 目录、但源文件已被删除（如旧版 multitool_stats.py）。
    # 仅删除断链且 readlink 落在本仓库 extras 目录内的软链，
    # 不碰用户自有插件或其它来源的文件。
    local repo_extras extras_dir resolved
    repo_extras="${INSTALL_PATH}/klipper/extras"
    extras_dir="${KLIPPER_PATH}/klippy/extras"

    for target in "${extras_dir}"/*.py; do
        # glob 无匹配时 *.py 字面量本身不是软链，跳过
        [ -L "${target}" ] || continue
        resolved="$(readlink "${target}" 2>/dev/null || true)"
        case "${resolved}" in
            "${repo_extras}"/*)
                if [ ! -e "${target}" ]; then
                    rm -f "${target}" || die "移除孤儿软链接失败: ${target}"
                    echo "  -> [CLEAN] 移除孤儿软链 $(basename "${target}") (源已删除: ${resolved})"
                fi
                ;;
        esac
    done
}

function prepare_config_staging {
    [ -d "${CONFIG_PATH}" ] || die "未找到 Klipper 配置目录: ${CONFIG_PATH}"
    [ -w "${CONFIG_PATH}" ] || die "当前用户无权写入 Klipper 配置目录: ${CONFIG_PATH}"

    CONFIG_STAGING_PATH="$(mktemp -d "${CONFIG_PATH}/.${CONFIG_SUBDIR}.install.XXXXXX")" \
        || die "创建配置暂存目录失败: ${CONFIG_PATH}"
    CONFIG_WORK_PATH="${CONFIG_STAGING_PATH}"
    trap cleanup_config_staging EXIT
    echo "[CONFIG] 在暂存目录生成全新配置: ${CONFIG_STAGING_PATH}"
}

function activate_staged_config {
    local target="${CONFIG_PATH}/${CONFIG_SUBDIR}"
    local staging="${CONFIG_STAGING_PATH}"
    local backup_root=""
    local backup_path=""

    [ -n "${staging}" ] || die "配置暂存目录尚未创建。"
    [ -d "${staging}" ] || die "配置暂存目录不存在: ${staging}"
    [ -f "${staging}/multitool_config.cfg" ] || die "暂存配置缺少 multitool_config.cfg。"
    [ -f "${staging}/multihotend.cfg" ] || die "暂存配置缺少 multihotend.cfg。"
    if [ -n "${TOOLCHANGE_PROFILE}" ]; then
        [ -f "${staging}/change_tool.cfg" ] || die "暂存配置缺少 change_tool.cfg。"
    fi

    if [ -e "${target}" ] || [ -L "${target}" ]; then
        backup_root="$(mktemp -d "${CONFIG_PATH}/${CONFIG_SUBDIR}.backup.XXXXXX")" \
            || die "创建配置备份目录失败: ${CONFIG_PATH}"
        backup_path="${backup_root}/${CONFIG_SUBDIR}"
        if ! mv "${target}" "${backup_path}"; then
            rmdir "${backup_root}" 2>/dev/null || true
            die "备份现有配置目录失败: ${target}"
        fi
    fi

    if ! mv "${staging}" "${target}"; then
        if [ -n "${backup_path}" ]; then
            if mv "${backup_path}" "${target}"; then
                rmdir "${backup_root}" 2>/dev/null || true
                die "激活新配置失败，已恢复原配置目录。"
            fi
            CONFIG_STAGING_PATH=""
            CONFIG_WORK_PATH=""
            die "激活新配置失败，且无法恢复原配置。旧配置位于 ${backup_path}，新配置位于 ${staging}。"
        fi
        die "激活新配置失败: ${target}"
    fi

    CONFIG_STAGING_PATH=""
    CONFIG_WORK_PATH=""
    CONFIG_BACKUP_PATH="${backup_path}"
    if [ -n "${CONFIG_BACKUP_PATH}" ]; then
        echo "[CONFIG] 原配置已备份到: ${CONFIG_BACKUP_PATH}"
    fi
    echo "[CONFIG] 已启用全新配置目录: ${target}"
}


function copy_config {
    local target_dir
    target_dir="$(config_work_path)"

    echo "[CONFIG] 部署默认配置到 ${target_dir}/"
    mkdir -p "${target_dir}" || die "无法创建配置目录: ${target_dir}"
    [ -w "${target_dir}" ] || die "当前用户无权写入配置目录: ${target_dir}"

    local cfg target_file source_file
    for cfg in ${CONFIG_FILES}; do
        target_file="${target_dir}/${cfg}"
        source_file="${INSTALL_PATH}/${cfg}"
        [ -f "${source_file}" ] || die "缺少默认配置文件: ${source_file}"
        if [ -f "${target_file}" ]; then
            echo "  -> 已存在 ${cfg}，跳过覆盖（保留用户修改）"
        else
            cp "${source_file}" "${target_file}" || die "复制配置文件失败: ${source_file} -> ${target_file}"
            echo "  -> 已复制 ${cfg}"
        fi
    done
}

function install_tool_calibration_config {
    local target_dir
    local target_file

    target_dir="$(config_work_path)"

    case "${TOOL_CALIBRATION_SCHEME}" in
        none)
            echo "[CONFIG] 对刀方案：无对刀，跳过对刀配置。"
            ;;
        touch)
            target_file="${target_dir}/calibration.cfg"
            if [ -f "${target_file}" ]; then
                echo "  -> 已存在 calibration.cfg，跳过覆盖（保留用户修改）"
            else
                [ -f "${INSTALL_PATH}/calibration.cfg" ] || die "缺少默认配置文件: ${INSTALL_PATH}/calibration.cfg"
                cp "${INSTALL_PATH}/calibration.cfg" "${target_file}" || die "复制 calibration.cfg 失败。"
                echo "  -> 已复制 calibration.cfg"
            fi
            ;;
        eddy)
            target_file="${target_dir}/calibration-eddy.cfg"
            if [ -f "${target_file}" ]; then
                echo "  -> 已存在 calibration-eddy.cfg，跳过覆盖（保留用户修改）"
            else
                echo "[CONFIG] 下载涡流对刀配置 calibration-eddy.cfg..."
                download_url "${CALIBRATION_EDDY_CFG_URL}" "${target_file}"
                echo "  -> 已下载 calibration-eddy.cfg"
            fi
            ;;
        *)
            die "未知对刀方案: ${TOOL_CALIBRATION_SCHEME}"
            ;;
    esac
}

function extruder_name {
    local tool="${1}"
    if [ "${tool}" -eq 0 ]; then
        printf "extruder"
    else
        printf "extruder%d" "${tool}"
    fi
}

function extruder_list {
    local count="${1}"
    local i name out
    out=""
    for ((i = 0; i < count; i++)); do
        name="$(extruder_name "${i}")"
        if [ -z "${out}" ]; then
            out="${name}"
        else
            out="${out}, ${name}"
        fi
    done
    printf "%s\n" "${out}"
}

function ask_dock_fan_mode {
    local answer
    while true; do
        cat >&2 <<EOF
请选择 dock_fan 模式：
  1) 一个共享 dock_fan 监听所有 extruder（默认）
  2) 每个 extruder 一个 dock_fan

EOF
        printf "请输入选项 [1/2，默认 1]: " >&2
        read_answer answer
        case "${answer}" in
            ""|1) printf "shared\n"; return ;;
            2) printf "per_tool\n"; return ;;
            *) echo "输入无效，请输入 1 或 2。" >&2 ;;
        esac
    done
}

function ask_tool_hardware_mode {
    local answer
    while true; do
        cat >&2 <<EOF
请选择硬件模式：
  1) 多热端：多个热端复用一个挤出机步进（默认）
  2) 多工具头：每个工具头都有独立挤出机步进

EOF
        printf "请输入选项 [1/2，默认 1]: " >&2
        read_answer answer
        case "${answer}" in
            ""|1) printf "shared_extruder\n"; return ;;
            2) printf "multi_toolhead\n"; return ;;
            *) echo "输入无效，请输入 1 或 2。" >&2 ;;
        esac
    done
}

function ask_multihotend_board {
    local answer answer_number index count i default_label

    if [ "${#BOARD_PROFILE_FILES[@]}" -eq 0 ]; then
        discover_board_profiles
    fi
    count="${#BOARD_PROFILE_FILES[@]}"

    while true; do
        printf "请选择多热端扩展板：\n"
        for ((i = 0; i < count; i++)); do
            default_label=""
            if [ "${i}" -eq 0 ]; then
                default_label="，默认"
            fi
            printf "  %d) %s（最多 %s 个热端%s）\n" \
                "$((i + 1))" "${BOARD_PROFILE_NAMES[i]}" \
                "${BOARD_PROFILE_MAX_TOOLS[i]}" "${default_label}"
        done
        printf "\n请输入选项 [1-%d，默认 1]: " "${count}"
        read_answer answer
        if [ -z "${answer}" ]; then
            select_board_profile_file "${BOARD_PROFILE_FILES[0]}"
            return
        fi
        case "${answer}" in
            *[!0-9]*)
                echo "输入无效，请输入 1..${count}。"
                continue
                ;;
        esac
        if [ "${#answer}" -gt 6 ]; then
            echo "输入无效，请输入 1..${count}。"
            continue
        fi
        answer_number=$((10#${answer}))
        if [ "${answer_number}" -ge 1 ] && [ "${answer_number}" -le "${count}" ]; then
            index=$((answer_number - 1))
            select_board_profile_file "${BOARD_PROFILE_FILES[index]}"
            return
        fi
        echo "输入无效，请输入 1..${count}。"
    done
}

function multihotend_board_name {
    [ -n "${MULTIHOTEND_BOARD_NAME}" ] || die "尚未选择多热端扩展板。"
    printf "%s\n" "${MULTIHOTEND_BOARD_NAME}"
}

function multihotend_board_max_tools {
    [ -n "${MULTIHOTEND_BOARD_MAX_TOOLS}" ] || die "尚未选择多热端扩展板。"
    printf "%s\n" "${MULTIHOTEND_BOARD_MAX_TOOLS}"
}

function multihotend_default_tool_count {
    local board_max="${1}"
    if [ "${board_max}" -lt 4 ]; then
        printf "%s\n" "${board_max}"
    else
        printf "4\n"
    fi
}

function emit_multihotend_board_aliases {
    [ -n "${MULTIHOTEND_BOARD_PROFILE}" ] || die "尚未选择多热端扩展板。"
    emit_profile_body "${MULTIHOTEND_BOARD_PROFILE}" "${MULTIHOTEND_BOARD_BODY_START}" \
        || die "读取 PCB profile 正文失败: ${MULTIHOTEND_BOARD_PROFILE}"
}

function emit_full_extruder_section {
    local tool="${1}"
    local section stepper_label section_comment
    section="$(extruder_name "${tool}")"
    if [ "${tool}" -eq 0 ]; then
        stepper_label="T0"
        section_comment="T0 的 Klipper extruder section；T0 名称固定为 extruder"
    else
        stepper_label="T${tool}"
        section_comment="T${tool} 的 Klipper extruder section；名称为 extruder${tool}"
    fi
    cat <<EOF
# ${section_comment}
[${section}]
step_pin: TODO_${stepper_label}_EXTRUDER_STEP_PIN   # 【必改】挤出机步进 STEP 引脚
dir_pin: TODO_${stepper_label}_EXTRUDER_DIR_PIN     # 【必改】挤出机步进 DIR 引脚；方向相反时在引脚前加/去掉 !
enable_pin: TODO_${stepper_label}_EXTRUDER_ENABLE_PIN # 【必改】挤出机步进 ENABLE 引脚；常见为 ! 开头
microsteps: 32                                      # 挤出机细分；需与驱动配置一致
full_steps_per_rotation: 200                        # 电机每圈整步数；1.8 度电机通常为 200
rotation_distance: TODO_${stepper_label}_ROTATION_DISTANCE # 【必改】挤出机 rotation_distance，按实际挤出机构校准
filament_diameter: 1.750                            # 耗材直径；常见 1.75mm 耗材填 1.750
heater_pin: multihotend:T${tool}H                   # T${tool} 加热棒 MOSFET 输出引脚；实际 MCU 引脚在 [board_pins multihotend] 中填写
nozzle_diameter: 0.400                              # 喷嘴直径；需与实际喷嘴和切片器一致
smooth_time: 0.4                                    # 温度平滑时间；默认值通常可用
min_temp: 0                                         # 允许的最低温度；热敏异常低于此值会报错
max_temp: 300                                       # 允许的最高温度；按热端安全上限调整
sensor_type: Generic 3950                           # 热敏类型默认 Generic 3950；如果实际不是 3950，请改成 PT1000 等对应类型
sensor_pin: multihotend:T${tool}S                   # T${tool} 热敏输入引脚；实际 MCU 引脚在 [board_pins multihotend] 中填写
control: pid                                        # 默认使用 PID 控温；首次使用前建议执行 PID_CALIBRATE 重新校准
pid_kp: 26.213                                      # 默认 PID Kp 占位值；PID 校准后替换为 SAVE_CONFIG 输出值
pid_ki: 1.304                                       # 默认 PID Ki 占位值；PID 校准后替换为 SAVE_CONFIG 输出值
pid_kd: 131.721                                     # 默认 PID Kd 占位值；PID 校准后替换为 SAVE_CONFIG 输出值
max_power: 0.9                                      # 加热最大功率比例；0.9 表示最高 90%
pressure_advance: 0.000                             # 压力提前；按耗材和挤出机校准，0 表示关闭
max_extrude_only_distance: 400                      # 允许纯挤出最大长度；换料/排料动作可能需要较大值
min_extrude_temp: 170                               # 低于该温度禁止挤出，防止冷挤出损坏挤出机

# T${tool} 挤出机 TMC2209 驱动配置
[tmc2209 ${section}]
uart_pin: TODO_${stepper_label}_EXTRUDER_UART_PIN   # 【必改】TMC UART 通讯引脚
interpolate: False                                  # 是否启用 256 细分插值；高速挤出通常建议关闭
run_current: 0.85                                   # 驱动运行电流，按电机额定电流和散热调整
sense_resistor: 0.110                               # 驱动采样电阻；按驱动模块实际值填写
stealthchop_threshold: 0                            # 0 表示挤出机使用 spreadCycle，通常更稳

EOF
}

function emit_heater_only_extruder_section {
    local tool="${1}"
    cat <<EOF
# T${tool} 仅温控热端；复用 T0 的物理挤出机步进
[extruder${tool}]
nozzle_diameter: 0.400                              # 喷嘴直径；需与实际喷嘴和切片器一致
filament_diameter: 1.750                            # 耗材直径；常见 1.75mm 耗材填 1.750
heater_pin: multihotend:T${tool}H                   # T${tool} 加热棒 MOSFET 输出引脚；实际 MCU 引脚在 [board_pins multihotend] 中填写
sensor_type: Generic 3950                           # 热敏类型默认 Generic 3950；如果实际不是 3950，请改成 PT1000 等对应类型
sensor_pin: multihotend:T${tool}S                   # T${tool} 热敏输入引脚；实际 MCU 引脚在 [board_pins multihotend] 中填写
min_temp: 0                                         # 允许的最低温度；热敏异常低于此值会报错
max_temp: 300                                       # 允许的最高温度；按热端安全上限调整
control: pid                                        # 默认使用 PID 控温；首次使用前建议执行 PID_CALIBRATE 重新校准
pid_kp: 26.213                                      # 默认 PID Kp 占位值；PID 校准后替换为 SAVE_CONFIG 输出值
pid_ki: 1.304                                       # 默认 PID Ki 占位值；PID 校准后替换为 SAVE_CONFIG 输出值
pid_kd: 131.721                                     # 默认 PID Kd 占位值；PID 校准后替换为 SAVE_CONFIG 输出值
max_power: 0.9                                      # 加热最大功率比例；0.9 表示最高 90%
min_extrude_temp: 170                               # 低于该温度禁止挤出；该热端无独立步进时仍用于温度安全判断

EOF
}

function generate_multihotend_config {
    local target_dir target_file
    local tool_count board_max default_tool_count board_name board_config_note heaters i name note

    target_dir="$(config_work_path)"
    target_file="${target_dir}/multihotend.cfg"
    if [ -z "${MULTIHOTEND_BOARD_PROFILE}" ]; then
        ask_multihotend_board
    fi
    board_max="$(multihotend_board_max_tools)"
    default_tool_count="$(multihotend_default_tool_count "${board_max}")"
    board_name="$(multihotend_board_name)"
    board_config_note="${MULTIHOTEND_BOARD_CONFIG_NOTE}"
    if [ -z "${MULTIHOTEND_TOOL_COUNT}" ]; then
        MULTIHOTEND_TOOL_COUNT="$(prompt_int_default "请输入热端数量 [1-${board_max}，默认 ${default_tool_count}]: " "${default_tool_count}" 1 "${board_max}")"
    fi
    case "${MULTIHOTEND_TOOL_COUNT}" in
        *[!0-9]*|"") die "热端数量必须是 1..${board_max} 之间的数字。" ;;
    esac
    if [ "${MULTIHOTEND_TOOL_COUNT}" -lt 1 ] || [ "${MULTIHOTEND_TOOL_COUNT}" -gt "${board_max}" ]; then
        die "${board_name} 支持的热端数量为 1..${board_max}，当前值为 ${MULTIHOTEND_TOOL_COUNT}。"
    fi
    tool_count="${MULTIHOTEND_TOOL_COUNT}"

    if [ -f "${target_file}" ]; then
        echo "[CONFIG] multihotend.cfg 已存在，跳过生成（保留用户修改）"
        return
    fi

    echo "[CONFIG] 为 ${board_name} 生成 multihotend.cfg"
    mkdir -p "${target_dir}" || die "无法创建配置目录: ${target_dir}"
    [ -w "${target_dir}" ] || die "当前用户无权写入配置目录: ${target_dir}"

    if [ -z "${TOOL_HARDWARE_MODE}" ]; then
        TOOL_HARDWARE_MODE="$(ask_tool_hardware_mode)"
    fi
    if [ "${TOOL_HARDWARE_MODE}" = "multi_toolhead" ]; then
        DOCK_FAN_MODE="per_tool"
        echo "[CONFIG] 多工具头模式：dock_fan 固定为每个 extruder 一个风扇。"
    elif [ -z "${DOCK_FAN_MODE}" ]; then
        DOCK_FAN_MODE="$(ask_dock_fan_mode)"
    fi
    heaters="$(extruder_list "${tool_count}")"

    {
        cat <<EOF
#####################################################################
# Multihotend 配置模板
#
# 此文件由 install.sh 自动生成。
# 扩展板型号：${board_name}
# 重要：${board_config_note}
# 完成配置后再重启 Klipper。
#####################################################################

# 多热端扩展板 MCU，名称固定为 multihotend
[mcu multihotend]
canbus_uuid: TODO_CANBUS_UUID                       # 【必改】扩展板 CAN UUID，可用 ~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0 查询

# 为 multihotend MCU 定义引脚别名，便于后续引用
[board_pins multihotend]
mcu: multihotend                                    # 这些别名属于上面的 [mcu multihotend]
EOF

        emit_multihotend_board_aliases

        cat <<EOF

#####################################################################
# 风扇
#####################################################################
EOF

        if [ "${DOCK_FAN_MODE}" = "shared" ]; then
            cat <<EOF
# 共享停靠坞风扇，监听所有热端温度
[heater_fan dock_fan]
pin: TODO_DOCK_FAN_PIN                              # 【必改】共享 dock_fan 的风扇输出引脚
max_power: 1.0                                      # 风扇最大功率比例，1.0 表示 100%
kick_start_time: 0.5                                # 风扇启动助推时间，防止低速不转
heater: ${heaters}                                  # 监听的热端列表，任一热端超过阈值都会启动
heater_temp: 50                                     # 热端超过 50°C 时启动风扇
fan_speed: 0.9                                      # 风扇运行速度，0.9 表示 90%

EOF
        else
            for ((i = 0; i < tool_count; i++)); do
                name="$(extruder_name "${i}")"
                cat <<EOF
# T${i} 独立停靠坞风扇，只监听 ${name}
[heater_fan dock_fan_t${i}]
pin: TODO_DOCK_FAN_T${i}_PIN                        # 【必改】T${i} dock_fan 风扇输出引脚
max_power: 1.0                                      # 风扇最大功率比例，1.0 表示 100%
kick_start_time: 0.5                                # 风扇启动助推时间，防止低速不转
heater: ${name}                                     # 只监听当前工具对应的热端
heater_temp: 50                                     # 当前热端超过 50°C 时启动风扇
fan_speed: 0.9                                      # 风扇运行速度，0.9 表示 90%

EOF
            done
        fi

        cat <<EOF
# 热端散热风扇，可共享监听所有热端
[heater_fan hotend_fan]
pin: TODO_HOTEND_FAN_PIN                            # 【必改】热端散热风扇输出引脚
max_power: 1.0                                      # 风扇最大功率比例，1.0 表示 100%
kick_start_time: 0.5                                # 风扇启动助推时间，防止低速不转
heater: ${heaters}                                  # 监听的热端列表，任一热端超过阈值都会启动
heater_temp: 50                                     # 热端超过 50°C 时启动风扇
fan_speed: 1.0                                      # 热端散热风扇运行速度，1.0 表示 100%

#####################################################################
# multihotend MCU 温度
#####################################################################
# 显示 multihotend MCU 板载温度
[temperature_sensor multihotend温度]
sensor_type: temperature_mcu                        # 使用 Klipper 内置 MCU 温度传感器
sensor_mcu: multihotend                             # 读取 [mcu multihotend] 的 MCU 温度
min_temp: 0                                         # MCU 最低安全温度
max_temp: 100                                       # MCU 最高安全温度，超过会报错保护

#####################################################################
# 挤出机 / 热端
#####################################################################
EOF

        if [ "${TOOL_HARDWARE_MODE}" = "multi_toolhead" ]; then
            cat <<EOF
#####################################################################
# 多工具头模式：每个工具头都有独立挤出机步进
#####################################################################
EOF

            for ((i = 0; i < tool_count; i++)); do
                emit_full_extruder_section "${i}"
            done
        else
            cat <<EOF
#####################################################################
# 多热端模式：T0 使用物理挤出机，T1..Tn 仅做温度管理
#####################################################################
EOF

            emit_full_extruder_section 0
            for ((i = 1; i < tool_count; i++)); do
                emit_heater_only_extruder_section "${i}"
            done
        fi
    } > "${target_file}" || die "生成 multihotend.cfg 失败: ${target_file}"

    echo "  -> 已生成 multihotend.cfg"
    for note in "${MULTIHOTEND_BOARD_GENERATE_NOTES[@]}"; do
        printf "     %s\n" "${note}"
    done
    if [ "${TOOL_HARDWARE_MODE}" = "multi_toolhead" ]; then
        echo "     多工具头模式请在 multitool_config.cfg 中确认 sync_extruder_motion: False。"
    fi
}

function patch_multitool_config_tool_count {
    local count="${1}"
    local cfg
    local tmp_cfg

    cfg="$(config_work_path)/multitool_config.cfg"

    [ -f "${cfg}" ] || {
        echo "[CONFIG] 未找到 multitool_config.cfg，无法自动设置 tool_count。"
        return
    }

    tmp_cfg="$(mktemp "${cfg}.tmp.XXXXXX")" || die "创建 multitool_config.cfg 临时文件失败。"
    local awk_status
    if awk -v count="${count}" '
        /^\[/ {
            in_multitool = ($0 == "[multitool]")
        }
        in_multitool && /^[[:space:]]*tool_count[[:space:]]*:/ {
            comment = ""
            if (match($0, /[[:space:]]+#.*/)) {
                comment = substr($0, RSTART)
            }
            print "tool_count: " count comment
            changed = 1
            next
        }
        { print }
        END {
            if (!changed) {
                exit 2
            }
        }
    ' "${cfg}" > "${tmp_cfg}"; then
        awk_status=0
    else
        awk_status=$?
    fi
    case "${awk_status}" in
        0)
            mv "${tmp_cfg}" "${cfg}" || die "写入 multitool_config.cfg 失败: ${cfg}"
            echo "[CONFIG] 已设置 multitool_config.cfg: tool_count=${count}"
            ;;
        2)
            rm -f "${tmp_cfg}"
            echo "[CONFIG] 未在 multitool_config.cfg 的 [multitool] 中找到 tool_count，请手动设置为 ${count}。"
            ;;
        *)
            rm -f "${tmp_cfg}"
            die "设置 multitool_config.cfg tool_count 失败。"
            ;;
    esac
}

function calibration_config_file {
    local target_dir
    target_dir="$(config_work_path)"
    case "${TOOL_CALIBRATION_SCHEME}" in
        touch) printf "%s\n" "${target_dir}/calibration.cfg" ;;
        eddy) printf "%s\n" "${target_dir}/calibration-eddy.cfg" ;;
        *) return 1 ;;
    esac
}

function patch_calibration_tool_count {
    local count="${1}"
    local cfg cfg_base
    local tmp_cfg

    if ! cfg="$(calibration_config_file)"; then
        return
    fi
    cfg_base="$(basename "${cfg}")"

    [ -f "${cfg}" ] || {
        echo "[CONFIG] 未找到 ${cfg_base}，无法自动设置 variable_tool_count。"
        return
    }

    tmp_cfg="$(mktemp "${cfg}.tmp.XXXXXX")" || die "创建 ${cfg_base} 临时文件失败。"
    local awk_status
    if awk -v count="${count}" '
        /^\[/ {
            in_vars = ($0 == "[gcode_macro _TOOL_CALIB_VARS]")
        }
        in_vars && /^[[:space:]]*variable_tool_count[[:space:]]*:/ {
            comment = ""
            if (match($0, /[[:space:]]+#.*/)) {
                comment = substr($0, RSTART)
            }
            print "variable_tool_count: " count comment
            changed = 1
            next
        }
        { print }
        END {
            if (!changed) {
                exit 2
            }
        }
    ' "${cfg}" > "${tmp_cfg}"; then
        awk_status=0
    else
        awk_status=$?
    fi
    case "${awk_status}" in
        0)
            mv "${tmp_cfg}" "${cfg}" || die "写入 ${cfg_base} 失败: ${cfg}"
            echo "[CONFIG] 已设置 ${cfg_base}: variable_tool_count=${count}"
            ;;
        2)
            rm -f "${tmp_cfg}"
            echo "[CONFIG] 未在 ${cfg_base} 的 _TOOL_CALIB_VARS 中找到 variable_tool_count，请手动设置为 ${count}。"
            ;;
        *)
            rm -f "${tmp_cfg}"
            die "设置 ${cfg_base} variable_tool_count 失败。"
            ;;
    esac
}

function patch_generated_tool_count_configs {
    local count="${MULTIHOTEND_TOOL_COUNT}"
    if [ -z "${count}" ]; then
        return
    fi
    patch_multitool_config_tool_count "${count}"
    patch_calibration_tool_count "${count}"
}

function ask_toolchange_scheme {
    local answer answer_number index count i

    if [ "${#TOOLCHANGE_PROFILE_FILES[@]}" -eq 0 ]; then
        discover_toolchange_profiles
    fi
    count="${#TOOLCHANGE_PROFILE_FILES[@]}"

    while true; do
        printf "请选择换头方案：\n"
        printf "  0) 自定义：自定义换头/换热端移动路径。（默认）\n"
        for ((i = 0; i < count; i++)); do
            printf "  %d) %s：%s\n" \
                "$((i + 1))" "${TOOLCHANGE_PROFILE_NAMES[i]}" \
                "${TOOLCHANGE_PROFILE_DESCRIPTIONS[i]}"
        done
        printf "\n请输入选项 [0-%d，默认 0]: " "${count}"
        read_answer answer
        if [ -z "${answer}" ] || [ "${answer}" = "0" ]; then
            select_custom_toolchange_scheme
            return
        fi
        case "${answer}" in
            *[!0-9]*)
                echo "输入无效，请输入 0..${count}。"
                continue
                ;;
        esac
        if [ "${#answer}" -gt 6 ]; then
            echo "输入无效，请输入 0..${count}。"
            continue
        fi
        answer_number=$((10#${answer}))
        if [ "${answer_number}" -ge 1 ] && [ "${answer_number}" -le "${count}" ]; then
            index=$((answer_number - 1))
            select_toolchange_profile_file "${TOOLCHANGE_PROFILE_FILES[index]}"
            return
        fi
        echo "输入无效，请输入 0..${count}。"
    done
}

function ask_tool_calibration_scheme {
    local answer
    while true; do
        cat <<EOF
请选择对刀方案：
  0) 肉眼对刀：不安装对刀插件，不部署对刀配置。（肉眼对刀？？？）
  1) 微动对刀：安装 tools_calibrate.py，并部署 calibration.cfg。（https://github.com/viesturz/klipper-toolchanger/blob/main/tools_calibrate.md）
  2) 涡流对刀：安装 tool_eddy_calibration.py，并部署 calibration-eddy.cfg。（https://github.com/chengxg/tool_eddy_calibration）

EOF
        printf "请输入 0,1,2 [默认 0]: "
        read_answer answer
        case "${answer}" in
            ""|0)
                TOOL_CALIBRATION_SCHEME="none"
                DEPLOYED_CONFIG_FILES="${CONFIG_FILES}"
                return
                ;;
            1)
                TOOL_CALIBRATION_SCHEME="touch"
                DEPLOYED_CONFIG_FILES="${CONFIG_FILES} calibration.cfg"
                return
                ;;
            2)
                TOOL_CALIBRATION_SCHEME="eddy"
                DEPLOYED_CONFIG_FILES="${CONFIG_FILES} calibration-eddy.cfg"
                return
                ;;
            *) echo "输入无效，请输入 0, 1 或 2。" ;;
        esac
    done
}

function ask_multihotend_generation_options {
    local board_max default_tool_count

    if [ -z "${MULTIHOTEND_BOARD_PROFILE}" ]; then
        ask_multihotend_board
    fi
    board_max="$(multihotend_board_max_tools)"
    default_tool_count="$(multihotend_default_tool_count "${board_max}")"
    MULTIHOTEND_TOOL_COUNT="$(prompt_int_default "请输入热端数量 [1-${board_max}，默认 ${default_tool_count}]: " "${default_tool_count}" 1 "${board_max}")"

    case "${TOOLCHANGE_PROFILE_HARDWARE_MODE}" in
        prompt) TOOL_HARDWARE_MODE="$(ask_tool_hardware_mode)" ;;
        shared_extruder|multi_toolhead) TOOL_HARDWARE_MODE="${TOOLCHANGE_PROFILE_HARDWARE_MODE}" ;;
        *) die "换头方案 ${TOOLCHANGE_NAME} 包含未知硬件模式: ${TOOLCHANGE_PROFILE_HARDWARE_MODE}" ;;
    esac

    if [ "${TOOL_HARDWARE_MODE}" = "multi_toolhead" ]; then
        DOCK_FAN_MODE="per_tool"
    else
        DOCK_FAN_MODE="$(ask_dock_fan_mode)"
    fi
}

function install_toolchange_config {
    local target_dir target_file

    if [ -z "${TOOLCHANGE_PROFILE}" ]; then
        return
    fi
    [ -n "${MULTIHOTEND_TOOL_COUNT}" ] || die "生成换头方案配置前必须先设置热端数量。"

    target_dir="$(config_work_path)"
    target_file="${target_dir}/change_tool.cfg"
    if [ -f "${target_file}" ]; then
        echo "[CONFIG] change_tool.cfg 已存在，跳过复制（保留用户修改）"
        return
    fi
    mkdir -p "${target_dir}" || die "无法创建配置目录: ${target_dir}"
    [ -w "${target_dir}" ] || die "当前用户无权写入配置目录: ${target_dir}"

    if ! awk \
            -v start="${TOOLCHANGE_BODY_START}" \
            -v tool_count="${MULTIHOTEND_TOOL_COUNT}" '
        function trim(value) {
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            return value
        }
        function emit_tool_block(    tool, line_number, rendered) {
            for (tool = 0; tool < tool_count; tool++) {
                for (line_number = 1; line_number <= block_size; line_number++) {
                    rendered = block[line_number]
                    gsub(/@@TOOL@@/, tool, rendered)
                    print rendered
                }
            }
        }
        NR < start { next }
        {
            stripped = trim($0)
            if (stripped == "# BEGIN MULTITOOL_TOOL_TEMPLATE") {
                in_repeat = 1
                block_size = 0
                next
            }
            if (stripped == "# END MULTITOOL_TOOL_TEMPLATE") {
                emit_tool_block()
                in_repeat = 0
                next
            }
            if (in_repeat) {
                block[++block_size] = $0
                next
            }
            print
        }
        END {
            if (in_repeat) {
                exit 2
            }
        }
    ' "${TOOLCHANGE_PROFILE}" > "${target_file}"; then
        rm -f "${target_file}"
        die "生成 ${TOOLCHANGE_NAME} change_tool.cfg 失败。"
    fi
    if grep -qF '@@TOOL@@' "${target_file}" \
            || grep -qF '# BEGIN MULTITOOL_TOOL_TEMPLATE' "${target_file}" \
            || grep -qF '# END MULTITOOL_TOOL_TEMPLATE' "${target_file}"; then
        rm -f "${target_file}"
        die "生成 ${TOOLCHANGE_NAME} change_tool.cfg 后仍有未展开的模板标记。"
    fi
    echo "[CONFIG] 已生成 ${TOOLCHANGE_NAME} change_tool.cfg"
}

function patch_multitool_hooks_for_scheme {
    local cfg
    local tmp_cfg
    local release_count pickup_count

    if [ -z "${TOOLCHANGE_PROFILE}" ]; then
        return
    fi

    cfg="$(config_work_path)/multitool_config.cfg"

    [ -f "${cfg}" ] || die "未找到 multitool_config.cfg，无法设置 ${TOOLCHANGE_NAME} 钩子。"
    release_count="$(grep -c '^\[gcode_macro multitool_release_tool\]$' "${cfg}" || true)"
    pickup_count="$(grep -c '^\[gcode_macro multitool_pickup_tool\]$' "${cfg}" || true)"
    [ "${release_count}" -eq 1 ] \
        || die "multitool_config.cfg 必须恰好包含一个 multitool_release_tool 宏。"
    [ "${pickup_count}" -eq 1 ] \
        || die "multitool_config.cfg 必须恰好包含一个 multitool_pickup_tool 宏。"

    tmp_cfg="$(mktemp "${cfg}.tmp.XXXXXX")" || die "创建 multitool_config.cfg 临时文件失败。"
    awk \
        -v release_macro="${TOOLCHANGE_RELEASE_MACRO}" \
        -v pickup_macro="${TOOLCHANGE_PICKUP_MACRO}" '
        function emit_release() {
            print "[gcode_macro multitool_release_tool]"
            print "gcode:"
            print "    " release_macro " TOOL={params.TOOL}"
            print ""
        }
        function emit_pickup() {
            print "[gcode_macro multitool_pickup_tool]"
            print "gcode:"
            print "    " pickup_macro " TOOL={params.TOOL}"
            print ""
        }
        $0 == "[gcode_macro multitool_release_tool]" {
            emit_release()
            skip = 1
            next
        }
        $0 == "[gcode_macro multitool_pickup_tool]" {
            emit_pickup()
            skip = 1
            next
        }
        skip && (/^\[/ || /^#/) {
            skip = 0
        }
        !skip {
            print
        }
    ' "${cfg}" > "${tmp_cfg}" || {
        rm -f "${tmp_cfg}"
        die "调整 ${TOOLCHANGE_NAME} 钩子失败。"
    }
    mv "${tmp_cfg}" "${cfg}" || die "写入 multitool_config.cfg 失败: ${cfg}"
    echo "[CONFIG] 已将 multitool_config.cfg 钩子调整为 ${TOOLCHANGE_NAME} 方案"
}

function patch_printer_cfg {
    local printer_cfg="${CONFIG_PATH}/printer.cfg"

    if [ ! -f "${printer_cfg}" ]; then
        echo "[CONFIG] 未找到 printer.cfg (${printer_cfg})，跳过 include 注入。"
        echo "         请手动在 printer.cfg 顶部添加：${INCLUDE_LINE}"
        return
    fi

    if grep -qF "${INCLUDE_LINE}" "${printer_cfg}"; then
        echo "[CONFIG] printer.cfg 已包含 include 行，跳过注入。"
        return
    fi

    echo "[CONFIG] 在 printer.cfg 顶部插入：${INCLUDE_LINE}"
    local tmp_cfg
    tmp_cfg="$(mktemp "${printer_cfg}.tmp.XXXXXX")" || die "创建 printer.cfg 临时文件失败。"
    if ! {
        printf "%s\n\n" "${INCLUDE_LINE}"
        cat "${printer_cfg}"
    } > "${tmp_cfg}"; then
        rm -f "${tmp_cfg}"
        die "生成新的 printer.cfg 失败。"
    fi
    mv "${tmp_cfg}" "${printer_cfg}" || die "写入 printer.cfg 失败: ${printer_cfg}"
    echo "  -> 已更新 printer.cfg"
}

function restart_klipper {
    echo "[POST-INSTALL] 重启 Klipper 服务..."
    sudo systemctl restart klipper || die "重启 klipper.service 失败，请运行 systemctl status klipper 查看原因。"
}

function restart_moonraker_if_needed {
    if [ "${MOONRAKER_CONF_CHANGED}" -ne 1 ]; then
        return
    fi
    if sudo systemctl list-units --full -all -t service --no-legend 2>/dev/null | grep -qE '(^| )moonraker(@|[-_.a-zA-Z0-9]*\.service|\.service)'; then
        echo "[POST-INSTALL] 重启 Moonraker 服务..."
        sudo systemctl restart moonraker || die "重启 moonraker.service 失败，请运行 systemctl status moonraker 查看原因。"
    else
        echo "[MOONRAKER] 未检测到 moonraker.service，已跳过服务重启。"
    fi
}

function install_frontend_if_requested {
    local target_parent target_base staging backup

    if [ "${FRONTEND_CHOICE}" -eq 0 ]; then
        return
    fi

    [ -f "${FRONTEND_SOURCE_PATH}/index.html" ] || die "本地 ${FRONTEND_NAME} 产物已失效: ${FRONTEND_SOURCE_PATH}"
    [ -f "${FRONTEND_TARGET_PATH}/index.html" ] || die "现有 ${FRONTEND_NAME} 安装已失效: ${FRONTEND_TARGET_PATH}"

    target_parent="$(dirname "${FRONTEND_TARGET_PATH}")"
    target_base="$(basename "${FRONTEND_TARGET_PATH}")"
    staging="$(mktemp -d "${target_parent}/.${target_base}.install.XXXXXX")" || die "无法创建 ${FRONTEND_NAME} 暂存目录。"
    backup="${target_parent}/.${target_base}.previous.$$"

    if [ -e "${backup}" ] || [ -L "${backup}" ]; then
        rm -rf -- "${staging}"
        die "${FRONTEND_NAME} 备份目录已存在: ${backup}"
    fi

    echo "[POST-INSTALL] 从本地产物安装/更新 ${FRONTEND_NAME}: ${FRONTEND_SOURCE_PATH}"
    if ! cp -a "${FRONTEND_SOURCE_PATH}/." "${staging}/"; then
        rm -rf -- "${staging}"
        die "复制 ${FRONTEND_NAME} 本地产物失败。"
    fi
    if [ -f "${FRONTEND_TARGET_PATH}/config.json" ]; then
        if ! cp "${FRONTEND_TARGET_PATH}/config.json" "${staging}/config.json"; then
            rm -rf -- "${staging}"
            die "保留 ${FRONTEND_NAME} config.json 失败。"
        fi
        echo "[CONFIG] 已保留现有 ${FRONTEND_NAME} config.json"
    fi
    if [ ! -f "${staging}/index.html" ]; then
        rm -rf -- "${staging}"
        die "${FRONTEND_NAME} 暂存目录缺少 index.html。"
    fi

    if ! mv "${FRONTEND_TARGET_PATH}" "${backup}"; then
        rm -rf -- "${staging}"
        die "备份现有 ${FRONTEND_NAME} 目录失败: ${FRONTEND_TARGET_PATH}"
    fi
    if ! mv "${staging}" "${FRONTEND_TARGET_PATH}"; then
        if ! mv "${backup}" "${FRONTEND_TARGET_PATH}"; then
            die "部署 ${FRONTEND_NAME} 失败，且无法恢复原目录: ${backup}"
        fi
        rm -rf -- "${staging}"
        die "部署 ${FRONTEND_NAME} 失败，已恢复原目录。"
    fi
    if ! rm -rf -- "${backup}"; then
        echo "[WARN] ${FRONTEND_NAME} 已更新，但无法清理备份目录: ${backup}" >&2
    fi

    echo "[DONE] ${FRONTEND_NAME} 已从本地产物更新: ${FRONTEND_TARGET_PATH}"
}

function print_plugins_completion {
    cat <<EOF

[DONE] 插件/网页安装完成。

已处理：
    - Klipper 插件链接及孤儿链接清理
    - Moonraker 组件链接（如已安装 Moonraker）
    - 配套前端（按本次前端选择执行）

按所选模式，本次未修改：
    - ${CONFIG_PATH}/${CONFIG_SUBDIR}/ 下的配置
    - ${CONFIG_PATH}/printer.cfg
    - moonraker.conf

EOF
}

function print_configure_completion {
    local board_name note next_step
    board_name="$(multihotend_board_name)"

    cat <<EOF

[DONE] 安装及换热端配置完成。

扩展板：${board_name}
热端数量：${MULTIHOTEND_TOOL_COUNT}
换头方案：${TOOLCHANGE_NAME}

全新配置已部署到：
    ${CONFIG_PATH}/${CONFIG_SUBDIR}/
        ${DEPLOYED_CONFIG_FILES}
EOF

    if [ -n "${CONFIG_BACKUP_PATH}" ]; then
        cat <<EOF

原配置备份：
    ${CONFIG_BACKUP_PATH}
EOF
    fi

    cat <<EOF

printer.cfg 已检查以下 include：
    ${INCLUDE_LINE}

下一步：
    1. 修改 ${CONFIG_PATH}/${CONFIG_SUBDIR}/multitool_config.cfg
       - 确认 [multitool] tool_count / z_hop / accel_swap 等参数
       - 多热端复用挤出机：保持 sync_extruder_motion: True
       - 多工具头独立挤出机：设置 sync_extruder_motion: False
EOF

    if [ -z "${TOOLCHANGE_PROFILE}" ]; then
        printf "       - 自定义方案：实现 multitool_release_tool / multitool_pickup_tool\n"
    else
        printf "       - %s 方案：确认公共钩子已转发到 %s / %s\n" \
            "${TOOLCHANGE_NAME}" "${TOOLCHANGE_RELEASE_MACRO}" "${TOOLCHANGE_PICKUP_MACRO}"
    fi

    cat <<EOF

    2. 修改 ${CONFIG_PATH}/${CONFIG_SUBDIR}/multihotend.cfg：
       - canbus_uuid
       - fan pin
       - extruder step/dir/enable/uart pin
       - rotation_distance / sensor_type 等挤出机参数
EOF

    for note in "${MULTIHOTEND_BOARD_COMPLETION_NOTES[@]}"; do
        printf "       - %s\n" "${note}"
    done

    next_step=3
    if [ -n "${TOOLCHANGE_PROFILE}" ]; then
        printf "\n    %d. 修改 %s/%s/change_tool.cfg：\n" \
            "${next_step}" "${CONFIG_PATH}" "${CONFIG_SUBDIR}"
        printf "       - 按 %s 模板中的注释完成方案参数配置\n" "${TOOLCHANGE_NAME}"
        for note in "${TOOLCHANGE_COMPLETION_NOTES[@]}"; do
            printf "       - %s\n" "${note}"
        done
        next_step=$((next_step + 1))
    fi

    cat <<EOF

    ${next_step}. 检查 printer.cfg 或其它主配置
       - 确认包含：${INCLUDE_LINE}

    $((next_step + 1)). 涡流对刀详细配置和使用说明
       - https://demo.chengxg.top/pangxie/#/articles/eddy_calibration

    完整教程和配置参考：
    https://github.com/null01024/klipper-toolchange-stats/wiki/Quick-Start
    https://github.com/null01024/klipper-toolchange-stats/wiki/Configuration-Reference

可选: 在 moonraker.conf 中添加 update_manager 以支持 OTA 更新：

    [update_manager klipper-toolchange-stats]
    type: git_repo
    path: ~/klipper-toolchange-stats
    origin: https://github.com/null01024/klipper-toolchange-stats.git
    managed_services: klipper
    primary_branch: main
    install_script: install.sh

EOF
}

function run_install {
    preflight_checks
    sync_repo
    prepare_profile_catalogs

    if [ "${INSTALL_MODE}" = "configure" ]; then
        ask_multihotend_board
        ask_tool_calibration_scheme
        ask_toolchange_scheme
        ask_multihotend_generation_options
        DEPLOYED_CONFIG_FILES="${DEPLOYED_CONFIG_FILES} multihotend.cfg"
        if [ -n "${TOOLCHANGE_PROFILE}" ]; then
            DEPLOYED_CONFIG_FILES="${DEPLOYED_CONFIG_FILES} change_tool.cfg"
        fi
    else
        TOOL_CALIBRATION_SCHEME="none"
        DEPLOYED_CONFIG_FILES=""
    fi

    ask_frontend_choice
    validate_frontend_if_requested
    link_extension
    link_moonraker_components

    if [ "${INSTALL_MODE}" = "configure" ]; then
        patch_moonraker_lane_data_conf
        install_tool_calibration_python
    fi

    clean_orphan_links

    if [ "${INSTALL_MODE}" = "configure" ]; then
        prepare_config_staging
        copy_config
        install_tool_calibration_config
        generate_multihotend_config
        install_toolchange_config
        patch_generated_tool_count_configs
        patch_multitool_hooks_for_scheme
        activate_staged_config
        patch_printer_cfg
    fi

    restart_klipper
    restart_moonraker_if_needed
    install_frontend_if_requested

    if [ "${INSTALL_MODE}" = "configure" ]; then
        print_configure_completion
    else
        print_plugins_completion
    fi
}

function main {
    printf "\n=========================================\n"
    echo "- Klipper multitool-stats 安装/更新脚本 -"
    echo "完整安装文档: https://github.com/null01024/klipper-toolchange-stats/wiki/Quick-Start"
    printf "=========================================\n\n"

    ask_install_mode
    run_install
}

__script_source="${BASH_SOURCE:-${0}}"
if [ "${__script_source}" = "${0}" ]; then
    main "$@"
fi
