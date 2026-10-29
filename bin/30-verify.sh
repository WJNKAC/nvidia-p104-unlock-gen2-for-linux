#!/bin/bash
# ============================================================
# 30-verify.sh —— 验证：带宽是否达标 + 补丁是否真的接管
#
# 三个判据，缺一不可：
#   A. 带宽      pinned H2D ≥ 1.4 GB/s（Gen2 x4 应 ~1.55）
#                ★ 必须 512MB×15；128MB×3 会落在恢复窗口里只测到 0.78
#   B. 稳定      N 轮 CUDA 上下文后链路仍是 2
#   C. 补丁生效  摘掉守卫后链路仍保持（守卫零动作）
#
# 用法：
#   sudo bash bin/30-verify.sh            # A + B（默认 5 轮）
#   sudo bash bin/30-verify.sh 20         # A + B（20 轮）
#   sudo bash bin/30-verify.sh --full     # 加做 C（会临时卸掉守卫）
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

ROUNDS=5                # B 阶段的轮数
FULL=0                  # 1 = 加做 C 阶段（摘守卫）
BW_SIZE_MB=512          # 带宽测试：单次大小
BW_ITERS=15             # 带宽测试：次数（★ 不能用小值，见文件头）
BW_THRESHOLD=1.4        # A 阶段的门槛（GB/s）

# 各阶段结果
A_OK=0
B_BAD=0
C_OK=1
BW_H2D=""               # 实测 pinned H2D（GB/s）
BW_D2H=""               # 实测 pinned D2H（GB/s）

# ============================================================
# 2. 函数
# ============================================================

# 把 1/0 转成带颜色的文字
status_text() {
    if [ "$1" = "1" ]; then echo "$2"; else echo "$3"; fi
}

# 解析参数：--full 或纯数字轮数
parse_args() {
    local a
    for a in "$@"; do
        case "${a}" in
            --full)
                FULL=1 ;;
            *[!0-9]*)
                err "未知参数 ${a}"
                echo "   用法: sudo bash bin/30-verify.sh [轮数] [--full]"
                exit 2 ;;
            *)
                ROUNDS="${a}" ;;
        esac
    done
}

# A. 带宽测试
check_bandwidth() {
    local out h2d

    hdr "A. 带宽（${BW_SIZE_MB}MB × ${BW_ITERS}）"

    if ! is_gen2; then
        warn "当前不是 Gen2，带宽必然是 Gen1 水平（0.78）"
    fi

    out="$("${BUILD_DIR}/bwtest2" "${BW_SIZE_MB}" "${BW_ITERS}" 2>&1)"
    echo "${out}" | sed 's/^/   /'

    h2d="$(echo "${out}" | grep pinned | grep -o 'H2D med [0-9.]*' | awk '{print $3}')"
    BW_H2D="${h2d}"
    BW_D2H="$(echo "${out}" | grep pinned | grep -o 'D2H med [0-9.]*' | awk '{print $3}')"

    echo
    echo " 测后链路：$(read_regs)"

    if [ -n "${h2d}" ] && python3 -c "import sys; sys.exit(0 if ${h2d} >= ${BW_THRESHOLD} else 1)"; then
        ok "pinned H2D = ${h2d} GB/s（Gen2 达标）"
        A_OK=1
    else
        err "pinned H2D = ${h2d:-?} GB/s（< ${BW_THRESHOLD}，仍是 Gen1 水平）"
        A_OK=0
    fi
}

# B. 稳定性：连续建 CUDA 上下文，看链路掉不掉
check_stability() {
    local i speed

    hdr "B. 稳定性（${ROUNDS} 轮 CUDA 上下文）"

    B_BAD=0
    for i in $(seq 1 "${ROUNDS}"); do
        "${BUILD_DIR}/minictx" 1 >/dev/null 2>&1
        sleep 0.8
        speed="$(cur_speed)"
        printf '   第 %2d 轮: LNKSTA=%s  %s\n' "${i}" "${speed}" \
            "$([ "${speed}" = "2" ] && echo ok || echo '★掉级')"
        [ "${speed}" = "2" ] || B_BAD=$((B_BAD + 1))
    done

    sleep 3
    echo "   3s 后：$(read_regs)"
    if [ "${B_BAD}" = "0" ]; then
        ok "全程保持 Gen2"
    else
        err "${B_BAD} 轮掉级"
    fi
}

# C. 补丁独立性：摘掉守卫再测，看链路是否仍保持
check_patch_independence() {
    local i speed cbad

    hdr "C. 补丁独立性（临时摘掉守卫）"
    echo "   理由：带着守卫测，测不出补丁的作用 —— 守卫会把链路拉回来。"

    if lsmod | grep -q '^gen2kr'; then
        rmmod gen2kr && echo "   gen2kr 已卸载"
    fi
    sleep 1
    echo "   摘守卫前：$(read_regs)"

    cbad=0
    for i in $(seq 1 3); do
        "${BUILD_DIR}/minictx" 1 >/dev/null 2>&1
        sleep 0.8
        speed="$(cur_speed)"
        printf '     第 %d 轮: LNKSTA=%s\n' "${i}" "${speed}"
        [ "${speed}" = "2" ] || cbad=$((cbad + 1))
    done

    sleep 8
    echo "   再等 8s：$(read_regs)"
    is_gen2 || cbad=$((cbad + 1))

    if [ "${cbad}" = "0" ]; then
        ok "补丁独立有效：无守卫时链路仍保持 Gen2"
        C_OK=1
    else
        err "补丁不足：无守卫时有 ${cbad} 次掉级"
        C_OK=0
    fi

    echo "   重新装载守卫（安全网）："
    if insmod "${BUILD_DIR}/gen2kr.ko" 2>/dev/null; then
        echo "     gen2kr 已恢复"
    else
        warn "gen2kr 恢复失败"
    fi
}

# 汇总
show_summary() {
    echo
    echo "======================================================"
    echo " 结论"
    echo "======================================================"
    echo "   A 带宽      : $(status_text "${A_OK}" "${C_G}通过${C_0}" "${C_R}未达标${C_0}")  pinned H2D=${BW_H2D:-?} D2H=${BW_D2H:-?}"
    echo "   B 稳定性    : $(status_text "$([ "${B_BAD}" = "0" ] && echo 1 || echo 0)" "${C_G}通过${C_0}" "${C_R}${B_BAD} 轮掉级${C_0}")"
    if [ "${FULL}" = "1" ]; then
        echo "   C 补丁独立性: $(status_text "${C_OK}" "${C_G}通过${C_0}" "${C_R}未通过${C_0}")"
    fi
    echo "   链路        : $(read_regs)"
    echo
    echo "   Xid 检查："
    dmesg | grep -i 'Xid' | tail -3 | sed 's/^/     /' || echo "     无"
    echo "     （注意 r8169 网卡也会报 XID，与 GPU 无关）"
    echo "======================================================"
}

# ============================================================
# 3. main
# ============================================================
main() {
    parse_args "$@"

    need_root
    ensure_build auto || exit 1

    echo "======================================================"
    echo " 验证（${ROUNDS} 轮）"
    echo "======================================================"
    echo " 起点：$(read_regs)"

    check_bandwidth
    check_stability

    [ "${FULL}" = "1" ] && check_patch_independence

    show_summary
}

main "$@"
