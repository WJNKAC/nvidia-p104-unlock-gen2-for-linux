#!/usr/bin/env bash
# pci-state.sh —— 判定 PCIe 设备当前处于哪一种"读不到"的状态
#
# 背景：三种状态都会表现为「寄存器读 0xffffffff / enable=1 拉不回 D0」，
#       但成因和恢复手段完全不同。见 reports/178。
#
#   A. D3COLD   电源态错，设备没上电   → 能救（先 unbind 再 remove+rescan）
#   B. GONE     链路断了，设备不在总线 → 基本救不回，要重启
#   C. ZOMBIE   枚举还在但配置空间不答 → 救不回，要重启
#
# 用法：sudo ./pci-state.sh [BDF]        默认 0000:01:00.0
set -u
BDF="${1:-0000:01:00.0}"
SHORT="${BDF#0000:}"          # 01:00.0
VENDEV="10de:1b87"            # 按需修改
SYS="/sys/bus/pci/devices/$BDF"

echo "=== 探针对象 $BDF ==="

# ---- 1. lspci 里还在不在（最硬的分界）----
IN_LSPCI=0
if lspci -Dn 2>/dev/null | grep -qi "$VENDEV"; then
    IN_LSPCI=1
fi
echo "1) lspci -Dn 枚举      : $([ $IN_LSPCI = 1 ] && echo '在 ✓' || echo '不在 ✗')"

# ---- 2. sysfs 目录在不在 ----
IN_SYSFS=0
[ -d "$SYS" ] && IN_SYSFS=1
echo "2) sysfs 目录          : $([ $IN_SYSFS = 1 ] && echo '在 ✓' || echo '不在 ✗')"

# ---- 3. power_state 字面值（在 A 里才读得到 D3cold）----
PWR="(无)"
[ $IN_SYSFS = 1 ] && PWR=$(cat "$SYS/power_state" 2>/dev/null || echo "(读不到)")
echo "3) power_state         : $PWR"

# ---- 4. 配置空间 vendor id ----
VID=""
if command -v setpci >/dev/null 2>&1; then
    VID=$(setpci -s "$SHORT" 0.l 2>/dev/null | tr -d ' \n')
fi
if [ -z "$VID" ]; then
    echo "4) 配置空间 0x00       : (setpci 选不到设备)"
elif [ "$VID" = "ffffffff" ]; then
    echo "4) 配置空间 0x00       : ffffffff  ← 不应答"
else
    echo "4) 配置空间 0x00       : $VID  ← 正常应答"
fi

# ---- 5. PCIe capability 链能否解析（区分 A 和 C 的关键）----
CAPOK="(未测)"
if [ -n "$VID" ] && [ "$VID" != "ffffffff" ]; then
    OUT=$(setpci -s "$SHORT" CAP_EXP+0x2c.l 2>&1)
    if echo "$OUT" | grep -qi "not found\|no capabilities"; then
        CAPOK="能力链不可解析（capability not found）"
    elif echo "$OUT" | grep -qi "^[0-9a-f]"; then
        CAPOK="可解析  LNKCAP2=$OUT"
    else
        CAPOK="$OUT"
    fi
fi
echo "5) PCIe 能力链         : $CAPOK"

# ---- 6. 上游根端口链路状态 ----
# 从 BDF 推根端口：读 sysfs 的父设备
RP=""
if [ $IN_SYSFS = 1 ]; then
    RP=$(basename "$(dirname "$(readlink -f "$SYS")")" 2>/dev/null)
fi
# 兜底：本机已知 00:01.0
RPBDF="${RP:-0000:00:01.0}"
RPS=$(setpci -s "${RPBDF#0000:}" CAP_EXP+0x12.w 2>/dev/null | tr -d ' \n')
echo "6) 上游根端口 $RPBDF : LNKSTA=${RPS:-读不到}"

# ---- 7. dmesg 关键字（A 有专属台词）----
DM=$(dmesg 2>/dev/null | grep -i "Unable to change power state from D3cold" | tail -1)
if [ -n "$DM" ]; then
    echo "7) dmesg               : ★ 出现 'Unable to change power state from D3cold to D0'"
else
    echo "7) dmesg               : 无 D3cold 专有关键字"
fi

# ---- 判定 ----
echo
echo "=== 判定 ==="
if [ $IN_LSPCI = 0 ] && [ $IN_SYSFS = 0 ]; then
    echo "★ B. GONE —— 链路断了，设备不在总线"
    echo "  软件救不回。次级总线复位偶尔有效，实测本次无效 → 重启。"
elif [ "$PWR" = "D3cold" ]; then
    echo "★ A. D3COLD —— 电源态错，设备没上电（可救）"
    echo "  正确修法："
    echo "    echo $BDF > /sys/bus/pci/drivers/nvidia/unbind   # 先解绑！"
    echo "    echo 1 > $SYS/remove"
    echo "    echo 1 > /sys/bus/pci/rescan"
    echo "    echo 1 > $SYS/enable ; echo on > $SYS/power/control"
elif [ "$VID" = "ffffffff" ]; then
    echo "★ C. ZOMBIE —— 枚举还在，但配置空间不应答"
    echo "  power_state=$PWR（不是 D3cold，所以不是电源问题）"
    echo "  端点自身死了。软件救不回 → 重启。"
else
    echo "★ OK —— 设备正常应答"
fi
