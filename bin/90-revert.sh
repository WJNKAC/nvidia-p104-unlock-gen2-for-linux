#!/bin/bash
# ============================================================
# 90-revert.sh —— 完全回滚到原始状态
#
# 回滚两件事：
#   ① 卸载 gen2kr（守卫停止）
#   ② 还原 nvidia.ko 到备份（去掉 2 字节补丁）
# 然后重载驱动。
#
# 用法：
#   sudo bash bin/90-revert.sh --dry-run
#   sudo bash bin/90-revert.sh
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

DRY=0                   # 1 = 只打印计划

# nvidia 模块的卸载顺序（依赖关系：drm → modeset → uvm → 主模块）
NVIDIA_MODULES=(nvidia_drm nvidia_modeset nvidia_uvm nvidia)

# ============================================================
# 2. 函数
# ============================================================

# 解析参数
parse_args() {
    case "${1:-}" in
        --dry-run) DRY=1 ;;
        "")        ;;
        *)
            err "未知参数 $1"
            echo "   用法: sudo bash bin/90-revert.sh [--dry-run]"
            exit 2
            ;;
    esac
}

# 第一步：卸载 gen2kr
step_unload_kmod() {
    hdr "1. 卸载 gen2kr"

    if ! lsmod | grep -q '^gen2kr'; then
        echo "   gen2kr 未加载"
        return 0
    fi

    if [ "${DRY}" = "1" ]; then
        echo "   ${C_Y}[dry-run]${C_0} rmmod gen2kr"
        return 0
    fi

    if rmmod gen2kr; then
        ok "已卸载"
    else
        warn "rmmod 失败（可能仍被占用）"
    fi
}

# 第二步：还原 nvidia.ko
step_revert_patch() {
    hdr "2. 还原 nvidia.ko"

    if [ "${DRY}" = "1" ]; then
        python3 "${PKG_ROOT}/bin/patch_no_gen1.py" check 2>&1 | sed 's/^/   /'
        echo "   ${C_Y}[dry-run]${C_0} python3 bin/patch_no_gen1.py revert"
        return 0
    fi

    python3 "${PKG_ROOT}/bin/patch_no_gen1.py" revert \
        || warn "还原失败（可能本来就没打补丁）"
    echo "   当前 md5 = $(md5sum "${KO}" | cut -d' ' -f1)"
}

# 第三步：重载驱动
step_reload() {
    local m

    hdr "3. 重载驱动"

    for m in "${NVIDIA_MODULES[@]}"; do
        lsmod | grep -q "^${m} " || continue
        if [ "${DRY}" = "1" ]; then
            echo "   ${C_Y}[dry-run]${C_0} rmmod ${m}"
        else
            rmmod "${m}" 2>/dev/null || true
        fi
    done
    sleep 1

    if [ "${DRY}" = "1" ]; then
        echo "   ${C_Y}[dry-run]${C_0} modprobe nvidia && modprobe nvidia_uvm"
        return 0
    fi

    if modprobe nvidia && modprobe nvidia_uvm; then
        ok "驱动已重载"
    else
        err "驱动重载失败"
        return 1
    fi
    sleep 2
    echo "   $(read_regs)"
}

# 结束语
show_footer() {
    echo
    echo "======================================================"
    if [ "${DRY}" = "0" ]; then
        echo " 已回滚。链路现在应为 Gen1（$(cur_speed)）—— 这是原厂默认行为。"
        echo " 想重新解锁：sudo bash bin/20-install.sh"
    else
        echo " dry-run 结束，未改动任何东西"
    fi
    echo "======================================================"
}

# ============================================================
# 3. main
# ============================================================
main() {
    parse_args "$@"

    need_root

    echo "======================================================"
    echo " 回滚"
    [ "${DRY}" = "1" ] && echo " ${C_Y}模式：dry-run${C_0}"
    echo "======================================================"

    step_unload_kmod
    step_revert_patch
    step_reload || exit 1

    show_footer
}

main "$@"
