#!/bin/bash
# Diagnose ZDT EMM42 CAN communication without Klipper.
#
# Defaults follow the ZDT manual CAN format:
#   extended frame id = ID_Addr << 8
#   payload = command + checksum
#
# Examples:
#   ./zdt_emm42_can_diag.sh
#   ./zdt_emm42_can_diag.sh -i can0 -a 1
#   ./zdt_emm42_can_diag.sh --with-addr
#   ./zdt_emm42_can_diag.sh --cmds 24,35,3A,36 --listen 3

set -u
export LC_ALL=C

IFACE="can0"
ADDR="1"
CHECKSUM="6B"
LISTEN_SECONDS="2"
WITH_ADDR="0"
CMDS="24,35,3A,36"
KEEP_LOG="0"

RED="\033[0;31m"
YELLOW="\033[0;33m"
GREEN="\033[0;32m"
RESET="\033[0m"

die() {
    printf "${RED}[ERROR] %s${RESET}\n" "$*" >&2
    exit 1
}

warn() {
    printf "${YELLOW}[WARN] %s${RESET}\n" "$*" >&2
}

info() {
    printf "${GREEN}[INFO] %s${RESET}\n" "$*"
}

usage() {
    cat <<'EOF'
Usage:
  ./zdt_emm42_can_diag.sh [options]

Options:
  -i, --interface IFACE   CAN interface, default: can0
  -a, --addr N            ZDT ID_Addr decimal value, default: 1
  -c, --checksum HEX      Checksum byte, default: 6B
  --cmds LIST             Comma-separated command bytes, default: 24,35,3A,36
  --listen SECONDS        candump capture time, default: 2
  --with-addr             Send serial-shaped payload, e.g. 01 24 6B
  --keep-log              Keep raw candump log and print its path
  -h, --help              Show this help

Notes:
  The default payload for addr=1 cmd=24 is:
    cansend can0 00000100#246B
  With --with-addr it becomes:
    cansend can0 00000100#01246B
EOF
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "未找到命令 $1。请安装 can-utils/iproute2 后重试。"
}

is_uint() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

hex_byte() {
    local value
    value="${1#0x}"
    value="${value#0X}"
    value="$(printf '%s' "${value}" | tr '[:lower:]' '[:upper:]')"
    [ "${#value}" -le 2 ] || die "字节值过长: $1"
    case "${value}" in
        ''|*[!0-9A-F]*) die "不是有效十六进制字节: $1" ;;
    esac
    printf "%02X" "$((16#${value}))"
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -i|--interface)
                [ "$#" -ge 2 ] || die "$1 需要参数"
                IFACE="$2"
                shift 2
                ;;
            -a|--addr)
                [ "$#" -ge 2 ] || die "$1 需要参数"
                ADDR="$2"
                shift 2
                ;;
            -c|--checksum)
                [ "$#" -ge 2 ] || die "$1 需要参数"
                CHECKSUM="$2"
                shift 2
                ;;
            --cmds)
                [ "$#" -ge 2 ] || die "$1 需要参数"
                CMDS="$2"
                shift 2
                ;;
            --listen)
                [ "$#" -ge 2 ] || die "$1 需要参数"
                LISTEN_SECONDS="$2"
                shift 2
                ;;
            --with-addr)
                WITH_ADDR="1"
                shift
                ;;
            --keep-log)
                KEEP_LOG="1"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "未知参数: $1"
                ;;
        esac
    done
}

print_interface_status() {
    info "CAN interface status"
    ip -details -statistics link show "${IFACE}" || die "读取接口失败: ${IFACE}"
    if ! ip link show "${IFACE}" 2>/dev/null | grep -q "UP"; then
        warn "${IFACE} 当前不是 UP 状态"
    fi
}

