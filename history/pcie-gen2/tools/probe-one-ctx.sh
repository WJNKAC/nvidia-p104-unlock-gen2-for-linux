#!/bin/bash
# probe-one-ctx.sh —— 单轮 CUDA 上下文：抓 0x8C040 的每一次写入（含写入值与写入点）
#
# 背景（reports/195）
#   从 Gen2 起点高频采样，看到 0x8C040 与 LNKSTA 在同一瞬间（0.090s）一起变：
#       0x20545800(SPEED=1) → 0x20585800(SPEED=2)
#       0x1042(sp=2)        → 0x1041(sp=1)
#   ⇒ 是软件改写 SPEED 导致降级。但多次测试中 CX==0x8c040 只抓到 1 次，
#     且值是 SPEED=1（补丁后）。所以要么有第二个写路径，要么样本丢失。
#
#   本脚本把「上下文轮数压到 1」以把样本丢失降到最低，
#   并把 0x8C040 与 0x8C0xx 的所有写入连值一起列出。
#
# 用法：bash probe-one-ctx.sh [minictx|bwtest2] [轮数]
set -u
MODE="${1:-minictx}"
N="${2:-1}"
D=/data/labs/nvidia/pcie-gen2

if [ "$MODE" = "bwtest2" ]; then CMD=("$D/bwtest2" 64 "$N"); else CMD=("$D/minictx" "$N"); fi

read_regs() {
python3 - <<'PY'
import mmap,os,struct
p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
v=struct.unpack_from('<I',m,0x8C040)[0]
l=struct.unpack_from('<I',m,0x88088)[0]
c=struct.unpack_from('<I',m,0x880A4)[0]
print(f"0x8C040=0x{v:08x} SPEED={(v>>18)&3}   LNKSTA[3:0]={(l>>16)&0xF}   LCAP2=0x{c:04x}")
PY
}

BASE=$(cat /sys/module/nvidia/sections/.text)
ADDR=$(python3 -c "print('0x%x' % (int('$BASE',16) + 0x5ff6c0))")   # _nv053912rm 写封装

echo "=== 起点 ==="; read_regs
echo "=== 写封装 _nv053912rm @ $ADDR；跑 $MODE $N 轮 ==="

OUT=/tmp/one-ctx.data
rm -f "$OUT"
perf record -e "mem:$ADDR:x" -a --intr-regs=ip,cx,dx,r8 -o "$OUT" -- "${CMD[@]}" 2>&1 | tail -2

echo
echo "=== 所有 CX==0x8c040 的写入（R8 = 写入值）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | grep -i 'CX:0x8c040' || echo "  （无）"

echo
echo "=== 所有 0x8C0xx 偏移的出现次数（看还有谁在这片区域活动）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null \
  | grep -oE 'CX:0x8c[0-9a-f]{3}' | sort | uniq -c | sort -rn | head -15

echo
echo "=== 总命中 ==="
perf script -i "$OUT" 2>/dev/null | grep -c . || true

echo
echo "=== 终点 ==="; read_regs
