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
# Re-run this script to update both the Klipper plugin and the Mainsail fork.

set -euo pipefail
export LC_ALL=C

KLIPPER_STATS_REPO_RAW="${KLIPPER_STATS_REPO_RAW:-https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
MAINSAIL_PATH="${MAINSAIL_PATH:-${HOME}/mainsail}"
MAINSAIL_TOOLCHANGER_REPO="${MAINSAIL_TOOLCHANGER_REPO:-null01024/mainsail-toolchanger}"
MAINSAIL_TOOLCHANGER_ASSET="${MAINSAIL_TOOLCHANGER_ASSET:-mainsail.zip}"
GH_PROXY="${GH_PROXY:-}"

TMP_DIRS=""

function cleanup {
    local dir
    for dir in ${TMP_DIRS}; do
        [ -n "${dir}" ] && [ -d "${dir}" ] && rm -rf "${dir}"
    done
}
trap cleanup EXIT

function make_tmp_dir {
    local dir
    dir="$(mktemp -d)"
    TMP_DIRS="${TMP_DIRS} ${dir}"
    printf "%s\n" "${dir}"
}

function die {
    echo "[ERROR] $*" >&2
    exit 1
}

function preflight_checks {
    if [ "${EUID}" -eq 0 ]; then
        die "不要以 root 身份运行此脚本。请直接用普通用户执行，脚本需要时会调用 sudo。"
    fi

    if ! command -v unzip >/dev/null 2>&1; then
        die "未找到 unzip，请先安装 unzip 后重新运行。"
    fi

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
        curl -LfsS "${proxied}" -o "${dest}"
    else
        wget -qO "${dest}" "${proxied}"
    fi
}

function pretty_home_path {
    local path="${1}"
    case "${path}" in
        "${HOME}") printf "~\n" ;;
        "${HOME}/"*) printf "~/%s\n" "${path#"${HOME}/"}" ;;
        *) printf "%s\n" "${path}" ;;
    esac
}

function unique_backup_path {
    local base="${1}"
    local candidate stamp index
    stamp="$(date +%Y%m%d-%H%M%S)"
    candidate="${base}.backup.${stamp}"
    index=1
    while [ -e "${candidate}" ]; do
        candidate="${base}.backup.${stamp}.${index}"
        index=$((index + 1))
    done
    printf "%s\n" "${candidate}"
}

function current_script_dir {
    local source="${BASH_SOURCE[0]:-}"
    if [ -n "${source}" ] && [ -f "${source}" ]; then
        cd "$(dirname "${source}")" && pwd
    else
        printf "\n"
    fi
}

