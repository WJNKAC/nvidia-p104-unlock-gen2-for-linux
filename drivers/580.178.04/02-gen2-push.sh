#!/bin/bash
# ============================================================
# 02-gen2-push.sh —— 加载内核模块并把 PCIe 链路推到 Gen2
#
# 前提：01-gen2-patch.sh 已经打过补丁并重载/重启过
#       （不满足的话，推上去也会被驱动降回来）
#
# 做什么：编译模块（若无 .ko）→ insmod → unbind/bind 触发一次 probe
#         → gen2kr 在 nv_pci_probe 返回瞬间解锁声明 + 写 SPEED + 重训
#         → 链路升到 5.0GT/s
#
# 用法：
#   sudo bash 02-gen2-push.sh status     # 看模块是否加载 + 链路状态
#   sudo bash 02-gen2-push.sh start      # 加载模块并推送
#   sudo bash 02-gen2-push.sh stop       # 卸载模块（守卫停止，链路会掉回 Gen1）
#   sudo bash 02-gen2-push.sh restart    # stop + start
#   sudo bash 02-gen2-push.sh build      # 只编译模块
#   sudo bash 02-gen2-push.sh uninstall  # stop + 删除编译产物（彻底清理）
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

ACTION="${1:-}"
WAIT_SECONDS=20         # 等链路升到 Gen2 的最长时间

# 退出码
EXIT_PRECHECK=1
EXIT_PUSH_FAIL=2

# ============================================================
# 2. 函数
# ============================================================

usage() {
    cat <<'EOF'
用法: sudo bash 02-gen2-push.sh <动作>

动作:
  status      查看模块加载状态与链路（只读）
  start       编译（若无 .ko）→ 加载模块 → 推送 → 等链路到 Gen2
  stop        卸载模块（链路会掉回 Gen1）
  restart     stop + start
  build       只编译内核模块（普通用户即可，不必 sudo）
  uninstall   stop + 删除编译产物

说明:
  本脚本负责「把链路推上去」。补丁（01）只做到"不再降级"。
  还原时请反向依次执行：02 stop → 01 revert（→ 00 revert 如果打过）
EOF
}

validate() {
    case "${ACTION}" in
        status|start|stop|restart|build|uninstall) ;;
        "")  usage; exit 2 ;;
        *)   err "未知动作：${ACTION}"; usage; exit 2 ;;
    esac
}

# 检查前置：补丁是否在位
check_patch_applied() {
    hdr "检查前置（01 补丁是否在位）"

    if [ ! -f "${KO}" ]; then
        err "找不到 ${KO}"
        return 1
    fi

    local state
    state="$(python3 "${PATCH_PY}" check "$(spec_gen2_for_check)" "$0" 2>&1 \
             | grep -oE '未打补丁|已打补丁' | head -1)"

    case "${state}" in
        已打补丁)
            ok "01 补丁在位" ;;
        未打补丁)
            err "01 补丁不在 —— 那推上去也会被驱动降回来"
            echo "     先跑: sudo bash 01-gen2-patch.sh apply"
            echo "     然后重载模块或重启，再回来跑本脚本"
            return 1 ;;
        *)
            warn "无法判定补丁状态，继续但可能无效" ;;
    esac
    return 0
}

# 给 _patch.py check 用的最小 spec（只查状态，不做别的）
spec_gen2_for_check() {
    cat <<EOF
{
  "name": "gen2",
  "desc": "状态检查",
  "path": "${KO}",
  "anchor": {"mode": "sig", "sig": "${GEN2_SIG_HEX}"},
  "edits": [
    {"off": $(( GEN2_OFF_LEA )), "old": "${GEN2_ORIG_LEA}", "new": "${GEN2_NEW_LEA}", "what": ""},
    {"off": $(( GEN2_OFF_CMP )), "old": "${GEN2_ORIG_CMP}", "new": "${GEN2_NEW_CMP}", "what": ""}
  ]
}
EOF
}

# ---------- status ----------
do_status() {
    hdr "模块状态"

    if kmod_loaded; then
        ok "gen2kr 已加载"
        local p
        for p in guard_period_ms guard_rounds guard_enabled; do
            printf '     %-16s = %s\n' "$p" \
                "$(cat "/sys/module/gen2kr/parameters/${p}" 2>/dev/null || echo N/A)"
        done
    else
        echo "     gen2kr 未加载"
    fi

    hdr "模块产物"
    if kmod_built; then
        ok "$(kmod_path)  ($(stat -c%s "$(kmod_path)") 字节)"
    else
        warn "未编译（跑 start 会自动编，或单独跑 build）"
    fi

    hdr "链路"
    echo "     $(read_regs)"
    if is_gen2; then
        ok "当前 Gen2（5.0GT/s）"
    else
        echo "     当前 Gen$(cur_speed)（不是 Gen2）"
    fi
}

# ---------- build ----------
do_build() {
    build_kmod || return 1
    ok "产物: $(kmod_path)"
}

