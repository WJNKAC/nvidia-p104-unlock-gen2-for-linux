#!/bin/bash
# 10-check.sh —— 环境自检（★ 纯只读，不改动任何东西）
#
# 在动手之前跑这个，确认：
#   设备在、驱动版本对、补丁工具能定位、探针能跑、寄存器状态是什么
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

echo "======================================================"
echo " NVIDIA P104-100 PCIe Gen2 解锁 —— 环境自检"
echo "======================================================"

hdr "1. 系统"
echo "  内核      : $KVER"
echo "  发行版    : $(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d'"' -f2)"
echo "  当前用户  : $(id -un) (uid $(id -u))"

hdr "2. nvidia 驱动"
if [ -f "$KO" ]; then
    ok "$KO"
    echo "     md5 = $(md5sum "$KO" | cut -d' ' -f1)"
    echo "     大小 = $(stat -c%s "$KO")"
else
    err "找不到 $KO"
    echo "     期望路径：/lib/modules/$KVER/updates/dkms/nvidia.ko"
    echo "     若驱动装在别处，请修改 bin/_common.sh 里的 KO"
fi
if [ -r /proc/driver/nvidia/version ]; then
    echo "     $(head -1 /proc/driver/nvidia/version)"
fi
lsmod | grep -E '^nvidia' | sed 's/^/     已加载: /' || echo "     nvidia 未加载"

hdr "3. 设备"
if lspci -Dn 2>/dev/null | grep -q '10de:1b87'; then
    ok "P104-100 (10de:1b87) 在总线"
    lspci -vv -s "$DEV_SHORT" 2>/dev/null | grep -o 'LnkSta:.*' | sed 's/^/     /'
    echo "     cfg = $(setpci -s "$DEV_SHORT" 0.l 2>&1)"
else
    err "找不到 P104-100"
    lspci -Dn | grep -i 10de | sed 's/^/     /' || echo "     （无 NVIDIA 设备）"
fi

hdr "4. 寄存器（唯一可信判据）"
echo "     $(read_regs)"
echo "     ↑ LNKSTA=2 才算 Gen2；CAP2=0x0006 才算声明已解锁"
if alive; then ok "MMIO 应答正常"; else err "MMIO 连续 20 次 0xffffffff —— 设备可能 ZOMBIE"; fi

hdr "5. 补丁工具能否定位目标"
if [ -f "$KO" ]; then
    python3 "$PKG_ROOT/bin/patch_no_gen1.py" check 2>&1 | sed 's/^/     /'
fi

hdr "6. 探针"
if ensure_build check >/dev/null 2>&1; then ok "build/ 探针已就绪"; else warn "build/ 探针缺失 → 先跑 bin/build.sh"; fi
[ -f "$BUILD_DIR/gen2kr.ko" ] && ok "build/gen2kr.ko 已就绪" || warn "build/gen2kr.ko 缺失 → 先跑 bin/build.sh"
[ -f "$KMOD_DIR/gen2kr.ko" ] && ok "kmod/gen2kr.ko 已就绪" || true

hdr "7. 当前 gen2kr 状态"
if lsmod | grep -q '^gen2kr'; then
    warn "gen2kr 已加载（守卫在运行）"
    for p in guard_period_ms debounce_cnt retrain_min_ms speed_settle_ms stage_gap_ms; do
        printf '     %-16s = %s\n' "$p" "$(cat /sys/module/gen2kr/parameters/$p 2>/dev/null || echo N/A)"
    done
else
    echo "     gen2kr 未加载"
fi

hdr "8. 干扰源检查"
if pgrep -x nvitop >/dev/null 2>&1; then
    err "检测到 nvitop —— 它曾用 Xid 79 打死过设备，且占 /dev/nvidia* 使 rmmod EBUSY"
else
    ok "无 nvitop"
fi
pgrep -x Xorg >/dev/null 2>&1 && warn "Xorg 在运行（会持有 /dev/nvidia*，rmmod 可能 EBUSY）" || true

echo
echo "======================================================"
S="$(cur_speed)"
C="$(cap2)"
if [ "$S" = "2" ]; then
    echo " 当前状态：${C_G}链路已是 Gen2${C_0}"
else
    echo " 当前状态：链路 Gen$([ "$S" = "1" ] && echo 1 || echo "?")，需要解锁"
fi
echo " 下一步  ：sudo bash bin/20-install.sh --dry-run   先看计划"
echo "======================================================"
