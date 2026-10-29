#!/bin/bash
# ============================================================
# xorg-p104.sh —— 给 P104-100 起/停一个专用 X server
#
# 为什么需要它：P104-100 是 3D controller（无物理输出），但装了 NVIDIA 驱动后
# 照样能起 X server（AllowEmptyInitialConfiguration）。这样可以：
#   · 跑不吃 PRIME offload 的程序
#   · 跑 benchmark 时整屏都是 P104
#   · 用 nvidia-settings 调 P104 的 fan / power
#
# ★ 配置文件**不能**放 /etc/X11/xorg.conf.d/
#   实测：放进去会抢 screen 0，导致 Intel 不被加载、X11 桌面黑屏。
#   所以这里一律用 `Xorg :N -config <file>` 显式指定。
#
# 用法：
#   ./xorg-p104.sh status      看 P104 的 X 是否在跑（只读）
#   ./xorg-p104.sh start       起一个专用 X（默认 :8）
#   ./xorg-p104.sh stop        停掉
#   ./xorg-p104.sh restart     stop + start
#   ./xorg-p104.sh gl         在专用 X 上跑 glxinfo，确认渲染器是 P104
#   ./xorg-p104.sh offload     打印 PRIME offload 的用法（不需要专用 X）
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
  status     看专用 X 是否在跑 + 它的 GL 渲染器（只读）
  start      起一个专用 X（默认 ${P104_DISPLAY}，需 root）
  stop       停掉
  restart    stop + start
  gl         在专用 X 上跑 glxinfo（确认是 P104）
  offload    打印 PRIME offload 用法（不需要专用 X，推荐先试这个）

环境变量:
  P104_DISPLAY   专用 X 的显示号，默认 :8（例：P104_DISPLAY=:9 ./xorg-p104.sh start）

说明:
  P104-100 没有物理输出，这个 X 是"无头"的 —— 看不到画面，但能做 OpenGL 渲染。
  配置文件 ${XORG_CONF##*/} 不能放 /etc/X11/xorg.conf.d/（会黑屏）。
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

    # 关键：确认渲染器是 P104
    hdr "GL 渲染器"
    local info
    if info="$(DISPLAY="${P104_DISPLAY}" glxinfo -B 2>&1)"; then
        echo "${info}" | grep -E "OpenGL vendor|OpenGL renderer|OpenGL core profile version|Dedicated video memory|direct rendering" | sed 's/^/   /'
    else
        warn "glxinfo 失败（可能没装 mesa-utils）"
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

    if [ ! -f "${XORG_CONF}" ]; then
        err "找不到配置文件 ${XORG_CONF}"
        return 1
    fi
    ok "配置 ${XORG_CONF##*/}"

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
    echo "   \$ ${XORG_BIN} ${P104_DISPLAY} -config ${XORG_CONF##*/}"

    # -ac：允许本地任意用户连（X 无输出、只跑渲染，本地使用足够）
    nohup "${XORG_BIN}" "${P104_DISPLAY}" \
          -config "${XORG_CONF}" \
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
    echo "   用法: DISPLAY=${P104_DISPLAY} <程序>"
    echo "   停止: sudo $0 stop"
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
do_offload() {
    hdr "PRIME render offload（推荐，不需要专用 X）"

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
}

# ============================================================
# 3. main
# ============================================================
main() {
    case "${ACTION}" in
        status)   do_status ;;
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
