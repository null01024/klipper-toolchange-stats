#!/bin/bash
# Klipper multitool-stats 安装/更新脚本
# 用法 (远程):
#   wget -O - https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | bash
# 用法 (远程 + GitHub HTTP 下载代理):
#   GH_PROXY=https://v6.gh-proxy.org/ wget -O - https://v6.gh-proxy.org/https://raw.githubusercontent.com/null01024/klipper-toolchange-stats/main/install.sh | GH_PROXY=https://v6.gh-proxy.org/ bash
# 用法 (本地):
#   bash ~/klipper-toolchange-stats/install.sh

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-toolchange-stats}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
REPO_URL="${REPO_URL:-https://github.com/null01024/klipper-toolchange-stats.git}"
GH_PROXY="${GH_PROXY:-}"
FRESH_INSTALL=0
TOOLCHANGE_SCHEME="custom"
TOOL_CALIBRATION_SCHEME="none"
TOOL_HARDWARE_MODE=""
FRESH_TOOL_COUNT=""
FRONTEND_CHOICE=0
TOOLCHANGER_STACK_RUNNING="${TOOLCHANGER_STACK_RUNNING:-0}"
TOOLS_CALIBRATE_URL="${TOOLS_CALIBRATE_URL:-https://raw.githubusercontent.com/viesturz/klipper-toolchanger/main/klipper/extras/tools_calibrate.py}"
TOOL_EDDY_CALIBRATION_URL="${TOOL_EDDY_CALIBRATION_URL:-https://raw.githubusercontent.com/chengxg/tool_eddy_calibration/master/tool_eddy_calibration.py}"
CALIBRATION_EDDY_CFG_URL="${CALIBRATION_EDDY_CFG_URL:-https://raw.githubusercontent.com/chengxg/tool_eddy_calibration/master/config/calibration-eddy.cfg}"

# 配置在 printer.cfg 中的 include 行（写在文件最顶部）
INCLUDE_LINE="[include multitool/*.cfg]"
CONFIG_SUBDIR="multitool"
# 需要部署到用户配置目录的 cfg 列表（空格分隔，已存在则不覆盖）
CONFIG_FILES="multitool_config.cfg"
DEPLOYED_CONFIG_FILES="${CONFIG_FILES}"

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

function ask_yes_no_default_no {
    local prompt="${1}"
    local answer
    while true; do
        printf "%s" "${prompt}"
        read_answer answer
        case "${answer}" in
            ""|n|N) return 1 ;;
            y|Y) return 0 ;;
            *) echo "请输入 y 或 n。" ;;
        esac
    done
}

function prompt_int_default {
    local prompt="${1}"
    local default="${2}"
    local min="${3}"
    local max="${4}"
    local answer
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
                if [ "${answer}" -ge "${min}" ] && [ "${answer}" -le "${max}" ]; then
                    printf "%s\n" "${answer}"
                    return
                fi
                echo "请输入 ${min}..${max} 之间的数字。" >&2
                ;;
        esac
    done
}

function ask_fresh_install {
    if ask_yes_no_default_no "是否为新安装？新安装会生成 multitool/multihotend.cfg [y/N]: "; then
        FRESH_INSTALL=1
    else
        FRESH_INSTALL=0
    fi
    echo
}

function ask_frontend_choice {
    local answer
    if [ "${TOOLCHANGER_STACK_RUNNING}" = "1" ]; then
        FRONTEND_CHOICE=0
        return
    fi
    while true; do
        cat <<EOF
请选择是否安装/更新配套前端：
  0. 不安装/更新前端
  1. Fluidd（维护可能不及时）
  2. Mainsail
请输入 0,1,2 [0]: 
EOF
        read_answer answer
        if [ -z "${answer}" ]; then
            answer=0
        fi
        case "${answer}" in
            0|1|2)
                FRONTEND_CHOICE="${answer}"
                break
                ;;
            *)
                echo "请输入 0..2 之间的数字。"
                ;;
        esac
    done
    echo
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
    if [ ! -d "${CONFIG_PATH}" ]; then
        die "未找到 Klipper 配置目录: ${CONFIG_PATH}。如果你的配置目录不在默认位置，请用 CONFIG_PATH=... 覆盖。"
    fi
    [ -w "${CONFIG_PATH}" ] || die "当前用户无权写入 Klipper 配置目录: ${CONFIG_PATH}"
}