build_payload() {
    local cmd checksum addr_hex
    cmd="$(hex_byte "$1")"
    checksum="$(hex_byte "${CHECKSUM}")"
    if [ "${WITH_ADDR}" = "1" ]; then
        addr_hex="$(printf "%02X" "$((10#${ADDR}))")"
        printf "%s%s%s" "${addr_hex}" "${cmd}" "${checksum}"
    else
        printf "%s%s" "${cmd}" "${checksum}"
    fi
}

send_queries() {
    local can_id cmd payload
    can_id="$(printf "%08X" "$((10#${ADDR} << 8))")"
    IFS=',' read -r -a cmd_array <<< "${CMDS}"

    info "Sending ZDT read commands"
    printf "  interface: %s\n" "${IFACE}"
    printf "  addr:      %s\n" "${ADDR}"
    printf "  can id:    %s\n" "${can_id}"
    printf "  payload:   %s\n" "$([ "${WITH_ADDR}" = "1" ] && printf 'with address' || printf 'command only')"

    for cmd in "${cmd_array[@]}"; do
        cmd="$(hex_byte "${cmd}")"
        payload="$(build_payload "${cmd}")"
        printf "  -> cansend %s %s#%s\n" "${IFACE}" "${can_id}" "${payload}"
        cansend "${IFACE}" "${can_id}#${payload}" || warn "发送失败: ${can_id}#${payload}"
        sleep 0.08
    done
}

summarize_log() {
    local log_path can_id compact_id total addr_hits query_hits request_like response_candidates ext_like standard_like error_like
    log_path="$1"
    can_id="$(printf "%08X" "$((10#${ADDR} << 8))")"
    compact_id="$(printf "%03X" "$((10#${ADDR} << 8))")"

    total="$(wc -l < "${log_path}" | tr -d ' ')"
    addr_hits="$(grep -E -c "(^|[[:space:]])(${can_id}|${compact_id})[#[:space:]]" "${log_path}" || true)"
    request_like="$(awk -v id1="${can_id}" -v id2="${compact_id}" \
        -v addr="$(printf "%02X" "$((10#${ADDR}))")" \
        -v checksum="$(hex_byte "${CHECKSUM}")" '
        {
            for (i = 1; i <= NF; i++) {
                if (index($i, "#") == 0) {
                    continue
                }
                split($i, parts, "#")
                frame_id = toupper(parts[1])
                payload = toupper(parts[2])
                if (frame_id != id1 && frame_id != id2) {
                    continue
                }
                # Query shape in CAN mode: CMD + CHECKSUM, e.g. 246B.
                if (length(payload) == 4 && substr(payload, 3, 2) == checksum) {
                    count++
                    continue
                }
                # Compatibility/serial-shaped query: ADDR + CMD + CHECKSUM.
                if (length(payload) == 6 && substr(payload, 1, 2) == addr &&
                    substr(payload, 5, 2) == checksum) {
                    count++
                    continue
                }
            }
        }
        END { print count + 0 }' "${log_path}")"
    query_hits="0"
    IFS=',' read -r -a cmd_array <<< "${CMDS}"
    for cmd in "${cmd_array[@]}"; do
        local payload
        cmd="$(hex_byte "${cmd}")"
        payload="$(build_payload "${cmd}")"
        query_hits="$((query_hits + $(grep -E -c "(^|[[:space:]])(${can_id}|${compact_id})#${payload}([[:space:]]|$)" "${log_path}" || true)))"
    done
    response_candidates="$((addr_hits - request_like))"
    [ "${response_candidates}" -ge 0 ] || response_candidates="0"
    ext_like="$(grep -E -c "(^|[[:space:]])[0-9A-Fa-f]{8}[#[:space:]]" "${log_path}" || true)"
    standard_like="$(grep -E -c "(^|[[:space:]])[0-9A-Fa-f]{3}[#[:space:]]" "${log_path}" || true)"
    error_like="$(grep -E -i -c "ERROR|BUS|ACK|PROTO|CTRL|ERRORFRAME" "${log_path}" || true)"

    info "Capture summary"
    printf "  raw frames:          %s\n" "${total}"
    printf "  frames with ZDT id:  %s (%s or %s)\n" "${addr_hits}" "${can_id}" "${compact_id}"
    printf "  script query echoes: %s\n" "${query_hits}"
    printf "  request-like frames: %s\n" "${request_like}"
    printf "  response candidates: %s\n" "${response_candidates}"
    printf "  extended-like ids:   %s\n" "${ext_like}"
    printf "  standard-like ids:   %s\n" "${standard_like}"
    printf "  error-like lines:    %s\n" "${error_like}"

    if [ "${total}" -eq 0 ]; then
        warn "没有抓到任何 CAN 帧。确认 ${IFACE} 是否正在收发，或 candump 是否有权限。"
    elif [ "${addr_hits}" -eq 0 ] && [ "${ext_like}" -eq 0 ]; then
        warn "只看到非扩展帧，未看到 ZDT 地址对应的扩展帧回复。重点查 EMM42 CAN1_MAP、波特率、接线、终端电阻和 ID_Addr。"
    elif [ "${addr_hits}" -eq 0 ]; then
        warn "看到了扩展帧，但没有 ${can_id}/${compact_id}。可能设备地址不是 ${ADDR}，或总线上有其它扩展设备。"
    elif [ "${response_candidates}" -eq 0 ]; then
        warn "只看到了本机发送查询帧的回显，没有疑似设备回复帧。重点查 EMM42 是否真的在回应。"
    else
        info "抓到了 ZDT 地址相关帧，请检查 raw capture 中的返回 payload。"
    fi

    echo
    info "Raw capture"
    if [ "${total}" -gt 0 ]; then
        tail -n 80 "${log_path}"
    else
        echo "  <empty>"
    fi
}

main() {
    local log_path dump_pid

    parse_args "$@"
    is_uint "${ADDR}" || die "addr 必须是十进制整数: ${ADDR}"
    [ "$((10#${ADDR}))" -ge 1 ] && [ "$((10#${ADDR}))" -le 255 ] || die "addr 必须在 1..255"
    is_uint "${LISTEN_SECONDS}" || die "listen 必须是秒数整数: ${LISTEN_SECONDS}"

    require_command ip
    require_command candump
    require_command cansend
    require_command timeout

    print_interface_status

    log_path="$(mktemp "/tmp/zdt-emm42-candump.XXXXXX.log")"
    info "Starting candump for ${LISTEN_SECONDS}s"
    timeout "${LISTEN_SECONDS}" candump -L "${IFACE}" > "${log_path}" 2>&1 &
    dump_pid="$!"
    sleep 0.2

    send_queries
    wait "${dump_pid}" >/dev/null 2>&1 || true
    summarize_log "${log_path}"

    if [ "${KEEP_LOG}" = "1" ]; then
        info "Log kept at: ${log_path}"
    else
        rm -f "${log_path}"
    fi
}

main "$@"
