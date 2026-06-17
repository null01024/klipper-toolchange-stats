#!/bin/bash
# Klipper multitool-stats 安装/更新脚本
# 用法 (远程):
#   wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
# 用法 (本地):
#   bash ~/klipper-toolchange-stats/install.sh

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
REPO_URL="${REPO_URL:-https://github.com/null01024/klipper-toolchange-stats.git}"

# 配置在 printer.cfg 中的 include 行（写在文件最顶部）
INCLUDE_LINE="[include multitool/*.cfg]"
CONFIG_SUBDIR="multitool"
# 需要部署到用户配置目录的 cfg 列表（空格分隔，已存在则不覆盖）
CONFIG_FILES="multitool_config.cfg calibration.cfg"

set -eu
export LC_ALL=C

RED="\033[0;31m"
RESET="\033[0m"

function die {
    printf "${RED}[ERROR] %s${RESET}\n" "$*" >&2
    exit 1
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
    if [ ! -d "${CONFIG_PATH}" ]; then
        die "未找到 Klipper 配置目录: ${CONFIG_PATH}。如果你的配置目录不在默认位置，请用 CONFIG_PATH=... 覆盖。"
    fi
    [ -w "${CONFIG_PATH}" ] || die "当前用户无权写入 Klipper 配置目录: ${CONFIG_PATH}"
}

function sync_repo {
    local installdirname installbasename
    installdirname="$(dirname "${INSTALL_PATH}")"
    installbasename="$(basename "${INSTALL_PATH}")"
    if [ ! -d "${installdirname}" ]; then
        mkdir -p "${installdirname}" || die "无法创建安装父目录: ${installdirname}"
    fi
    [ -w "${installdirname}" ] || die "当前用户无权写入安装父目录: ${installdirname}"

    if [ ! -d "${INSTALL_PATH}" ]; then
        echo "[DOWNLOAD] 正在克隆仓库..."
        if git -C "${installdirname}" clone "${REPO_URL}" "${installbasename}"; then
            chmod +x "${INSTALL_PATH}/install.sh" || die "无法设置 install.sh 可执行权限: ${INSTALL_PATH}/install.sh"
            printf "[DOWNLOAD] 克隆完成！\n\n"
        else
            die "克隆 git 仓库失败: ${REPO_URL}"
        fi
        return
    fi

    if [ ! -d "${INSTALL_PATH}/.git" ]; then
        die "${INSTALL_PATH} 已存在，但不是 Git 仓库，无法自动更新。请手动检查该目录，或更换 INSTALL_PATH 后重新运行。"
    fi

    local current_branch status_output
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
    if ! git -C "${INSTALL_PATH}" fetch origin "${current_branch}:refs/remotes/origin/${current_branch}"; then
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


function copy_config {
    local target_dir="${CONFIG_PATH}/${CONFIG_SUBDIR}"

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
    cp "${printer_cfg}" "${printer_cfg}.bak.multitool" || die "备份 printer.cfg 失败: ${printer_cfg}.bak.multitool"
    if ! {
        printf "%s\n\n" "${INCLUDE_LINE}"
        cat "${printer_cfg}.bak.multitool"
    } > "${tmp_cfg}"; then
        rm -f "${tmp_cfg}"
        die "生成新的 printer.cfg 失败。"
    fi
    mv "${tmp_cfg}" "${printer_cfg}" || die "写入 printer.cfg 失败: ${printer_cfg}"
    echo "  -> 已备份原文件到 printer.cfg.bak.multitool"
}

function restart_klipper {
    echo "[POST-INSTALL] 重启 Klipper 服务..."
    sudo systemctl restart klipper || die "重启 klipper.service 失败，请运行 systemctl status klipper 查看原因。"
}

printf "\n=========================================\n"
echo "- Klipper multitool-stats 安装/更新脚本 -"
printf "=========================================\n\n"

preflight_checks
sync_repo
link_extension
clean_orphan_links
copy_config
patch_printer_cfg
restart_klipper

cat <<EOF

[DONE] 安装完成。

默认配置已部署到：
    ${CONFIG_PATH}/${CONFIG_SUBDIR}/
        ${CONFIG_FILES}

printer.cfg 顶部已自动加入：
    ${INCLUDE_LINE}

下一步：
    请查看 README 完成配置：
    https://github.com/null01024/klipper-toolchange-stats#readme

可选: 在 moonraker.conf 中添加 update_manager 以支持 OTA 更新：

    [update_manager klipper-toolchange-stats]
    type: git_repo
    path: ~/klipper-toolchange-stats
    origin: https://github.com/null01024/klipper-toolchange-stats.git
    managed_services: klipper
    primary_branch: main
    install_script: install.sh

EOF