function sync_repo {
    local installdirname installbasename proxied_repo_url
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
        if git -C "${installdirname}" clone "${proxied_repo_url}" "${installbasename}"; then
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
            tools_calibrate.py|tool_eddy_calibration.py)
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

function install_tool_calibration_config {
    local target_dir="${CONFIG_PATH}/${CONFIG_SUBDIR}"
    local target_file

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
    local target_dir="${CONFIG_PATH}/${CONFIG_SUBDIR}"
    local target_file="${target_dir}/multihotend.cfg"
    local tool_count dock_fan_mode heaters i name

    if [ -z "${FRESH_TOOL_COUNT}" ]; then
        FRESH_TOOL_COUNT="$(prompt_int_default "请输入热端数量 [1-8，默认 4]: " 4 1 8)"
    fi
    tool_count="${FRESH_TOOL_COUNT}"

    if [ -f "${target_file}" ]; then
        echo "[CONFIG] multihotend.cfg 已存在，跳过生成（保留用户修改）"
        return
    fi

    echo "[CONFIG] 新安装：生成 multihotend.cfg"
    mkdir -p "${target_dir}" || die "无法创建配置目录: ${target_dir}"
    [ -w "${target_dir}" ] || die "当前用户无权写入配置目录: ${target_dir}"

    if [ -z "${TOOL_HARDWARE_MODE}" ]; then
        TOOL_HARDWARE_MODE="$(ask_tool_hardware_mode)"
    fi
    if [ "${TOOL_HARDWARE_MODE}" = "multi_toolhead" ]; then
        dock_fan_mode="per_tool"
        echo "[CONFIG] 多工具头模式：dock_fan 固定为每个 extruder 一个风扇。"
    else
        dock_fan_mode="$(ask_dock_fan_mode)"
    fi
    heaters="$(extruder_list "${tool_count}")"

    {
        cat <<EOF
#####################################################################
# Multihotend 配置模板
#
# 此文件由 install.sh 在新安装模式下生成。
# 重要：请先替换所有 TODO_* 占位，并填写 [board_pins multihotend] 中等号后的真实 MCU 引脚，再重启 Klipper。
#####################################################################

# 多热端扩展板 MCU，名称固定为 multihotend
[mcu multihotend]
canbus_uuid: TODO_CANBUS_UUID                       # 【必改】扩展板 CAN UUID，可用 ~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0 查询

# 为 multihotend MCU 定义引脚别名，便于后续引用
[board_pins multihotend]
mcu: multihotend                                    # 这些别名属于上面的 [mcu multihotend]
aliases:                                            # 【必改】填写等号后的真实 MCU 引脚；别名名称保持不变
    T7H=,T7S=,IO7=,
    T6H=,T6S=,IO6=,
    T5H=,T5S=,IO5=,
    T4H=,T4S=,IO4=,
    T3H=,T3S=,IO3=,
    T2H=,T2S=,IO2=,
    T1H=,T1S=,IO1=,
    T0H=,T0S=,IO0=

#####################################################################
# 风扇
#####################################################################
EOF

        if [ "${dock_fan_mode}" = "shared" ]; then
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
    echo "     请填写其中所有 TODO_* 字段，以及 [board_pins multihotend] 中等号后的真实 MCU 引脚后再使用。"
    if [ "${TOOL_HARDWARE_MODE}" = "multi_toolhead" ]; then
        echo "     多工具头模式请在 multitool_config.cfg 中确认 sync_extruder_motion: False。"
    fi
}

function patch_multitool_config_tool_count {
    local count="${1}"
    local cfg="${CONFIG_PATH}/${CONFIG_SUBDIR}/multitool_config.cfg"
    local tmp_cfg

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
    case "${TOOL_CALIBRATION_SCHEME}" in
        touch) printf "%s\n" "${CONFIG_PATH}/${CONFIG_SUBDIR}/calibration.cfg" ;;
        eddy) printf "%s\n" "${CONFIG_PATH}/${CONFIG_SUBDIR}/calibration-eddy.cfg" ;;
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

