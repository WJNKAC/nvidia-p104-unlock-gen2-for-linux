#!/bin/bash
# ============================================================
# push.sh —— 轻量推送：不重载 nvidia，只让设备重新 probe 一次
#
# ## 与 20-install.sh 的区别
#   20-install.sh 会 `rmmod nvidia && modprobe nvidia`，那是为了**让补丁生效**
#   （改完 .ko 二进制必须重载才读新的）。
#
#   但补丁一旦在 .ko 里（重启后仍在），就**不需要再重载** ——
#   只需要让设备重新走一遍 `nv_pci_probe`，让 gen2kr 的 kretprobe 命中一次。
#
#   本脚本就是那条最短路径：
#       insmod gen2kr  →  unbind  →  bind   →  等链路到 Gen2
#
# ## 前提
#   · 补丁已打（patch_no_gen1.py check 显示已打补丁）
#   · nvidia 已加载（正常开机就是）
#
# ## 实测（reports/201）
#   ```
#   [fast] 直接写 SPEED=1，原 20045800 → 20045800
#   [push] 起始 cap2=00000006 config=c0b48d00 lnkcfg0=20045800
#   [push] 写完 cap2=00000006 config=c0b4ad00 lnkcfg0=20045800
#   ★ SUCCESS Gen2 LNKSTA=1042（第 1 次）
#   ```
#   → 链路 5GT/s，pinned H2D 1.561 / D2H 1.708 GB/s
#
# 用法：
#   sudo bash bin/push.sh
#   sudo bash bin/push.sh --no-insmod    # gen2kr 已在内存里，只做 unbind/bind
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

DO_INSMOD=1             # 1 = 先 rmmod 再 insmod（清 triggered 标志）
WAIT_SECONDS=20         # 等链路升到 Gen2 的最长时间

# 退出码
EXIT_PRECHECK=1         # 前置条件不满足
EXIT_PUSH_FAIL=2        # 推送未成功

# ============================================================
# 2. 函数
# ============================================================

# 解析命令行参数
parse_args() {
    case "${1:-}" in
        --no-insmod) DO_INSMOD=0 ;;
        "")          ;;
        *)
            err "未知参数 $1"
            echo "   用法: sudo bash bin/push.sh [--no-insmod]"
            exit 2
            ;;
    esac
}

# 前置检查：nvidia 已加载 + 补丁在位
check_preconditions() {
    local patch_state

    hdr "0. 前置检查"

    if ! lsmod | grep -q '^nvidia '; then
        err "nvidia 未加载 —— 先 modprobe nvidia（或用 20-install.sh）"
        return 1
    fi
    ok "nvidia 已加载（不会动它）"

    patch_state="$(python3 "${PKG_ROOT}/bin/patch_no_gen1.py" check 2>/dev/null \
                   | grep -oE '未打补丁|已打补丁')"

    case "${patch_state}" in
        已打补丁)
            ok "补丁在位" ;;
        未打补丁)
            err "补丁不在 —— 那 push 上去也会被驱动降回来"
            echo "     先跑：sudo bash bin/20-install.sh   （它会 apply + 重载）"
            return 1 ;;
        *)
            warn "无法判定补丁状态，继续但可能无效" ;;
    esac
    return 0
}

# 确保 gen2kr 在内存里，且 triggered 标志是干净的
#   gen2kr 的 init 只挂 kretprobe，本身不推送；
#   但若旧实例已在内存里，triggered 可能已被置位 → push 不会再触发，
#   所以默认先 rmmod 再 insmod。
ensure_gen2kr_loaded() {
    hdr "1. gen2kr（只挂 kretprobe，本身不推送）"

    if lsmod | grep -q '^gen2kr'; then
        echo "   已在内存里"
        if [ "${DO_INSMOD}" = "1" ]; then
            rmmod gen2kr 2>/dev/null && echo "   已重载以清 triggered 标志"
        fi
    fi

    if ! lsmod | grep -q '^gen2kr'; then
        insmod "${BUILD_DIR}/gen2kr.ko" || { err "insmod 失败"; return 1; }
        echo "   已加载 ${BUILD_DIR}/gen2kr.ko"
    fi

    sleep 1
    return 0
}

# 检查是否有进程占用 GPU
#
# ★ 2026-10-29 事故：GPU 被浏览器占用时写 unbind 不会失败、也不会返回，
#   而是卡在 device_release_driver_internal 里，进程落入 D/R 状态、
#   kill -9 无效，只能整机重启。所以这一步是硬阻断，不是警告。
check_gpu_idle() {
    local holders

    holders="$(fuser /dev/nvidia* 2>/dev/null | tr -s ' ')"
    [ -z "${holders}" ] && return 0

    err "有进程正在使用 GPU，unbind 会卡死内核（只能重启）"
    echo "     占用者："
    fuser -v /dev/nvidia* 2>&1 | sed 's/^/       /'
    echo
    echo "     请先关掉这些程序（浏览器 / Electron 应用最常见），然后重试。"
    echo "     ⚠ 实测 unbind 会停在 device_lock 不动，进程落入 D 状态、kill -9 无效。"
    return 1
}

# 通过 unbind/bind 触发一次 nv_pci_probe，让 kretprobe 命中
trigger_probe() {
    hdr "2. 触发一次 probe（kretprobe 在这里命中）"

    if [ ! -w /sys/bus/pci/drivers/nvidia/unbind ]; then
        err "无权写 unbind —— 需要 root"
        return 1
    fi

    check_gpu_idle || return 1

    echo "   unbind ..."
    echo "${DEV_BDF}" > /sys/bus/pci/drivers/nvidia/unbind || { err "unbind 失败"; return 1; }
    sleep 1

    echo "   bind   ..."
    echo "${DEV_BDF}" > /sys/bus/pci/drivers/nvidia/bind || { err "bind 失败"; return 1; }
    return 0
}

# 等链路升到 Gen2，失败时打印排查信息
wait_for_gen2() {
    hdr "3. 等链路升到 Gen2（最多 ${WAIT_SECONDS}s）"

    if wait_gen2 "${WAIT_SECONDS}"; then
        ok "链路 = Gen2    $(read_regs)"
        return 0
    fi

    warn "未升上去（$(read_regs)）"
    echo "   gen2kr 日志："
    dmesg | grep -i gen2kr | tail -10 | sed 's/^/     /'
    echo
    echo "   排查见 docs/故障排查.md"
    echo "   若 CAP2 恒 0x0002 + TLS 恒 1 + 连续失败 ⇒ 整机重启，不要再试"
    return 1
}

# 打印结束语
show_footer() {
    echo
    echo "======================================================"
    echo " 完成。nvidia 模块未被卸载/重载。"
    echo " 验证带宽： build/bwtest2 512 15 | grep pinned   （应 ~1.56/1.71）"
    echo "======================================================"
}

# ============================================================
# 3. main
# ============================================================
main() {
    parse_args "$@"

    need_root
    ensure_build check || { err "探针未编译 → bash bin/build.sh"; exit "${EXIT_PRECHECK}"; }

    echo "======================================================"
    echo " 轻量推送（不重载 nvidia）"
    echo "======================================================"
    echo " 起点：$(read_regs)"

    check_preconditions      || exit "${EXIT_PRECHECK}"
    ensure_gen2kr_loaded     || exit "${EXIT_PRECHECK}"
    trigger_probe            || exit "${EXIT_PRECHECK}"
    wait_for_gen2            || exit "${EXIT_PUSH_FAIL}"

    show_footer
}

main "$@"
