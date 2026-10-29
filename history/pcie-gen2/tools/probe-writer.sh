#!/bin/bash
# probe-writer.sh —— 用「执行断点 + 抓寄存器」核实 BAR0 写封装的实参
#
# 为什么重做这件事（reports/195）
#   reports/191 曾用同样方法测 `_nv053912rm`，结论是"CX 里 0x8C040 出现 0 次"，
#   并据此推翻"它是 BAR0 写函数"。但本次发现两个问题：
#     1. `perf script` **默认输出不含寄存器**，必须显式 `-F comm,iregs`
#        —— 若不加，grep CX 永远 0 次，会得出"从未出现"的假结论。
#     2. 地址若用物理 BAR0（而非 ioremap 后的虚拟地址），数据断点也抓不到。
#   本脚本用**执行断点 + -F iregs**，只依赖函数入口地址，不依赖 BAR0 映射，
#   因此是干净的重测。
#
# 用法：bash probe-writer.sh [函数名] [MB]
set -u
FN="${1:-_nv053912rm}"
MB="${2:-32}"
BW=/data/labs/nvidia/pcie-gen2/bwtest2

declare -A OFF=(
  [_nv053912rm]=0x5ff6c0    # 带重试循环的 BAR0 写封装
  [_nv053907rm]=0x5ffda0    # 虚分派读封装
  [_nv017281rm]=0x1269f0    # 锁定环所在函数
  [lock]=0x127000           # or $0x80000（写 SPEED=2）
)
O=${OFF[$FN]:-}
if [ -z "$O" ]; then
  case "$FN" in 0x*) O=$FN ;; *) echo "未知函数 $FN"; exit 2 ;; esac
fi

BASE=$(cat /sys/module/nvidia/sections/.text)
ADDR=$(python3 -c "print('0x%x' % (int('$BASE',16) + $O))")
OUT=/tmp/writer-$FN.data
rm -f "$OUT"

echo "=== $FN @ $ADDR  (bwtest2 ${MB}MB) ==="
perf record -e "mem:$ADDR:x" -a --intr-regs=ip,cx,si,dx,r8 -o "$OUT" -- "$BW" "$MB" 1 2>&1 | tail -2

TOTAL=$(perf script -i "$OUT" 2>/dev/null | grep -c . || true)
echo "总命中: $TOTAL"
[ "$TOTAL" -eq 0 ] && { echo "★ 未命中，检查地址"; exit 0; }

echo
echo "=== ★ CX == 0x8c040 的次数 ==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | grep -ci 'CX:0x8c040' || true

echo
echo "=== CX 高频值 Top 20（BAR0 偏移应是小数值）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null \
  | grep -oE 'CX:0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -20

echo
echo "=== SI 高频值 Top 10（若签名是 (obj, esi=?, ecx=offset)…）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null \
  | grep -oE 'SI:0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -10

echo
echo "=== 前 10 条原始 ==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | head -10