# ---------- stop ----------
do_stop() {
    hdr "卸载模块"

    if ! kmod_loaded; then
        echo "     gen2kr 未加载（无需操作）"
        return 0
    fi

    if rmmod gen2kr; then
        ok "已卸载"
        echo "     注意：守卫停止后，驱动会在下次 cuInit 时把链路降回 Gen1"
    else
        err "rmmod 失败"
        echo "     可能仍被占用，检查: lsmod | grep gen2kr"
        return 1
    fi
}

# ---------- start ----------
do_start() {
    # 1) 补丁必须在位
    check_patch_applied || return "${EXIT_PRECHECK}"

    # 2) 模块必须已编译
    hdr "准备模块"
    if kmod_built; then
        ok "已有 $(basename "$(kmod_path)")"
    else
        echo "     未编译，现在编..."
        build_kmod || return "${EXIT_PRECHECK}"
    fi

    # 3) ★ 真正动硬件前，确认 GPU 空闲
    if ! check_gpu_idle; then
        return "${EXIT_PRECHECK}"
    fi

    # 4) 装载模块（先在位就先卸，清掉 triggered 标志）
    hdr "加载模块"
    if kmod_loaded; then
        echo "     已在内存里，先卸掉以清 triggered 标志"
        rmmod gen2kr 2>/dev/null || true
        sleep 1
    fi
    insmod "$(kmod_path)" || { err "insmod 失败"; return "${EXIT_PRECHECK}"; }
    ok "已加载"
    sleep 1

    # 5) 触发一次 probe
    hdr "触发 probe（kretprobe 在这里命中）"
    echo "   起点: $(read_regs)"

    echo "   unbind ..."
    echo "${DEV_BDF}" > /sys/bus/pci/drivers/nvidia/unbind \
        || { err "unbind 失败"; return "${EXIT_PRECHECK}"; }
    sleep 1

    echo "   bind   ..."
    echo "${DEV_BDF}" > /sys/bus/pci/drivers/nvidia/bind \
        || { err "bind 失败"; return "${EXIT_PRECHECK}"; }

    # 6) 等结果
    hdr "等链路升到 Gen2（最多 ${WAIT_SECONDS}s）"
    if wait_gen2 "${WAIT_SECONDS}"; then
        ok "链路 = Gen2    $(read_regs)"
        echo
        echo "   验证带宽: ${PKG_ROOT}/build/bwtest2 512 15 | grep pinned   （应 ~1.56/1.71）"
        return 0
    fi

    warn "未升上去（$(read_regs)）"
    echo "   gen2kr 日志："
    dmesg | grep -i gen2kr | tail -10 | sed 's/^/     /'
    echo
    echo "   排查见 ${PKG_ROOT}/docs/故障排查.md"
    echo "   若 CAP2 恒 0x0002 + TLS 恒 1 + 连续失败 ⇒ 整机重启，不要再试"
    return "${EXIT_PUSH_FAIL}"
}

# ---------- uninstall ----------
do_uninstall() {
    do_stop || return 1

    hdr "删除编译产物"
    rm -f "${KERNEL_SRC_DIR}"/gen2kr.o \
          "${KERNEL_SRC_DIR}"/gen2kr.ko \
          "${KERNEL_SRC_DIR}"/gen2kr.mod \
          "${KERNEL_SRC_DIR}"/gen2kr.mod.c \
          "${KERNEL_SRC_DIR}"/.gen2kr.o.d \
          "${KERNEL_SRC_DIR}"/Module.symvers \
          "${KERNEL_SRC_DIR}"/modules.order 2>/dev/null || true
    ok "已清理 $(basename "${KERNEL_SRC_DIR}")/ 下的编译产物"
    echo "     模块源码保留。cfg 文件（如有）可用 make clean 一并清理。"
}

# ============================================================
# 3. main
# ============================================================
main() {
    validate

    case "${ACTION}" in
        status)
            section "02 · 链路推送 — 状态"
            load_offsets
            do_status
            ;;

        build)
            section "02 · 只编译模块"
            load_offsets
            # ★ 不要求 root —— 编译本来就该用普通用户
            #   若用 sudo 跑，build_kmod 会降权到 SUDO_USER
            do_build || exit 1
            ;;

        stop)
            section "02 · 卸载模块"
            load_offsets
            need_root stop
            do_stop || exit 1
            ;;

        start)
            section "02 · 加载模块并推送链路"
            load_offsets
            need_root start
            do_start
            exit $?
            ;;

        restart)
            section "02 · 重启推送（stop + start）"
            load_offsets
            need_root restart
            do_stop || exit 1
            sleep 1
            do_start
            exit $?
            ;;

        uninstall)
            section "02 · 彻底卸载（stop + 清理产物）"
            load_offsets
            need_root uninstall
            do_uninstall || exit 1
            ;;
    esac
}

main "$@"
