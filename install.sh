#!/bin/bash
# Klipper toolchange-stats 安装脚本
# 用法 (远程):
#   wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
# 用法 (本地):
#   bash ~/klipper-toolchange-stats/install.sh

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
REPO_URL="${REPO_URL:-https://github.com/null01024/klipper-toolchange-stats.git}"

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
}

function check_download {
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
    else
        printf "[DOWNLOAD] 本地已存在仓库，跳过克隆。\n\n"
    fi
}

function link_extension {
    echo "[INSTALL] 链接扩展到 Klipper..."
    for file in "${INSTALL_PATH}"/klipper/extras/*.py; do
        ln -sfn "${file}" "${KLIPPER_PATH}/klippy/extras/"
    done
}

function restart_klipper {
    echo "[POST-INSTALL] 重启 Klipper 服务..."
    sudo systemctl restart klipper
}

printf "\n=========================================\n"
echo "- Klipper toolchange-stats 安装脚本 -"
printf "=========================================\n\n"

preflight_checks
check_download
link_extension
restart_klipper

cat <<'EOF'

[DONE] 安装完成。

请在你的 printer.cfg 中添加：

    [toolchange_stats]

可选: 在 moonraker.conf 中添加 update_manager 以支持 OTA 更新：

    [update_manager klipper-toolchange-stats]
    type: git_repo
    path: ~/klipper-toolchange-stats
    origin: https://github.com/null01024/klipper-toolchange-stats.git
    managed_services: klipper
    primary_branch: main

EOF
