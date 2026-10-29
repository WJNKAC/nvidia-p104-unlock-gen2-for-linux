#!/bin/bash
# ============================================================
# 20-install.sh —— 应用补丁并让链路跑到 Gen2
#
# 做两件事，顺序不能反：
#   ① 给 nvidia.ko 打 2 字节补丁：让 _nv017105rm(mode=1) 在入口 bail
#      ⇒ 驱动不再在每次 cuInit 时把链路降回 Gen1
#   ② 加载 gen2kr 在 nv_pci_probe 返回瞬间解锁声明并推一次 → 链路升到 5GT/s
#      之后补丁负责"保持"，gen2kr 的守卫退化为安全网（补丁丢失时才动手）
#
# ★ 什么时候需要重载 nvidia，什么时候不需要：
#   · 刚 apply 过补丁 ⇒ **必须重载**（改的是 .ko 文件，不重载读的还是旧代码）
#   · 补丁早就在 .ko 里（例如重启后）⇒ **不必重载**，只要让设备重新 probe 一次
#   后者用 bin/push.sh 更快、更轻。本脚本会自动判断。
#
# 用法：
#   sudo bash bin/20-install.sh --dry-run     # 只打印计划，不改任何东西
#   sudo bash bin/20-install.sh               # 执行（自动决定是否重载）
#   sudo bash bin/20-install.sh --no-patch    # 不打补丁，只加载 gen2kr（旧方案）
#   sudo bash bin/20-install.sh --no-kmod     # 只打补丁（用于已解锁过的场景）
#   sudo bash bin/20-install.sh --no-reload   # 强制不重载 nvidia（同 push.sh）
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# 命令行选项
DRY=0                   # 1 = 只打印计划
DO_PATCH=1              # 0 = 跳过补丁（--no-patch）
DO_KMOD=1               # 0 = 跳过 gen2kr（--no-kmod）
FORCE_NO_RELOAD=0       # 1 = 强制不重载（--no-reload）

# 运行时状态
PATCH_CHANGED=0         # 1 = 本次真的改了 .ko
DO_RELOAD=0             # 1 = 需要重载 nvidia

# nvidia 模块的卸载顺序（依赖关系：drm → modeset → uvm → 主模块）
NVIDIA_MODULES=(nvidia_drm nvidia_modeset nvidia_uvm nvidia)

# ============================================================
# 2. 函数
# ============================================================

# 执行命令；dry-run 下只打印
run() {
    if [ "${DRY}" = "1" ]; then
        echo "   ${C_Y}[dry-run]${C_0} $*"
    else
        echo "   $ $*"
        eval "$@"
    fi
}

# 解析命令行参数
parse_args() {
    local a
    for a in "$@"; do
        case "${a}" in
            --dry-run)   DRY=1 ;;
            --no-patch)  DO_PATCH=0 ;;
            --no-kmod)   DO_KMOD=0 ;;
            --no-reload) FORCE_NO_RELOAD=1 ;;
            *)
                err "未知参数 ${a}"
                echo "   可用: --dry-run --no-patch --no-kmod --no-reload"
                exit 2
                ;;
        esac
    done
}

# 前置检查：驱动、设备、MMIO、探针、是否锁死
precheck() {
    hdr "0. 前置检查"

    [ -f "${KO}" ] || { err "找不到 ${KO}"; return 1; }
    ok "驱动 ${KO}"

    lspci -Dn | grep -q '10de:1b87' || { err "找不到 P104-100"; return 1; }
    ok "设备在总线"

    if alive; then
        ok "MMIO 应答"
    else
        err "MMIO 无应答 —— 设备可能已 ZOMBIE，先整机重启"
        echo "     判定工具：python3 bin/linkcap-audit.py"
        return 1
    fi

    ensure_build check || { err "探针未编译 → bash bin/build.sh"; return 1; }

    # ★ 状态污染预检
    # 不能只看 "CAP2=0x0002 + TLS=1 + Gen1" —— 那是**原厂未解锁**的正常样子。
    # 锁死态的特征是「写过解锁位但没生效」，用 is_stuck 判断。
    if is_stuck; then
        warn "检测到驱动侧锁死态（解锁位已置位，但 CAP2 仍 0x0002、链路仍 Gen1）"
        echo "     实测该状态无法自愈（unbind/bind ×6、重训 ×13、重写 TLS ×20 全失败），"
        echo "     ${C_R}只能整机重启${C_0}。"
        echo "     判据：0x8841C=$(printf '0x%08x' "$(_r 0x8841C)")（bit15/13/11 已置位）"
    fi
    return 0
}

# 第一步：打 2 字节补丁，并记录 .ko 是否真的变了
step_patch() {
    local before after

    if [ "${DO_PATCH}" != "1" ]; then
        hdr "1. 跳过补丁（--no-patch）"
        return 0
    fi

    hdr "1. 应用 2 字节补丁"
    echo "   目标：_nv017105rm 入口的 mode 范围检查"
    echo "     127d66: lea -0x1(%rdx),%eax   →  lea -0x2(%rdx),%eax"
    echo "     127d81: cmp \$0x2,%eax         →  cmp \$0x1,%eax"
    echo "   效果：mode==1（Gen1）在入口直接 bail；mode 2/3 正常；≥4 与原逻辑等价"

    if [ "${DRY}" = "1" ]; then
        python3 "${PKG_ROOT}/bin/patch_no_gen1.py" check 2>&1 | sed 's/^/   /'
        echo "   ${C_Y}[dry-run]${C_0} python3 bin/patch_no_gen1.py apply"
        return 0
    fi

    before="$(md5sum "${KO}" | cut -d' ' -f1)"
    python3 "${PKG_ROOT}/bin/patch_no_gen1.py" apply || { err "补丁失败"; return 1; }
    after="$(md5sum "${KO}" | cut -d' ' -f1)"

    if [ "${before}" != "${after}" ]; then
        PATCH_CHANGED=1
        ok "补丁是新写入的 ⇒ 需要重载 nvidia 才生效"
    else
        ok "补丁本来就在（.ko 未变）⇒ 不需要重载"
    fi
    return 0
}

