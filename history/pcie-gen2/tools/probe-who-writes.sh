#!/bin/bash
# probe-who-writes.sh —— 找出「新建 CUDA 上下文时把 SPEED 写成 2」的真凶
#
# 背景（reports/195 实测）
#   补丁把 `_nv017281rm` 的 `or eax,0x80000`（SPEED=2）改成 `0x40000`（SPEED=1），
#   并已验证写入值从 0x20085800 → 0x20045800。
#   但推上 Gen2 后跑 3 轮 minictx，链路仍掉回 Gen1、0x8C040 变回 0x20585800（SPEED=2）。
#   ⇒ **另有写入者**。静态分析给出 11 个访问 0x8C040 的函数，其中 5 个是"读-改-写"。
#
# 做法
#   对 `_nv053912rm`（BAR0 写封装）打 perf 执行断点，带 `-g` 抓调用栈，
#   跑 minictx。凡是 CX==0x8c040 的样本，其调用栈就指向真正的写入者。
#
#   ★ x86 只有 4 个调试寄存器，执行断点最多同时 4 个，所以用调用栈比逐个试更省。
#
# 用法：bash probe-who-writes.sh [轮数]
set -u
N="${1:-3}"
BW=/data/labs/nvidia/pcie-gen2/minictx
OFF=0x5ff6c0          # _nv053912rm

BASE=$(cat /sys/module/nvidia/sections/.text)
ADDR=$(python3 -c "print('0x%x' % (int('$BASE',16) + $OFF))")
OUT=/tmp/who-writes.data
rm -f "$OUT"

echo "=== 写封装 _nv053912rm @ $ADDR ；跑 minictx $N 轮 ==="
echo -n "起点链路: "; lspci -vv -s 01:00.0 2>/dev/null | grep -o 'LnkSta:.*' | head -1

perf record -e "mem:$ADDR:x" -a -g --intr-regs=ip,cx,si,dx,r8 -o "$OUT" -- "$BW" "$N" 2>&1 | tail -2

echo
echo "总命中: $(perf script -i "$OUT" 2>/dev/null | grep -c . || true)"
echo
echo "=== ★ CX==0x8c040 的样本数 ==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | grep -ci 'CX:0x8c040' || true

echo
echo "=== 这些样本的完整记录（含 R8=写入值）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | grep -i 'CX:0x8c040'

echo
echo "=== 这些样本的调用栈 ==="
perf script -i "$OUT" --call-trace 2>/dev/null | awk '
  /CX:0x8c040/ {p=1; print "\n--- 样本 ---"; print; next}
  p && /^\s+[0-9a-f]+\s/ {print; next}
  p && /^$/ {p=0}
  p {print}
' | head -60

echo
echo "=== 全部样本的调用栈（按首帧聚合，找不同写入路径）==="
perf script -i "$OUT" -F callchain 2>/dev/null | head -40

echo
echo -n "结束链路: "; lspci -vv -s 01:00.0 2>/dev/null | grep -o 'LnkSta:.*' | head -1
