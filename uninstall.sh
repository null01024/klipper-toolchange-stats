#!/bin/bash
# Klipper multitool-stats backend uninstall script
#
# Usage:
#   bash ~/klipper-toolchange-stats/uninstall.sh
#
# This script removes the Klipper/Moonraker backend pieces installed by
# install.sh. It intentionally does not uninstall Fluidd/Mainsail frontend
# directories or frontend update_manager sections.

set -euo pipefail
export LC_ALL=C

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
MOONRAKER_PATH="${MOONRAKER_PATH:-${HOME}/moonraker}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
CONFIG_SUBDIR="multitool"
INCLUDE_LINE="[include multitool/*.cfg]"
SKIP_SERVICE_RESTART="${SKIP_SERVICE_RESTART:-0}"

MOONRAKER_CONF_CHANGED=0

RED="\033[0;31m"
YELLOW="\033[0;33m"
RESET="\033[0m"

function die {
    printf "${RED}[ERROR] %s${RESET}\n" "$*" >&2
    exit 1
}

function warn {
    printf "${YELLOW}[WARN] %s${RESET}\n" "$*" >&2
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

function preflight_checks {
    if [ "${EUID}" -eq 0 ]; then
        die "不要以 root 身份运行此脚本！请使用普通用户执行，脚本需要时会调用 sudo。"
    fi

    require_command awk
    require_command readlink
    require_command rm
    require_command mkdir
    require_command mktemp
    require_command cmp

    if [ "${SKIP_SERVICE_RESTART}" != "1" ]; then
        require_command sudo "请先安装 sudo，或使用具备 sudo 权限的普通用户。"
        require_command systemctl "未检测到 systemctl，请确认这是 systemd 环境。"
    fi
}

function remove_repo_links_in_dir {
    local src_dir="${1}"
    local dst_dir="${2}"
    local label="${3}"
    local target resolved removed

    if [ ! -d "${dst_dir}" ]; then
        echo "[SKIP] 未找到 ${label} 目录: ${dst_dir}"
        return
    fi

    removed=0
    for target in "${dst_dir}"/*.py; do
        [ -L "${target}" ] || continue
        resolved="$(readlink "${target}" 2>/dev/null || true)"
        case "${resolved}" in
            "${src_dir}"/*)
                rm -f "${target}" || die "移除 ${label} 软链接失败: ${target}"
                echo "  -> [REMOVE] $(basename "${target}")"
                removed=1
                ;;
        esac
    done

    if [ "${removed}" -eq 0 ]; then
        echo "[SKIP] 未发现本仓库安装的 ${label} 软链接。"
    fi
}

function unlink_klipper_extras {
    echo
    echo "[UNINSTALL] 移除 Klipper extras 软链接..."
    remove_repo_links_in_dir \
        "${INSTALL_PATH}/klipper/extras" \
        "${KLIPPER_PATH}/klippy/extras" \
        "Klipper extras"
}

function remove_calibration_plugins {
    local extras_dir="${KLIPPER_PATH}/klippy/extras"
    local target removed

    echo
    echo "[UNINSTALL] 移除对刀 Python 插件..."
    if [ ! -d "${extras_dir}" ]; then
        echo "[SKIP] 未找到 Klipper extras 目录: ${extras_dir}"
        return
    fi

    removed=0
    for target in \
        "${extras_dir}/tools_calibrate.py" \
        "${extras_dir}/tool_eddy_calibration.py"
    do
        if [ -e "${target}" ] || [ -L "${target}" ]; then
            rm -f "${target}" || die "移除对刀插件失败: ${target}"
            echo "  -> [REMOVE] $(basename "${target}")"
            removed=1
        fi
    done

    if [ "${removed}" -eq 0 ]; then
        echo "[SKIP] 未发现对刀 Python 插件。"
    fi
}

function unlink_moonraker_components {
    echo
    echo "[UNINSTALL] 移除 Moonraker components 软链接..."
    remove_repo_links_in_dir \
        "${INSTALL_PATH}/moonraker/components" \
        "${MOONRAKER_PATH}/moonraker/components" \
        "Moonraker components"
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

function remove_config_section {
    local section="${1}"
    local src="${2}"
    local dest="${3}"

    awk -v section="${section}" '
        function is_section(line) {
            return line ~ /^[[:space:]]*\[[^]]+\][[:space:]]*$/
        }
        $0 ~ "^[[:space:]]*\\[" section "\\][[:space:]]*$" {
            skip = 1
            changed = 1
            next
        }
        skip && is_section($0) {
            skip = 0
        }
        !skip {
            print
        }
        END {
            if (!changed) {
                exit 2
            }
        }
    ' "${src}" > "${dest}"
}

function patch_moonraker_conf {
    local conf tmp_file status

    echo
    echo "[UNINSTALL] 清理 moonraker.conf 后端组件配置..."
    conf="$(discover_moonraker_conf || true)"
    if [ -z "${conf}" ]; then
        echo "[SKIP] 未找到 moonraker.conf。"
        return
    fi
    [ -r "${conf}" ] || die "当前用户无权读取 moonraker.conf: ${conf}"
    [ -w "${conf}" ] || die "当前用户无权写入 moonraker.conf: ${conf}"

    tmp_file="$(mktemp "${conf}.tmp.XXXXXX")" || die "创建 moonraker.conf 临时文件失败。"
    if remove_config_section "multitool_lane_data" "${conf}" "${tmp_file}"; then
        status=0
    else
        status=$?
    fi

    case "${status}" in
        0)
            if cmp -s "${conf}" "${tmp_file}"; then
                rm -f "${tmp_file}" || warn "清理临时文件失败: ${tmp_file}"
                echo "[SKIP] moonraker.conf 无需修改。"
                return
            fi
            mv "${tmp_file}" "${conf}" || die "写入 moonraker.conf 失败: ${conf}"
            MOONRAKER_CONF_CHANGED=1
            echo "  -> [REMOVE] [multitool_lane_data] (${conf})"
            ;;
        2)
            rm -f "${tmp_file}" || warn "清理临时文件失败: ${tmp_file}"
            echo "[SKIP] moonraker.conf 未启用 [multitool_lane_data]。"
            ;;
        *)
            rm -f "${tmp_file}" || true
            die "处理 moonraker.conf 失败: ${conf}"
            ;;
    esac
}

function patch_printer_cfg {
    local printer_cfg="${CONFIG_PATH}/printer.cfg"
    local tmp_file status

    echo
    echo "[UNINSTALL] 移除 printer.cfg include..."
    if [ ! -f "${printer_cfg}" ]; then
        echo "[SKIP] 未找到 printer.cfg: ${printer_cfg}"
        return
    fi
    [ -r "${printer_cfg}" ] || die "当前用户无权读取 printer.cfg: ${printer_cfg}"
    [ -w "${printer_cfg}" ] || die "当前用户无权写入 printer.cfg: ${printer_cfg}"

    tmp_file="$(mktemp "${printer_cfg}.tmp.XXXXXX")" || die "创建 printer.cfg 临时文件失败。"
    if awk -v include_line="${INCLUDE_LINE}" '
        $0 == include_line {
            changed = 1
            next
        }
        { print }
        END {
            if (!changed) {
                exit 2
            }
        }
    ' "${printer_cfg}" > "${tmp_file}"; then
        status=0
    else
        status=$?
    fi

    case "${status}" in
        0)
            mv "${tmp_file}" "${printer_cfg}" || die "写入 printer.cfg 失败: ${printer_cfg}"
            echo "  -> [REMOVE] ${INCLUDE_LINE}"
            ;;
        2)
            rm -f "${tmp_file}" || warn "清理临时文件失败: ${tmp_file}"
            echo "[SKIP] printer.cfg 未包含 ${INCLUDE_LINE}。"
            ;;
        *)
            rm -f "${tmp_file}" || true
            die "处理 printer.cfg 失败: ${printer_cfg}"
            ;;
    esac
}

function remove_multitool_config_dir {
    local target_dir="${CONFIG_PATH}/${CONFIG_SUBDIR}"

    echo
    echo "[UNINSTALL] 删除 multitool 配置目录..."
    if [ -e "${target_dir}" ] || [ -L "${target_dir}" ]; then
        rm -rf -- "${target_dir}" || die "删除配置目录失败: ${target_dir}"
        echo "  -> [REMOVE] ${target_dir}"
    else
        echo "[SKIP] 未找到配置目录: ${target_dir}"
    fi
}

function restart_klipper {
    if [ "${SKIP_SERVICE_RESTART}" = "1" ]; then
        echo "[SKIP] 已设置 SKIP_SERVICE_RESTART=1，跳过 Klipper 重启。"
        return
    fi

    echo
    echo "[POST-UNINSTALL] 重启 Klipper 服务..."
    sudo systemctl restart klipper || die "重启 klipper.service 失败，请运行 systemctl status klipper 查看原因。"
}

function restart_moonraker_if_needed {
    if [ "${MOONRAKER_CONF_CHANGED}" -ne 1 ]; then
        return
    fi
    if [ "${SKIP_SERVICE_RESTART}" = "1" ]; then
        echo "[SKIP] 已设置 SKIP_SERVICE_RESTART=1，跳过 Moonraker 重启。"
        return
    fi

    if sudo systemctl list-units --full -all -t service --no-legend 2>/dev/null | grep -qE '(^| )moonraker(@|[-_.a-zA-Z0-9]*\.service|\.service)'; then
        echo
        echo "[POST-UNINSTALL] 重启 Moonraker 服务..."
        sudo systemctl restart moonraker || die "重启 moonraker.service 失败，请运行 systemctl status moonraker 查看原因。"
    else
        echo "[MOONRAKER] 未检测到 moonraker.service，已跳过服务重启。"
    fi
}

printf "\n=========================================\n"
echo "- Klipper multitool-stats 后端卸载脚本 -"
printf "=========================================\n\n"

preflight_checks
unlink_klipper_extras
remove_calibration_plugins
unlink_moonraker_components
patch_moonraker_conf
patch_printer_cfg
remove_multitool_config_dir
restart_klipper
restart_moonraker_if_needed

echo
echo "[DONE] 后端卸载完成。前端目录和前端 update_manager 配置未被修改。"
