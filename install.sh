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

function preflight_checks {
    if [ "$EUID" -eq 0 ]; then
        echo "[PRE-CHECK] 不要以 root 身份运行此脚本！"
        exit 1
    fi
    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F 'klipper.service')" ]; then
        printf "[PRE-CHECK] 已检测到 Klipper 服务，继续...\n\n"
    else
        echo "[ERROR] 未找到 Klipper 服务，请先安装 Klipper！"
        exit 1
    fi
    if [ ! -d "${KLIPPER_PATH}/klippy/extras" ]; then
        echo "[ERROR] 未找到 Klipper 源码目录: ${KLIPPER_PATH}"
        exit 1
    fi
    if [ ! -d "${CONFIG_PATH}" ]; then
        echo "[ERROR] 未找到 Klipper 配置目录: ${CONFIG_PATH}"
        echo "        如果你的配置目录不在默认位置，请用 CONFIG_PATH=... 覆盖。"
        exit 1
    fi
}

function sync_repo {
    local installdirname installbasename
    installdirname="$(dirname "${INSTALL_PATH}")"
    installbasename="$(basename "${INSTALL_PATH}")"
    if [ ! -d "${INSTALL_PATH}" ]; then
        echo "[DOWNLOAD] 正在克隆仓库..."
        if git -C "${installdirname}" clone "${REPO_URL}" "${installbasename}"; then
            chmod +x "${INSTALL_PATH}/install.sh"
            printf "[DOWNLOAD] 克隆完成！\n\n"
        else
            echo "[ERROR] 克隆 git 仓库失败！"
            exit 1
        fi
        return
    fi

    if [ ! -d "${INSTALL_PATH}/.git" ]; then
        echo "[ERROR] ${INSTALL_PATH} 已存在，但不是 Git 仓库，无法自动更新。"
        echo "        请手动检查该目录，或更换 INSTALL_PATH 后重新运行。"
        exit 1
    fi

    local current_branch status_output
    current_branch="$(git -C "${INSTALL_PATH}" branch --show-current)"
    if [ -z "${current_branch}" ]; then
        echo "[ERROR] ${INSTALL_PATH} 当前处于 detached HEAD，无法安全自动更新。"
        echo "        请切回普通分支后重新运行，例如：git -C ${INSTALL_PATH} switch main"
        exit 1
    fi

    status_output="$(git -C "${INSTALL_PATH}" status --porcelain)"
    if [ -n "${status_output}" ]; then
        echo "[ERROR] ${INSTALL_PATH} 存在未提交修改，已中止自动更新。"
        echo "        请先提交、stash 或清理本地改动后重新运行。"
        exit 1
    fi

    echo "[UPDATE] 本地已存在仓库，正在更新当前分支 ${current_branch}..."
    if ! git -C "${INSTALL_PATH}" fetch origin "${current_branch}:refs/remotes/origin/${current_branch}"; then
        echo "[ERROR] 拉取远端分支 origin/${current_branch} 失败。"
        exit 1
    fi

    if ! git -C "${INSTALL_PATH}" merge --ff-only "origin/${current_branch}"; then
        echo "[ERROR] 当前分支 ${current_branch} 无法 fast-forward 到 origin/${current_branch}。"
        echo "        本地分支可能已与远端分叉，请手动 merge/rebase 后重新运行。"
        exit 1
    fi

    chmod +x "${INSTALL_PATH}/install.sh"
    printf "[UPDATE] 更新完成！\n\n"
}

function link_extension {
    echo "[INSTALL] 链接扩展到 Klipper..."
    for file in "${INSTALL_PATH}"/klipper/extras/*.py; do
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

        ln -sfnT "${file}" "${target}"
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
                    rm -f "${target}"
                    echo "  -> [CLEAN] 移除孤儿软链 $(basename "${target}") (源已删除: ${resolved})"
                fi
                ;;
        esac
    done
}


function copy_config {
    local target_dir="${CONFIG_PATH}/${CONFIG_SUBDIR}"

    echo "[CONFIG] 部署默认配置到 ${target_dir}/"
    mkdir -p "${target_dir}"

    local cfg target_file source_file
    for cfg in ${CONFIG_FILES}; do
        target_file="${target_dir}/${cfg}"
        source_file="${INSTALL_PATH}/${cfg}"
        if [ -f "${target_file}" ]; then
            echo "  -> 已存在 ${cfg}，跳过覆盖（保留用户修改）"
        else
            cp "${source_file}" "${target_file}"
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
    cp "${printer_cfg}" "${printer_cfg}.bak.multitool"
    {
        printf "%s\n\n" "${INCLUDE_LINE}"
        cat "${printer_cfg}.bak.multitool"
    } > "${printer_cfg}"
    echo "  -> 已备份原文件到 printer.cfg.bak.multitool"
}

function restart_klipper {
    echo "[POST-INSTALL] 重启 Klipper 服务..."
    sudo systemctl restart klipper
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
