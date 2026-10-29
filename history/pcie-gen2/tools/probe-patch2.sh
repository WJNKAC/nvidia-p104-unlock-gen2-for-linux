#!/bin/bash
# probe-patch2.sh —— 定位 `_nv017105rm` 里真正的 SPEED=2 写入点
#
# 背景（reports/195）
#   已实测：补丁 `_nv017281rm` 后，从 Gen2 跑 minictx，链路仍掉 Gen1，
#   且采样看到 0x8C040 与 LNKSTA 在同一瞬间（0.090s）一起变化
#      0x20545800(SPEED=1) → 0x20585800(SPEED=2)
#   ⇒ 有第二个写入者。执行断点定位到 `_nv017105rm`（其余 3 个候选 0 次）。
#
#   该函数内有两处 `or $0x80000`（SPEED=2）：
#      0x127e65  or $0x80000,%eax     （前有 cmp $0x2 → je 12819b）
#      0x12803b  or $0x80000,%r8d     （前有 cmp $0x3 → je 128144）
#   本脚本对这两条**指令本身**打执行断点，跑 minictx，看实际走哪条。
#
# ★ 只有从 Gen2 掉到 Gen1 的那次执行才是真凶；Gen1 状态下执行只是"照抄"。
#
# 用法：bash probe-patch2.sh
set -u
D=/data/labs/nvidia/pcie-gen2
DEV=0000:01:00.0
OFF_EAX=0x127e65
OFF_R8D=0x12803b

BASE=$(cat /sys/module/nvidia/sections/.text)
A_EAX=$(python3 -c "print('0x%x' % (int('$BASE',16) + $OFF_EAX))")
A_R8D=$(python3 -c "print('0x%x' % (int('$BASE',16) + $OFF_R8D))")

echo "=== 1. 记录起点（★ 不强制要求 Gen2）==="
echo "  说明：在 Gen1 状态下，sp==1 会走 0x127e65 分支，同样执行 or \$0x80000，"
echo "        这本身就是锁定环。定位「哪条 or 被执行」不需要先到 Gen2，"
echo "        因为掉速后 0x8C040.SPEED 已被改成 2，重训目标仍是 2.5GT，反而推不上去。"
echo -n "  起点链路: "; lspci -vv -s $DEV 2>/dev/null | grep -o 'LnkSta:.*' | head -1
SPD=$(python3 - <<'PY'
import mmap,os,struct
b=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=b)
v=struct.unpack_from('<I',m,0x8C040)[0]
print(f"0x{v:08x} SPEED={(v>>18)&3}")
PY
)
echo "  0x8C040 = $SPD"

echo
echo "=== 2. 打点（两条 or \$0x80000）==="
echo "  0x127e65 or eax,0x80000  → $A_EAX"
echo "  0x12803b or r8d,0x80000  → $A_R8D"

OUT=/tmp/p2.data
rm -f "$OUT"
echo
echo "=== 3. 跑 minictx 1 轮 ==="
perf record -e "mem:$A_EAX:x" -e "mem:$A_R8D:x" -a -o "$OUT" -- "$D/minictx" 1 2>&1 | tail -2

echo
echo "=== 4. 命中统计 ==="
echo -n "  0x127e65 (eax) : "; perf script -i "$OUT" 2>/dev/null | grep -c "mem:$A_EAX:x" || true
echo -n "  0x12803b (r8d) : "; perf script -i "$OUT" 2>/dev/null | grep -c "mem:$A_R8D:x" || true
echo
echo "=== 5. 明细 ==="
perf script -i "$OUT" 2>/dev/null | head -10
echo
echo -n "=== 终点链路: "; lspci -vv -s $DEV 2>/dev/null | grep -o 'LnkSta:.*' | head -1
