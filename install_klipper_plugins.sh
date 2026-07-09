#!/bin/bash
# Install only this repository's Klipper extras plugins.
#
# Usage:
#   bash ~/klipper-toolchange-stats/install_klipper_plugins.sh
#
# Optional overrides:
#   INSTALL_PATH=/path/to/klipper-toolchange-stats KLIPPER_PATH=/path/to/klipper bash install_klipper_plugins.sh

set -eu
export LC_ALL=C

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"

RED="\033[0;31m"
RESET="\033[0m"

function die {
    printf "${RED}[ERROR] %s${RESET}\n" "$*" >&2
    exit 1
}

function current_script_dir {
    local source dir
    source="${BASH_SOURCE[0]}"
    while [ -L "${source}" ]; do
        dir="$(cd -P "$(dirname "${source}")" >/dev/null 2>&1 && pwd)"
        source="$(readlink "${source}")"
        case "${source}" in
            /*) ;;
            *) source="${dir}/${source}" ;;
        esac
    done
    cd -P "$(dirname "${source}")" >/dev/null 2>&1 && pwd
}

SCRIPT_DIR="$(current_script_dir)"
INSTALL_PATH="${INSTALL_PATH:-${SCRIPT_DIR}}"

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

function install_klipper_plugins {
    local src_dir dst_dir files file

    src_dir="${INSTALL_PATH}/klipper/extras"
    dst_dir="${KLIPPER_PATH}/klippy/extras"

    [ -d "${src_dir}" ] || die "未找到本项目 Klipper extras 目录: ${src_dir}"
    [ -d "${dst_dir}" ] || die "未找到 Klipper extras 目录: ${dst_dir}"
    [ -w "${dst_dir}" ] || die "当前用户无权写入 Klipper extras 目录: ${dst_dir}"

    files=("${src_dir}"/*.py)
    [ -e "${files[0]}" ] || die "未找到可安装的 Klipper extras 文件: ${src_dir}/*.py"

    echo "[INSTALL] 链接本项目 Klipper 插件..."
    for file in "${files[@]}"; do
        local base target resolved

        base="$(basename "${file}")"
        case "${base}" in
            tools_calibrate.py|tool_eddy_calibration.py)
                continue
                ;;
        esac

        target="${dst_dir}/${base}"
        if [ -e "${target}" ] || [ -L "${target}" ]; then
            resolved="$(readlink "${target}" 2>/dev/null || true)"
            if [ "${resolved}" != "${file}" ]; then
                echo "  -> [WARN] ${base} 已存在 (${resolved:-非软链})，将覆盖为本仓库链接"
            fi
        fi

        ln -sfnT "${file}" "${target}" || die "创建 Klipper extras 软链接失败: ${target} -> ${file}"
        echo "  -> ${base}"
    done
}

require_command basename
require_command dirname
require_command ln
require_command readlink

install_klipper_plugins

echo "[DONE] Klipper 插件安装完成。"