# 决定是否需要重载
decide_reload() {
    DO_RELOAD=0

    if [ "${FORCE_NO_RELOAD}" = "1" ]; then
        DO_RELOAD=0
    elif [ "${PATCH_CHANGED}" = "1" ]; then
        DO_RELOAD=1
    elif [ "${DRY}" = "1" ] && [ "${DO_PATCH}" = "1" ]; then
        # dry-run 下不知道会不会改，按"可能要重载"展示
        DO_RELOAD=1
    fi
}

# 第二步：视情况重载 nvidia
step_reload() {
    local m

    if [ "${DO_RELOAD}" != "1" ]; then
        hdr "2. 不重载 nvidia（补丁已在 .ko 里，无需重载）"
        if lsmod | grep -q '^nvidia '; then
            ok "nvidia 保持加载中"
            return 0
        fi
        err "nvidia 未加载 —— 请先 modprobe nvidia"
        return 1
    fi

    hdr "2. 重载 nvidia 驱动（让新写入的补丁生效）"

    if lsmod | grep -q '^gen2kr'; then
        run "rmmod gen2kr"
    fi

    for m in "${NVIDIA_MODULES[@]}"; do
        if lsmod | grep -q "^${m} "; then
            run "rmmod ${m} 2>/dev/null || true"
        fi
    done
    sleep 1

    if [ "${DRY}" = "0" ] && lsmod | grep -qE '^nvidia'; then
        err "nvidia 未能卸载，检查占用："
        echo "     fuser -v /dev/nvidia* 2>/dev/null"
        echo "     （Xorg / nvitop / 任何 CUDA 进程都会占住）"
        return 1
    fi
    [ "${DRY}" = "0" ] && ok "已卸干净"

    run "modprobe nvidia"
    run "modprobe nvidia_uvm"
    [ "${DRY}" = "0" ] && sleep 2
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

# 第三步：加载 gen2kr 并触发一次 probe
step_push() {
    if [ "${DO_KMOD}" != "1" ]; then
        hdr "3. 跳过 gen2kr（--no-kmod）"
        return 0
    fi

    hdr "3. 加载 gen2kr 并触发一次 probe"
    echo "   它在 nv_pci_probe 上挂 kretprobe，在 probe 返回的瞬间："
    echo "     ① 解锁端点声明（XP3G + 0x8841C + 0x88610）使 LCAP2 → 0x06"
    echo "     ② 写 0x8C040.SPEED=1（5.0GT/s）"
    echo "     ③ 两端 TLS=2，从根端口 Retrain"

    # ★ 真正执行前必须确认 GPU 空闲
    if [ "${DRY}" = "0" ]; then
        check_gpu_idle || return 1
    fi

    # 已在内存里就先卸掉，清掉 triggered 标志（否则 push 不会再触发）
    if lsmod | grep -q '^gen2kr'; then
        run "rmmod gen2kr 2>/dev/null || true"
        [ "${DRY}" = "0" ] && sleep 1
    fi

    run "insmod ${BUILD_DIR}/gen2kr.ko"
    [ "${DRY}" = "0" ] && sleep 1
    run "echo ${DEV_BDF} > /sys/bus/pci/drivers/nvidia/unbind"
    [ "${DRY}" = "0" ] && sleep 1
    run "echo ${DEV_BDF} > /sys/bus/pci/drivers/nvidia/bind"

    [ "${DRY}" = "1" ] && return 0

    echo "   等待链路升到 Gen2（最多 20s）..."
    if wait_gen2 20; then
        ok "链路 = Gen2    $(read_regs)"
        return 0
    fi

    warn "push 未成功（$(read_regs)）"
    echo "     gen2kr 日志："
    dmesg | grep -i gen2kr | tail -8 | sed 's/^/       /'
    echo
    echo "     若守卫正在自动重试，再等 25s；仍不行则参考 docs/故障排查.md"
    return 0
}

# 第四步：汇总结果
show_result() {
    local regs

    hdr "4. 结果"

    if [ "${DRY}" = "1" ]; then
        echo "   ${C_Y}dry-run 结束，未改动任何东西${C_0}"
        return 0
    fi

    regs="$(read_regs)"
    echo "   ${regs}"

    if is_gen2; then
        ok "链路已是 Gen2"
        echo
        echo "   下一步：sudo bash bin/30-verify.sh    （验证带宽 + 补丁是否真的接管）"
        return 0
    fi

    err "链路仍是 Gen1"
    echo "   可能原因："
    echo "     1. 设备处于驱动侧锁死态 → 整机重启后重试"
    echo "     2. gen2kr push 失败 → dmesg | grep gen2kr"
    echo "     3. 声明未解锁 → CAP2 应为 0x0006，当前 $(cap2)"
}

# ============================================================
# 3. main
# ============================================================
main() {
    parse_args "$@"

    need_root

    echo "======================================================"
    echo " 安装 PCIe Gen2 解锁"
    [ "${DRY}" = "1" ] && echo " ${C_Y}模式：dry-run（不会改动任何东西）${C_0}"
    echo "======================================================"

    precheck   || exit 1
    step_patch || exit 1

    decide_reload
    step_reload || exit 1

    [ "${DRY}" = "0" ] && ok "当前：$(read_regs)"

    step_push    || exit 1
    show_result

    echo "======================================================"
}

main "$@"