function patch_cxchanger_config_tool_count {
    local count="${1}"
    local cfg="${CONFIG_PATH}/${CONFIG_SUBDIR}/change_tool.cfg"
    local tmp_cfg

    [ -f "${cfg}" ] || {
        echo "[CONFIG] 未找到 change_tool.cfg，无法自动调整 CxChanger dock 坐标变量。"
        return
    }

    tmp_cfg="$(mktemp "${cfg}.tmp.XXXXXX")" || die "创建 change_tool.cfg 临时文件失败。"
    local awk_status
    if awk -v count="${count}" '
        function trim(s) {
            sub(/^[[:space:]]+/, "", s)
            sub(/[[:space:]]+$/, "", s)
            return s
        }
        function emit_docks(    i, xkey, ykey, xval, yval) {
            for (i = 0; i < count; i++) {
                xkey = "variable_t" i "_dock_x"
                ykey = "variable_t" i "_dock_y"
                xval = (xkey in values) ? values[xkey] : "0"
                yval = (ykey in values) ? values[ykey] : "0"
                printf "%s: %s               # 【必改】T%d 停靠坞中心 X 坐标\n", xkey, xval, i
                printf "%s: %s               # 【必改】T%d 停靠坞中心 Y 坐标\n", ykey, yval, i
            }
        }
        NR == FNR {
            if ($0 ~ /^[[:space:]]*variable_t[0-9]+_dock_[xy][[:space:]]*:/) {
                line = $0
                sub(/#.*/, "", line)
                split(line, parts, ":")
                key = trim(parts[1])
                value = substr(line, index(line, ":") + 1)
                values[key] = trim(value)
            }
            next
        }
        /^[[:space:]]*variable_t[0-9]+_dock_[xy][[:space:]]*:/ {
            if (in_docks) {
                next
            }
        }
        /各热端停靠坞坐标/ {
            print
            in_docks = 1
            emit_docks()
            changed = 1
            next
        }
        in_docks && /^[[:space:]]*$/ {
            print
            in_docks = 0
            next
        }
        in_docks && /^[[:space:]]*gcode:/ {
            print ""
            print
            in_docks = 0
            next
        }
        !in_docks {
            print
        }
        END {
            if (!changed) {
                exit 2
            }
        }
    ' "${cfg}" "${cfg}" > "${tmp_cfg}"; then
        awk_status=0
    else
        awk_status=$?
    fi
    case "${awk_status}" in
        0)
            mv "${tmp_cfg}" "${cfg}" || die "写入 change_tool.cfg 失败: ${cfg}"
            echo "[CONFIG] 已调整 CxChanger change_tool.cfg dock 坐标变量数量为 ${count}"
            ;;
        2)
            rm -f "${tmp_cfg}"
            echo "[CONFIG] 未在 change_tool.cfg 中找到 CxChanger dock 坐标变量区域，请手动补齐 t0..t$((count - 1))。"
            ;;
        *)
            rm -f "${tmp_cfg}"
            die "调整 CxChanger change_tool.cfg dock 坐标变量失败。"
            ;;
    esac
}

function patch_fresh_install_tool_count_configs {
    local count="${FRESH_TOOL_COUNT}"
    if [ -z "${count}" ]; then
        return
    fi
    patch_multitool_config_tool_count "${count}"
    patch_calibration_tool_count "${count}"
    if [ "${TOOLCHANGE_SCHEME}" = "cxchanger" ]; then
        patch_cxchanger_config_tool_count "${count}"
    fi
}

