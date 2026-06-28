#!/bin/bash
# Klipper toolchange stack install/update script
#
# Normal:
#   wget -qO- https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install_toolchanger_stack.sh | bash
#
# With GitHub HTTP download proxy:
#   GH_PROXY=https://v6.gh-proxy.org/ wget -qO- https://v6.gh-proxy.org/https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install_toolchanger_stack.sh | GH_PROXY=https://v6.gh-proxy.org/ bash
#
# Local:
#   bash ~/klipper-toolchange-stats/install_toolchanger_stack.sh
#
# Re-run this script to update both the Klipper plugin and the Fluidd fork.

set -euo pipefail
export LC_ALL=C

KLIPPER_STATS_REPO_RAW="${KLIPPER_STATS_REPO_RAW:-https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
FLUIDD_PATH="${FLUIDD_PATH:-${MAINSAIL_PATH:-${HOME}/fluidd}}"
FLUIDD_TOOLCHANGER_REPO="${FLUIDD_TOOLCHANGER_REPO:-${MAINSAIL_TOOLCHANGER_REPO:-null01024/fluidd-toolchanger}}"
FLUIDD_TOOLCHANGER_ASSET="${FLUIDD_TOOLCHANGER_ASSET:-${MAINSAIL_TOOLCHANGER_ASSET:-fluidd.zip}}"
FRONTEND_NAME="${FRONTEND_NAME:-Fluidd}"
FRONTEND_TOOLCHANGER_NAME="${FRONTEND_TOOLCHANGER_NAME:-fluidd-toolchanger}"
FRONTEND_UPDATE_MANAGER_NAME="${FRONTEND_UPDATE_MANAGER_NAME:-fluidd-toolchanger}"
GH_PROXY="${GH_PROXY:-}"
SKIP_PLUGIN_INSTALL="${SKIP_PLUGIN_INSTALL:-0}"

TMP_DIRS=""
RED="\033[0;31m"
YELLOW="\033[0;33m"
RESET="\033[0m"

function cleanup {
    local dir
    for dir in ${TMP_DIRS}; do
        [ -n "${dir}" ] && [ -d "${dir}" ] && rm -rf "${dir}"
    done
}
trap cleanup EXIT

function make_tmp_dir {
    local dir
    if ! dir="$(mktemp -d)"; then
        die "创建临时目录失败。"
    fi
    TMP_DIRS="${TMP_DIRS} ${dir}"
    printf "%s\n" "${dir}"
}

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
        die "不要以 root 身份运行此脚本。请直接用普通用户执行，脚本需要时会调用 sudo。"
    fi

    require_command bash
    require_command sudo "请先安装 sudo，或使用具备 sudo 权限的普通用户。"
    require_command unzip "请先安装 unzip 后重新运行。"
    require_command dirname
    require_command mktemp
    require_command cp
    require_command mv
    require_command awk
    require_command cmp

    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        die "未找到 curl 或 wget，至少需要其中一个用于下载前端 release。"
    fi
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
    local url dest proxied
    url="${1}"
    dest="${2}"
    proxied="$(proxy_url "${url}")"

    echo "[DOWNLOAD] ${url}"
    if [ -n "${GH_PROXY}" ] && [ "${proxied}" != "${url}" ]; then
        echo "           via ${GH_PROXY}"
    fi

    if command -v curl >/dev/null 2>&1; then
        curl -LfsS "${proxied}" -o "${dest}" || die "下载失败: ${url}"
    else
        wget -qO "${dest}" "${proxied}" || die "下载失败: ${url}"
    fi
    [ -s "${dest}" ] || die "下载文件为空: ${url}"
}

function pretty_home_path {
    local path="${1}"
    case "${path}" in
        "${HOME}") printf "~\n" ;;
        "${HOME}/"*) printf "~/%s\n" "${path#"${HOME}/"}" ;;
        *) printf "%s\n" "${path}" ;;
    esac
}

