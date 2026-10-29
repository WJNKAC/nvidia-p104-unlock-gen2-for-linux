#!/bin/bash
# ============================================================
# xorg-p104.sh —— 给 P104-100 起/停一个专用 X server
#
# 为什么需要它：**压空闲功耗**，不是拿来跑程序的。
#
#   实测（本机 P104-100 / 580.178.04）：
#     没有专用 X ⇒ 0% 利用率也一直停在 P0、SM 顶 1607 MHz、22.3~22.6 W
#     起了专用 X ⇒ 约 15 秒后进 P8、139 MHz、7.8~8.0 W（省 ~14.6 W）
#     停掉 X     ⇒ 立刻回到 P0 / 22.4 W（可逆）
#
#   P104-100 是 3D controller（无物理输出），但装了驱动照样能起 X server
#   （AllowEmptyInitialConfiguration）。驱动只有接管了显示子系统才会把
#   空闲的卡降进 P8；没有 X 就一直挂在 P0 空烧。
#
# ★ 配置文件**不能**放 /etc/X11/xorg.conf.d/
#   实测：放进去会抢 screen 0，导致 Intel 不被加载、X11 桌面黑屏。
#   所以这里一律用 `Xorg :N -config <file>` 显式指定。
#
# 用法：
#   ./xorg-p104.sh status      看是否在跑 + 它的 GL 渲染器（只读）
#   ./xorg-p104.sh power       看 P-state / 功耗 / 时钟（验证压功耗生效）
#   ./xorg-p104.sh start       起一个专用 X（默认 :8）
#   ./xorg-p104.sh stop        停掉
#   ./xorg-p104.sh restart     stop + start
#   ./xorg-p104.sh gl          在这个 X 上跑 glxinfo
#   ./xorg-p104.sh offload     打印 PRIME offload 用法（跑程序时才需要，与省电无关）
#
# 环境变量：
#   P104_DISPLAY  专用 X 的显示号，默认 :8
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
XORG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XORG_CONF="${XORG_DIR}/p104-xorg.conf"
XORG_BIN="/usr/lib/xorg/Xorg"

# 系统自带的那份（装了 bumblebee 就有），内容与本目录这份等价 —— 优先用它
BUMBLEBEE_CONF="/etc/bumblebee/xorg.conf.nvidia"

# 用哪个配置：系统有就用系统的，没有才用本目录自带的
pick_conf() {
    if [ -f "${BUMBLEBEE_CONF}" ]; then
        printf '%s' "${BUMBLEBEE_CONF}"
    else
        printf '%s' "${XORG_CONF}"
    fi
}

P104_DISPLAY="${P104_DISPLAY:-:8}"
DISP_NUM="${P104_DISPLAY#:}"          # :8 → 8

LOG_DIR="/tmp"
LOG_FILE="${LOG_DIR}/xorg-p104-${DISP_NUM}.log"
PID_FILE="/tmp/.xorg-p104-${DISP_NUM}.pid"

ACTION="${1:-}"