function ask_toolchange_scheme {
    local answer
    while true; do
        cat <<EOF
请选择换头方案：
  0) 自定义：自定义换头/换热端移动路径。
  1) CxChanger：https://github.com/cx330-TXY/CxChanger

EOF
        printf "请输入 0 或 1 [默认 0]: "
        read_answer answer
        case "${answer}" in
            ""|0) TOOLCHANGE_SCHEME="custom"; return ;;
            1) TOOLCHANGE_SCHEME="cxchanger"; return ;;
            *) echo "输入无效，请输入 0 或 1。" ;;
        esac
    done
}

function ask_tool_calibration_scheme {
    local answer
    while true; do
        cat <<EOF
请选择对刀方案：
  0) 无对刀：不安装对刀插件，不部署对刀配置。（不对刀玩多热端？？）
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

function install_cxchanger_config {
    local target_dir="${CONFIG_PATH}/${CONFIG_SUBDIR}"
    local target_file="${target_dir}/change_tool.cfg"
    local source_file="${INSTALL_PATH}/schemes/CxChanger/change_tool.cfg"

    [ -f "${source_file}" ] || die "缺少 CxChanger 换头模板: ${source_file}"
    if [ -f "${target_file}" ]; then
        echo "[CONFIG] change_tool.cfg 已存在，跳过复制（保留用户修改）"
        return
    fi
    cp "${source_file}" "${target_file}" || die "复制 change_tool.cfg 失败: ${source_file} -> ${target_file}"
    echo "[CONFIG] 已复制 CxChanger change_tool.cfg"
}

function patch_multitool_hooks_for_cxchanger {
    local cfg="${CONFIG_PATH}/${CONFIG_SUBDIR}/multitool_config.cfg"
    local tmp_cfg

    if [ ! -f "${cfg}" ]; then
        echo "[CONFIG] 未找到 multitool_config.cfg，无法自动调整 CxChanger 钩子。"
        return
    fi
    if ! grep -q '^\[gcode_macro multitool_release_tool\]$' "${cfg}" \
            || ! grep -q '^\[gcode_macro multitool_pickup_tool\]$' "${cfg}"; then
        echo "[CONFIG] 未找到 multitool_release_tool / multitool_pickup_tool 宏，无法自动调整。"
        echo "         请手动添加 _release_tool / _pickup_tool 转发钩子。"
        return
    fi

    tmp_cfg="$(mktemp "${cfg}.tmp.XXXXXX")" || die "创建 multitool_config.cfg 临时文件失败。"
    awk '
        function emit_release() {
            print "[gcode_macro multitool_release_tool]"
            print "gcode:"
            print "    _release_tool TOOL={params.TOOL}"
            print ""
        }
        function emit_pickup() {
            print "[gcode_macro multitool_pickup_tool]"
            print "gcode:"
            print "    _pickup_tool TOOL={params.TOOL}"
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
        die "调整 CxChanger 钩子失败。"
    }
    mv "${tmp_cfg}" "${cfg}" || die "写入 multitool_config.cfg 失败: ${cfg}"
    echo "[CONFIG] 已将 multitool_config.cfg 钩子调整为 CxChanger 方案"
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

function install_frontend_if_requested {
    local stack_script="${INSTALL_PATH}/install_toolchanger_stack.sh"
    local frontend_name
    if [ "${FRONTEND_CHOICE}" -eq 0 ]; then
        return
    fi
    [ -f "${stack_script}" ] || die "未找到 install_toolchanger_stack.sh: ${stack_script}"
    case "${FRONTEND_CHOICE}" in
        1)
            frontend_name="Fluidd"
            echo "[POST-INSTALL] 调用 install_toolchanger_stack.sh 安装/更新 ${frontend_name} 前端..."
            SKIP_PLUGIN_INSTALL=1 TOOLCHANGER_STACK_RUNNING=1 GH_PROXY="${GH_PROXY}" \
                bash "${stack_script}" || die "安装/更新 ${frontend_name} 前端失败。"
            ;;
        2)
            frontend_name="Mainsail"
            echo "[POST-INSTALL] 调用 install_toolchanger_stack.sh 安装/更新 ${frontend_name} 前端..."
            SKIP_PLUGIN_INSTALL=1 TOOLCHANGER_STACK_RUNNING=1 GH_PROXY="${GH_PROXY}" \
                FLUIDD_PATH="${HOME}/mainsail" \
                FLUIDD_TOOLCHANGER_REPO="null01024/mainsail-toolchanger" \
                FLUIDD_TOOLCHANGER_ASSET="mainsail.zip" \
                FRONTEND_NAME="Mainsail" \
                FRONTEND_TOOLCHANGER_NAME="mainsail-toolchanger" \
                FRONTEND_UPDATE_MANAGER_NAME="mainsail-toolchanger" \
                bash "${stack_script}" || die "安装/更新 ${frontend_name} 前端失败。"
            ;;
    esac
}