function current_script_dir {
    local source="${BASH_SOURCE:-}"
    if [ -n "${source}" ] && [ -f "${source}" ]; then
        cd "$(dirname "${source}")" && pwd || die "读取当前脚本目录失败。"
    else
        printf "\n"
    fi
}

function run_plugin_installer {
    local script_dir installer tmp installer_url

    if [ "${SKIP_PLUGIN_INSTALL}" = "1" ]; then
        echo
        echo "========================================="
        echo "- 跳过 klipper-toolchange-stats 插件安装 -"
        echo "========================================="
        echo
        echo "[SKIP] 插件安装已由 install.sh 完成。"
        return
    fi

    echo
    echo "========================================="
    echo "- 安装/更新 klipper-toolchange-stats 插件 -"
    echo "========================================="
    echo

    script_dir="$(current_script_dir)"
    installer=""

    if [ -n "${script_dir}" ] && [ -f "${script_dir}/install.sh" ]; then
        installer="${script_dir}/install.sh"
    elif [ -f "${INSTALL_PATH}/install.sh" ]; then
        installer="${INSTALL_PATH}/install.sh"
    fi

    if [ -n "${installer}" ]; then
        TOOLCHANGER_STACK_RUNNING=1 GH_PROXY="${GH_PROXY}" bash "${installer}" || die "执行插件安装脚本失败: ${installer}"
        return
    fi

    tmp="$(make_tmp_dir)"
    installer="${tmp}/install.sh"
    installer_url="${KLIPPER_STATS_REPO_RAW%/}/install.sh"
    download_url "${installer_url}" "${installer}"
    TOOLCHANGER_STACK_RUNNING=1 GH_PROXY="${GH_PROXY}" bash "${installer}" || die "执行下载的插件安装脚本失败: ${installer_url}"
}

function check_existing_fluidd {
    echo
    echo "========================================="
    echo "- 检查原版 ${FRONTEND_NAME} 前端 -"
    echo "========================================="
    echo

    if [ ! -d "${FLUIDD_PATH}" ] || [ ! -f "${FLUIDD_PATH}/index.html" ]; then
        die "未检测到原版 ${FRONTEND_NAME} 前端: ${FLUIDD_PATH}。请先通过 KIAUH 安装原版 ${FRONTEND_NAME} 前端后，再重新运行本脚本。"
    fi

    echo "[OK] 已检测到 ${FRONTEND_NAME} 前端: ${FLUIDD_PATH}"
}