# 颜色
if [ -t 1 ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
    C_R=; C_G=; C_Y=; C_B=; C_0=
fi
ok()   { echo "${C_G}✓${C_0} $*"; }
warn() { echo "${C_Y}⚠${C_0} $*"; }
err()  { echo "${C_R}✗${C_0} $*"; }
hdr()  { echo; echo "${C_B}── $* ──${C_0}"; }

# ============================================================
# 2. 函数
# ============================================================

usage() {
    cat <<EOF
用法: [sudo] ./xorg-p104.sh <动作>

动作:
  status     专用 X 是否在跑 + 它的 GL 渲染器 + 当前 P-state（只读）
  power      看 P-state / 功耗 / 时钟，并判断是否已进 P8（只读）
  start      起一个专用 X（默认 ${P104_DISPLAY}，需 root）→ 压空闲功耗
  stop       停掉（会回到 P0，开始空烧 22 W）
  restart    stop + start
  gl         在这个 X 上跑 glxinfo（确认是 P104）
  offload    打印 PRIME offload 用法（要跑程序才看，与省电无关）

环境变量:
  P104_DISPLAY   专用 X 的显示号，默认 :8（例：P104_DISPLAY=:9 ./xorg-p104.sh start）

说明:
  这个 X 的用途是**压空闲功耗**：没有它，P104 在 0% 利用率下也停在 P0、
  SM 顶 1607 MHz、白烧 22 W；起 X 约 15 秒后进 P8，降到 ~7.8 W。

  配置优先用系统自带的 ${BUMBLEBEE_CONF}（装了 bumblebee 就有），
  没有才用本目录的 p104-xorg.conf。两者等价 —— 原理是让驱动接管
  一个 screen，卡的空闲功耗管理才会把它降进 P8。

  ⚠ 任何时候都会有 Xorg 的场景（比如系统跑 X11 桌面会话），
    根本不需要这个脚本 —— SDDM 自己起的那个 Xorg 就够。
    本脚本只用于 Wayland 会话 / 无桌面这类"没有 Xorg"的情况。

  ⚠ 本目录的 p104-xorg.conf 不能放 /etc/X11/xorg.conf.d/（会黑屏）。
EOF
}

need_root() {
    [ "$(id -u)" = "0" ] || { err "需要 root：sudo $0 $*"; exit 1; }
}

# X 是否在跑（用 socket 判定，比 ps 可靠）
is_running() {
    [ -e "/tmp/.X11-unix/X${DISP_NUM}" ]
}

# 拿 X 的 pid
x_pid() {
    if [ -f "${PID_FILE}" ]; then
        cat "${PID_FILE}"
        return
    fi
    # 没有 pid 文件（比如重启后残留 socket），从进程里找
    pgrep -f "Xorg ${P104_DISPLAY} " 2>/dev/null | head -1
}

# ---------- status ----------
do_status() {
    hdr "专用 X ${P104_DISPLAY}"

    if ! is_running; then
        echo "   未运行"
        echo "   启动: sudo $0 start"
        return 0
    fi

    ok "socket 存在  /tmp/.X11-unix/X${DISP_NUM}"
    local p
    p="$(x_pid)"
    [ -n "${p}" ] && echo "   pid = ${p}" && echo "   日志: ${LOG_FILE}"

    # ★ 功耗要在 GL 查询**之前**读：
    #   一次 glxinfo 就会把卡从 P8 唤醒到 P0（实测最高 59 W），
    #   要 7~9 秒才回 P8。放后面读到的必然是 P0。
    hdr "功耗状态"
    print_power | sed 's/^/   /'

    # 关键：确认渲染器是 P104
    hdr "GL 渲染器"
    echo "   （查 GL 会把卡唤醒到 P0，约 7~9 秒后自己回 P8）"
    local info
    if info="$(DISPLAY="${P104_DISPLAY}" glxinfo -B 2>&1)"; then
        echo "${info}" | grep -E "OpenGL vendor|OpenGL renderer|OpenGL core profile version|Dedicated video memory|direct rendering" | sed 's/^/   /'
    else
        warn "glxinfo 失败（可能没装 mesa-utils）"
    fi
}

# ---------- power ----------
# nvidia-smi 的 pstate / power.draw / clocks 是实时读的（PCIe 字段才是缓存）
POWER_FIELDS="pstate,power.draw,clocks.sm,clocks.mem,temperature.gpu"

print_power() {
    local line
    if ! line="$(nvidia-smi --query-gpu=${POWER_FIELDS} --format=csv,noheader 2>/dev/null)"; then
        echo "nvidia-smi 不可用"
        return 1
    fi
    echo "${line}"
    case "${line}" in
        P8*) echo "→ P8，空闲降档已生效" ;;
        P0*) echo "→ P0，仍在满频待机（没起 X，或在初始化头 15 秒内）" ;;
        *)   echo "→ 其他 P-state" ;;
    esac
}

do_power() {
    hdr "P104 功耗状态"

    if is_running; then
        echo "   专用 X ${P104_DISPLAY}：运行中"
    else
        echo "   专用 X ${P104_DISPLAY}：未运行（预期会停在 P0）"
    fi
    echo

    local i
    for i in 1 2 3; do
        print_power | sed 's/^/   /'
        [ "${i}" -lt 3 ] && sleep 1
    done

    echo
    echo "   参考（本机实测）："
    echo "     无专用 X   → P0, 1607 MHz, 22.3~22.6 W"
    echo "     起 X 后    → P8,  139 MHz,  7.8~8.0 W"
    if ! is_running; then
        echo
        echo "   省电: sudo $0 start"
    fi
}

