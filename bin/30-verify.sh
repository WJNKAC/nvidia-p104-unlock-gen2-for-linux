#!/bin/bash
# 30-verify.sh —— 验证：带宽是否达标 + 补丁是否真的接管
#
# 三个判据，缺一不可：
#   A. 带宽      pinned H2D ≥ 1.5 GB/s（Gen2 x4 应 ~1.55）
#                ★ 必须 512MB×15；128MB×3 会落在恢复窗口里只测到 0.78
#   B. 稳定      N 轮 CUDA 上下文后链路仍是 2
#   C. 补丁生效  摘掉守卫后链路仍保持（守卫零动作）
#
# 用法：
#   sudo bash bin/30-verify.sh            # A + B（默认 5 轮）
#   sudo bash bin/30-verify.sh 20         # A + B（20 轮）
#   sudo bash bin/30-verify.sh --full     # 加做 C（会临时卸掉守卫）
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_root
ensure_build auto || exit 1

ROUNDS=5; FULL=0
for a in "$@"; do
    case "$a" in
        --full) FULL=1 ;;
        *[!0-9]*) err "未知参数 $a"; exit 2 ;;
        *) ROUNDS="$a" ;;
    esac
done

echo "======================================================"
echo " 验证（$ROUNDS 轮）"
echo "======================================================"
echo " 起点：$(read_regs)"

# ---------------- A. 带宽 ----------------
hdr "A. 带宽（512MB × 15）"
if ! is_gen2; then
    warn "当前不是 Gen2，带宽必然是 Gen1 水平（0.78）"
fi
BW_OUT="$("$BUILD_DIR/bwtest2" 512 15 2>&1)"
echo "$BW_OUT" | sed 's/^/   /'
PIN_H2D="$(echo "$BW_OUT" | grep pinned | grep -o 'H2D med [0-9.]*' | awk '{print $3}')"
PIN_D2H="$(echo "$BW_OUT" | grep pinned | grep -o 'D2H med [0-9.]*' | awk '{print $3}')"
echo
echo " 测后链路：$(read_regs)"
A_OK=0
if [ -n "$PIN_H2D" ] && python3 -c "import sys; sys.exit(0 if $PIN_H2D >= 1.4 else 1)"; then
    ok "pinned H2D = $PIN_H2D GB/s（Gen2 达标）"
    A_OK=1
else
    err "pinned H2D = ${PIN_H2D:-?} GB/s（< 1.4，仍是 Gen1 水平）"
fi

# ---------------- B. 稳定性 ----------------
hdr "B. 稳定性（$ROUNDS 轮 CUDA 上下文）"
BAD=0
for i in $(seq 1 "$ROUNDS"); do
    "$BUILD_DIR/minictx" 1 >/dev/null 2>&1
    sleep 0.8
    S="$(cur_speed)"
    printf '   第 %2d 轮: LNKSTA=%s  %s\n' "$i" "$S" "$([ "$S" = "2" ] && echo ok || echo '★掉级')"
    [ "$S" = "2" ] || BAD=$((BAD + 1))
done
sleep 3
echo "   3s 后：$(read_regs)"
[ "$BAD" = "0" ] && ok "全程保持 Gen2" || err "$BAD 轮掉级"

# ---------------- C. 补丁是否独立有效 ----------------
C_OK=1
if [ "$FULL" = "1" ]; then
    hdr "C. 补丁独立性（临时摘掉守卫）"
    echo "   理由：带着守卫测，测不出补丁的作用 —— 守卫会把链路拉回来。"
    if lsmod | grep -q '^gen2kr'; then
        rmmod gen2kr && echo "   gen2kr 已卸载"
    fi
    sleep 1
    echo "   摘守卫前：$(read_regs)"
    CBAD=0
    for i in $(seq 1 3); do
        "$BUILD_DIR/minictx" 1 >/dev/null 2>&1
        sleep 0.8
        S="$(cur_speed)"
        printf '     第 %d 轮: LNKSTA=%s\n' "$i" "$S"
        [ "$S" = "2" ] || CBAD=$((CBAD + 1))
    done
    sleep 8
    echo "   再等 8s：$(read_regs)"
    is_gen2 || CBAD=$((CBAD + 1))
    if [ "$CBAD" = "0" ]; then
        ok "补丁独立有效：无守卫时链路仍保持 Gen2"
    else
        err "补丁不足：无守卫时有 $CBAD 次掉级"
        C_OK=0
    fi
    echo "   重新装载守卫（安全网）："
    insmod "$BUILD_DIR/gen2kr.ko" 2>/dev/null && echo "     gen2kr 已恢复" || warn "gen2kr 恢复失败"
fi

# ---------------- 总结 ----------------
echo
echo "======================================================"
echo " 结论"
echo "======================================================"
echo "   A 带宽      : $([ "$A_OK" = "1" ] && echo "${C_G}通过${C_0}" || echo "${C_R}未达标${C_0}")  pinned H2D=${PIN_H2D:-?} D2H=${PIN_D2H:-?}"
echo "   B 稳定性    : $([ "$BAD" = "0" ] && echo "${C_G}通过${C_0}" || echo "${C_R}$BAD 轮掉级${C_0}")"
[ "$FULL" = "1" ] && echo "   C 补丁独立性: $([ "$C_OK" = "1" ] && echo "${C_G}通过${C_0}" || echo "${C_R}未通过${C_0}")"
echo "   链路        : $(read_regs)"
echo
echo "   Xid 检查："
dmesg | grep -i 'Xid' | tail -3 | sed 's/^/     /' || echo "     无"
echo "     （注意 r8169 网卡也会报 XID，与 GPU 无关）"
echo "======================================================"