function find_release_root {
    local extract_dir="${1}"
    local child count found

    if [ -f "${extract_dir}/index.html" ]; then
        printf "%s\n" "${extract_dir}"
        return
    fi

    count=0
    found=""
    for child in "${extract_dir}"/*; do
        [ -d "${child}" ] || continue
        count=$((count + 1))
        found="${child}"
    done

    if [ "${count}" -eq 1 ] && [ -f "${found}/index.html" ]; then
        printf "%s\n" "${found}"
        return
    fi

    return 1
}

function install_or_update_fluidd_toolchanger {
    local tmp zip extract staged release_root release_url
    local old_config target_parent

    echo
    echo "========================================="
    echo "- 安装/更新 ${FRONTEND_TOOLCHANGER_NAME} 前端 -"
    echo "========================================="
    echo

    tmp="$(make_tmp_dir)"
    zip="${tmp}/${FLUIDD_TOOLCHANGER_ASSET}"
    extract="${tmp}/extract"
    staged="${tmp}/staged"
    release_url="https://github.com/${FLUIDD_TOOLCHANGER_REPO}/releases/latest/download/${FLUIDD_TOOLCHANGER_ASSET}"

    mkdir -p "${extract}" "${staged}" || die "创建前端临时目录失败: ${tmp}"
    download_url "${release_url}" "${zip}"

    echo "[INSTALL] 解压 release 包..."
    unzip -q "${zip}" -d "${extract}" || die "解压 release 包失败: ${zip}"

    if ! release_root="$(find_release_root "${extract}")"; then
        die "release 包中未找到 index.html，已中止前端更新。请检查 ${FLUIDD_TOOLCHANGER_REPO} 的 ${FLUIDD_TOOLCHANGER_ASSET} 内容。"
    fi

    cp -a "${release_root}/." "${staged}/" || die "复制前端文件到临时目录失败: ${release_root} -> ${staged}"

    old_config=""
    if [ -f "${FLUIDD_PATH}/config.json" ]; then
        old_config="${tmp}/config.json"
        cp "${FLUIDD_PATH}/config.json" "${old_config}" || die "复制现有 config.json 失败: ${FLUIDD_PATH}/config.json"
        cp "${old_config}" "${staged}/config.json" || die "保留 config.json 到新前端目录失败。"
        echo "[CONFIG] 已保留现有 config.json"
    fi

    [ -f "${staged}/index.html" ] || die "解压后的前端目录缺少 index.html，已中止。"

    target_parent="$(dirname "${FLUIDD_PATH}")"
    mkdir -p "${target_parent}" || die "无法创建前端父目录: ${target_parent}"
    [ -w "${target_parent}" ] || die "当前用户无权写入前端父目录: ${target_parent}"

    if [ -e "${FLUIDD_PATH}" ] || [ -L "${FLUIDD_PATH}" ]; then
        echo "[INSTALL] 移除现有前端目录 ${FLUIDD_PATH}"
        rm -rf -- "${FLUIDD_PATH}" || die "移除现有前端目录失败: ${FLUIDD_PATH}"
    fi

    echo "[INSTALL] 部署前端到 ${FLUIDD_PATH}"
    if ! mv "${staged}" "${FLUIDD_PATH}"; then
        die "部署 ${FRONTEND_TOOLCHANGER_NAME} 失败: ${FLUIDD_PATH}"
    fi

    echo "[DONE] ${FRONTEND_TOOLCHANGER_NAME} 已更新。"
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

function remove_update_manager_section {
    local section="${1}"
    local input="${2}"
    local output="${3}"
    awk -v section="${section}" '
        BEGIN { skip = 0 }
        /^\[[^]]+\]/ {
            header = $0
            sub(/[[:space:]]*[#;].*$/, "", header)
            sub(/[[:space:]]+$/, "", header)
            skip = (header == section)
        }
        !skip { print }
    ' "${input}" > "${output}"
}

function append_update_manager_sections {
    local conf="${1}"
    local fluidd_path
    fluidd_path="$(pretty_home_path "${FLUIDD_PATH}")"

    {
        printf "\n"
        printf "[update_manager %s]\n" "${FRONTEND_UPDATE_MANAGER_NAME}"
        printf "type: web\n"
        printf "path: %s\n" "${fluidd_path}"
        printf "repo: %s\n" "${FLUIDD_TOOLCHANGER_REPO}"
        printf "channel: stable\n"
        printf "persistent_files:\n"
        printf "    config.json\n"
    } >> "${conf}"
}

function patch_moonraker_conf {
    local conf tmp tmp1 tmp2 changed

    echo
    echo "========================================="
    echo "- 检查 Moonraker update_manager 配置 -"
    echo "========================================="
    echo

    if [ -n "${MOONRAKER_CONF:-}" ] && [ ! -f "${MOONRAKER_CONF}" ]; then
        die "指定的 MOONRAKER_CONF 不存在: ${MOONRAKER_CONF}"
    fi

    conf="$(discover_moonraker_conf || true)"
    if [ -z "${conf}" ]; then
        echo "[MOONRAKER] 未找到 moonraker.conf，跳过自动配置。"
        echo "            如需指定路径，请使用 MOONRAKER_CONF=/path/to/moonraker.conf。"
        return
    fi
    [ -r "${conf}" ] || die "当前用户无权读取 moonraker.conf: ${conf}"
    [ -w "${conf}" ] || die "当前用户无权写入 moonraker.conf: ${conf}"

    tmp="$(make_tmp_dir)"
    tmp1="${tmp}/moonraker.conf.1"
    tmp2="${tmp}/moonraker.conf.2"

    cp "${conf}" "${tmp1}" || die "复制 moonraker.conf 到临时文件失败: ${conf}"
    remove_update_manager_section "[update_manager fluidd]" "${tmp1}" "${tmp2}" || die "处理 Moonraker 配置段失败: [update_manager fluidd]"
    if ! cp "${tmp2}" "${tmp1}"; then
        warn "更新 Moonraker 临时配置失败，已跳过 update_manager 自动配置。"
        return
    fi
    remove_update_manager_section "[update_manager mainsail]" "${tmp1}" "${tmp2}" || die "处理 Moonraker 配置段失败: [update_manager mainsail]"
    if ! cp "${tmp2}" "${tmp1}"; then
        warn "更新 Moonraker 临时配置失败，已跳过 update_manager 自动配置。"
        return
    fi
    if ! remove_update_manager_section "[update_manager mainsail-toolchanger]" "${tmp1}" "${tmp2}"; then
        warn "删除旧 mainsail-toolchanger update_manager 段失败，已跳过 update_manager 自动配置。"
        return
    fi
    if ! cp "${tmp2}" "${tmp1}"; then
        warn "更新 Moonraker 临时配置失败，已跳过 update_manager 自动配置。"
        return
    fi
    if ! remove_update_manager_section "[update_manager fluidd-toolchanger]" "${tmp1}" "${tmp2}"; then
        warn "删除旧 fluidd-toolchanger update_manager 段失败，已跳过 update_manager 自动配置。"
        return
    fi

    if ! append_update_manager_sections "${tmp2}"; then
        warn "追加新的 update_manager 配置失败，已跳过 Moonraker 自动配置。"
        return
    fi

    if cmp -s "${conf}" "${tmp2}"; then
        echo "[MOONRAKER] update_manager 配置已是目标状态，跳过修改。"
        rm -f "${tmp1}" "${tmp2}" || warn "清理 Moonraker 临时文件失败: ${tmp}"
        return
    fi

    if ! cp "${tmp2}" "${conf}"; then
        warn "写入 moonraker.conf 失败。"
        return
    fi
    rm -f "${tmp1}" "${tmp2}" || warn "清理 Moonraker 临时文件失败: ${tmp}"

    echo "[MOONRAKER] 已更新 ${conf}"

    changed=1
    if [ "${changed}" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
        if systemctl list-units --full -all -t service --no-legend 2>/dev/null | grep -qE '(^| )moonraker(@|[-_.a-zA-Z0-9]*\.service|\.service)'; then
            echo "[POST-INSTALL] 重启 Moonraker 服务..."
            sudo systemctl restart moonraker || warn "重启 moonraker.service 失败，请运行 systemctl status moonraker 查看原因。"
        else
            echo "[MOONRAKER] 未检测到 moonraker.service，已跳过服务重启。"
        fi
    fi
}

function main {
    cat <<EOF

=================================================
- Klipper Toolchanger Stack 安装/更新脚本 -
=================================================

GH_PROXY: ${GH_PROXY:-未启用}
前端类型: ${FRONTEND_NAME}
前端仓库: ${FLUIDD_TOOLCHANGER_REPO}
前端目录: ${FLUIDD_PATH}
前端包名: ${FLUIDD_TOOLCHANGER_ASSET}
插件安装: $([ "${SKIP_PLUGIN_INSTALL}" = "1" ] && printf "跳过" || printf "执行")

EOF

    preflight_checks
    check_existing_fluidd
    run_plugin_installer
    install_or_update_fluidd_toolchanger
    patch_moonraker_conf

    cat <<EOF

[DONE] 一键安装/更新完成。

已处理：
    - klipper-toolchange-stats 插件
    - ${FRONTEND_TOOLCHANGER_NAME} 前端 (${FLUIDD_PATH})
    - moonraker.conf update_manager 配置（如配置文件存在）

再次执行本脚本即可更新插件和前端。

下一步：
    请查看 README 完成配置：
    https://github.com/null01024/klipper-toolchange-stats#readme

EOF
}

__script_source="${BASH_SOURCE:-${0}}"
if [ "${__script_source}" = "${0}" ]; then
    main "$@"
fi