# ---------- start ----------
do_start() {
    need_root start

    hdr "检查前置"

    if is_running; then
        warn "${P104_DISPLAY} 已在运行（先 stop 再 start，或用 restart）"
        return 0
    fi

    local conf
    conf="$(pick_conf)"
    if [ ! -f "${conf}" ]; then
        err "找不到配置文件 ${conf}"
        return 1
    fi
    if [ "${conf}" = "${BUMBLEBEE_CONF}" ]; then
        ok "配置 ${conf}（系统自带，装了 bumblebee）"
    else
        ok "配置 ${conf}（本目录自带，系统没有 bumblebee）"
    fi

    if [ ! -x "${XORG_BIN}" ]; then
        err "找不到 ${XORG_BIN}"
        return 1
    fi
    ok "Xorg 二进制"

    if ! lspci -Dn 2>/dev/null | grep -q '10de:1b87'; then
        err "找不到 P104-100（10de:1b87）"
        return 1
    fi
    ok "P104-100 在总线"

    hdr "启动 Xorg ${P104_DISPLAY}"
    echo "   \$ ${XORG_BIN} ${P104_DISPLAY} -config ${conf}"

    # -ac：允许本地任意用户连（X 无输出、只跑渲染，本地使用足够）
    nohup "${XORG_BIN}" "${P104_DISPLAY}" \
          -config "${conf}" \
          -logfile "${LOG_FILE}" \
          -nolisten tcp \
          -ac \
          >/dev/null 2>&1 &
    echo $! > "${PID_FILE}"

    # 等 socket 出现（最多 10s）
    local i
    for i in $(seq 1 50); do
        is_running && break
        sleep 0.2
    done

    if ! is_running; then
        err "启动失败。日志最后 20 行："
        tail -20 "${LOG_FILE}" 2>/dev/null | sed 's/^/     /'
        rm -f "${PID_FILE}"
        return 1
    fi
    ok "已启动"

    hdr "验证渲染器"
    local info
    if info="$(DISPLAY="${P104_DISPLAY}" glxinfo -B 2>&1)"; then
        echo "${info}" | grep -E "OpenGL renderer|Dedicated video memory|direct rendering" | sed 's/^/   /'
        if echo "${info}" | grep -q "NVIDIA P104"; then
            ok "渲染器正确"
        else
            warn "渲染器不是 P104，检查 ${LOG_FILE}"
        fi
    else
        warn "glxinfo 不可用，跳过验证"
    fi

    echo
    echo "   卡应在大约 15 秒内掉进 P8（功耗 22 W → ~7.8 W）"
    echo "   确认: $0 power        停止: sudo $0 stop"
}

# ---------- stop ----------
do_stop() {
    need_root stop

    hdr "停止 ${P104_DISPLAY}"

    if ! is_running; then
        echo "   未运行（无需操作）"
        rm -f "${PID_FILE}"
        return 0
    fi

    local p
    p="$(x_pid)"
    if [ -z "${p}" ]; then
        warn "找不到 pid，用 socket 兜底清理"
        rm -f "/tmp/.X11-unix/X${DISP_NUM}"
        rm -f "${PID_FILE}"
        return 0
    fi

    kill "${p}" 2>/dev/null || true

    local i
    for i in $(seq 1 25); do
        is_running || break
        sleep 0.2
    done

    if is_running; then
        warn "TERM 没退出，用 KILL"
        kill -9 "${p}" 2>/dev/null || true
        sleep 1
    fi

    if is_running; then
        err "仍无法停止，socket 还在：/tmp/.X11-unix/X${DISP_NUM}"
        return 1
    fi

    rm -f "${PID_FILE}"
    ok "已停止"
    warn "卡会回到 P0，空闲又开始烧 ~22 W（要省电就重新 start）"
}

# ---------- gl ----------
do_gl() {
    if ! is_running; then
        err "${P104_DISPLAY} 未运行"
        echo "   先跑: sudo $0 start"
        return 1
    fi

    hdr "${P104_DISPLAY} 的 GL 信息"
    DISPLAY="${P104_DISPLAY}" glxinfo -B 2>&1 | sed 's/^/   /'
}

# ---------- offload 说明 ----------
# 注意：这是"要让程序跑在 P104 上"才用的，跟压空闲功耗是两件事。
# 起了专用 X 也不会让 offload 变得更省电，反之亦然。
do_offload() {
    hdr "PRIME render offload（要跑程序才用，与省电无关）"

    echo "   Intel 负责显示、P104 负责渲染。当前桌面/Wayland 都能用："
    echo
    echo "     __NV_PRIME_RENDER_OFFLOAD=1 \\"
    echo "     __GLX_VENDOR_LIBRARY_NAME=nvidia \\"
    echo "       <程序>"
    echo
    echo "   也可以只设 __NV_PRIME_RENDER_OFFLOAD=1（NV 驱动会自己挑 GL 库）"
    echo
    echo "   自测（在任意 X/Wayland 会话里）："
    echo "     __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia glxinfo -B | grep Renderer"
    echo "     → 应显示 NVIDIA P104-100/PCIe/SSE2"
    echo
    echo "   实测（本机，Intel HD P4600 + P104-100）："
    echo "     默认            → Mesa Intel(R) HD Graphics P4600/P4700"
    echo "     offload 后      → NVIDIA P104-100/PCIe/SSE2，8192 MB，direct rendering Yes"
    echo
    echo "   ⚠ 在非活动的 Wayland 会话里（loginctl 的 Active=no）测帧率会得到假数字："
    echo "     合成器不合成 ⇒ SwapBuffers 被节流 ⇒ 只有 ~2 FPS，核显原生也一样。"
}

# ============================================================
# 3. main
# ============================================================
main() {
    case "${ACTION}" in
        status)   do_status ;;
        power)    do_power ;;
        start)    do_start ;;
        stop)     do_stop ;;
        restart)  need_root restart; do_stop; sleep 1; do_start ;;
        gl)       do_gl ;;
        offload)  do_offload ;;
        ""|--help|-h) usage ;;
        *)        err "未知动作：${ACTION}"; usage; exit 2 ;;
    esac
}

main "$@"