function run_plugin_installer {
    local script_dir installer tmp installer_url

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
        bash "${installer}"
        return
    fi

    tmp="$(make_tmp_dir)"
    installer="${tmp}/install.sh"
    installer_url="${KLIPPER_STATS_REPO_RAW%/}/install.sh"
    download_url "${installer_url}" "${installer}"
    bash "${installer}"
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

function install_or_update_mainsail_toolchanger {
    local tmp zip extract staged release_root release_url
    local old_config backup target_parent

    echo
    echo "========================================="
    echo "- 安装/更新 mainsail-toolchanger 前端 -"
    echo "========================================="
    echo

    tmp="$(make_tmp_dir)"
    zip="${tmp}/${MAINSAIL_TOOLCHANGER_ASSET}"
    extract="${tmp}/extract"
    staged="${tmp}/staged"
    release_url="https://github.com/${MAINSAIL_TOOLCHANGER_REPO}/releases/latest/download/${MAINSAIL_TOOLCHANGER_ASSET}"

    mkdir -p "${extract}" "${staged}"
    download_url "${release_url}" "${zip}"

    echo "[INSTALL] 解压 release 包..."
    unzip -q "${zip}" -d "${extract}"

    if ! release_root="$(find_release_root "${extract}")"; then
        die "release 包中未找到 index.html，已中止前端更新。"
    fi

    cp -a "${release_root}/." "${staged}/"

    old_config=""
    if [ -f "${MAINSAIL_PATH}/config.json" ]; then
        old_config="${tmp}/config.json"
        cp "${MAINSAIL_PATH}/config.json" "${old_config}"
        cp "${old_config}" "${staged}/config.json"
        echo "[CONFIG] 已保留现有 config.json"
    fi

    [ -f "${staged}/index.html" ] || die "解压后的前端目录缺少 index.html，已中止。"

    backup=""
    target_parent="$(dirname "${MAINSAIL_PATH}")"
    mkdir -p "${target_parent}"

    if [ -e "${MAINSAIL_PATH}" ] || [ -L "${MAINSAIL_PATH}" ]; then
        backup="$(unique_backup_path "${MAINSAIL_PATH}")"
        echo "[BACKUP] 备份现有前端目录到 ${backup}"
        mv "${MAINSAIL_PATH}" "${backup}"
    fi

    echo "[INSTALL] 部署前端到 ${MAINSAIL_PATH}"
    if ! mv "${staged}" "${MAINSAIL_PATH}"; then
        if [ -n "${backup}" ] && [ -e "${backup}" ]; then
            echo "[ROLLBACK] 部署失败，恢复 ${MAINSAIL_PATH}"
            mv "${backup}" "${MAINSAIL_PATH}"
        fi
        die "部署 mainsail-toolchanger 失败。"
    fi

    echo "[DONE] mainsail-toolchanger 已更新。"
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
    local stats_path mainsail_path
    stats_path="$(pretty_home_path "${INSTALL_PATH}")"
    mainsail_path="$(pretty_home_path "${MAINSAIL_PATH}")"

    {
        printf "\n"
        printf "[update_manager klipper-toolchange-stats]\n"
        printf "type: git_repo\n"
        printf "path: %s\n" "${stats_path}"
        printf "origin: https://github.com/null01024/klipper-toolchange-stats.git\n"
        printf "managed_services: klipper\n"
        printf "primary_branch: main\n"
        printf "install_script: install.sh\n"
        printf "\n"
        printf "[update_manager mainsail-toolchanger]\n"
        printf "type: web\n"
        printf "path: %s\n" "${mainsail_path}"
        printf "repo: %s\n" "${MAINSAIL_TOOLCHANGER_REPO}"
        printf "channel: stable\n"
        printf "persistent_files:\n"
        printf "    config.json\n"
    } >> "${conf}"
}

function patch_moonraker_conf {
    local conf tmp tmp1 tmp2 backup changed

    echo
    echo "========================================="
    echo "- 检查 Moonraker update_manager 配置 -"
    echo "========================================="
    echo

    conf="$(discover_moonraker_conf || true)"
    if [ -z "${conf}" ]; then
        echo "[MOONRAKER] 未找到 moonraker.conf，跳过自动配置。"
        echo "            如需指定路径，请使用 MOONRAKER_CONF=/path/to/moonraker.conf。"
        return
    fi

    tmp="$(make_tmp_dir)"
    tmp1="${tmp}/moonraker.conf.1"
    tmp2="${tmp}/moonraker.conf.2"

    cp "${conf}" "${tmp1}"
    remove_update_manager_section "[update_manager mainsail]" "${tmp1}" "${tmp2}"
    cp "${tmp2}" "${tmp1}"
    remove_update_manager_section "[update_manager klipper-toolchange-stats]" "${tmp1}" "${tmp2}"
    cp "${tmp2}" "${tmp1}"
    remove_update_manager_section "[update_manager mainsail-toolchanger]" "${tmp1}" "${tmp2}"

    append_update_manager_sections "${tmp2}"

    if cmp -s "${conf}" "${tmp2}"; then
        echo "[MOONRAKER] update_manager 配置已是目标状态，跳过修改。"
        rm -f "${tmp1}" "${tmp2}"
        return
    fi

    backup="${conf}.bak.toolchanger.$(date +%Y%m%d-%H%M%S)"
    cp "${conf}" "${backup}"
    cp "${tmp2}" "${conf}"
    rm -f "${tmp1}" "${tmp2}"

    echo "[MOONRAKER] 已更新 ${conf}"
    echo "            备份文件: ${backup}"

    changed=1
    if [ "${changed}" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
        if systemctl list-units --full -all -t service --no-legend 2>/dev/null | grep -qE '(^| )moonraker(@|[-_.a-zA-Z0-9]*\.service|\.service)'; then
            echo "[POST-INSTALL] 重启 Moonraker 服务..."
            sudo systemctl restart moonraker
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
前端仓库: ${MAINSAIL_TOOLCHANGER_REPO}
前端目录: ${MAINSAIL_PATH}

EOF

    preflight_checks
    run_plugin_installer
    install_or_update_mainsail_toolchanger
    patch_moonraker_conf

    cat <<EOF

[DONE] 一键安装/更新完成。

已处理：
    - klipper-toolchange-stats 插件
    - mainsail-toolchanger 前端 (${MAINSAIL_PATH})
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