printf "\n=========================================\n"
echo "- Klipper multitool-stats 安装/更新脚本 -"
printf "=========================================\n\n"

preflight_checks
sync_repo
ask_fresh_install
ask_tool_calibration_scheme
ask_frontend_choice
link_extension
install_tool_calibration_python
clean_orphan_links
copy_config
install_tool_calibration_config
if [ "${FRESH_INSTALL}" -eq 1 ]; then
    ask_toolchange_scheme
    if [ "${TOOLCHANGE_SCHEME}" = "cxchanger" ]; then
        TOOL_HARDWARE_MODE="shared_extruder"
    fi
    generate_multihotend_config
    if [ "${TOOLCHANGE_SCHEME}" = "cxchanger" ]; then
        install_cxchanger_config
    fi
    patch_fresh_install_tool_count_configs
    if [ "${TOOLCHANGE_SCHEME}" = "cxchanger" ]; then
        patch_multitool_hooks_for_cxchanger
    fi
fi
patch_printer_cfg
restart_klipper
install_frontend_if_requested

cat <<EOF

[DONE] 安装完成。

默认配置已部署到：
    ${CONFIG_PATH}/${CONFIG_SUBDIR}/
        ${DEPLOYED_CONFIG_FILES}

printer.cfg 顶部已自动加入：
    ${INCLUDE_LINE}

下一步：
    1. 修改 ${CONFIG_PATH}/${CONFIG_SUBDIR}/multitool_config.cfg
       - 确认 [multitool] tool_count / z_hop / accel_swap 等参数
       - 多热端复用挤出机：保持 sync_extruder_motion: True
       - 多工具头独立挤出机：设置 sync_extruder_motion: False
       - 自定义方案：实现 multitool_release_tool / multitool_pickup_tool
       - CxChanger 方案：确认钩子已转发到 _release_tool / _pickup_tool

    2. 如果本次新生成了 multihotend.cfg，必须填写所有 TODO_* 和 board_pins 引脚值：
       - canbus_uuid
       - board_pins aliases 中等号后的真实 MCU 引脚
       - fan pin
       - extruder step/dir/enable/uart pin
       - rotation_distance / sensor_type 等挤出机参数

    3. 如果使用 CxChanger，请修改 ${CONFIG_PATH}/${CONFIG_SUBDIR}/change_tool.cfg
       - 每个工具的 dock_x / dock_y
       - dock_shift_x / dock_dodge_y / dock_safe_y
       - feed_safe / feed_fast / feed_slow

    4. 检查 printer.cfg 或其它主配置
       - 确认包含：${INCLUDE_LINE}

    5. 涡流对刀详细配置和使用说明
       - https://demo.chengxg.top/pangxie/#/articles/eddy_calibration

    更多配置说明请查看 README：
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
