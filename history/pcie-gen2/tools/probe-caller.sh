#!/bin/bash
# probe-caller.sh —— 抓「谁把 mode=1 传给 set_link_speed」
#
# ## 已确证（reports/198）
#   perf 硬件执行断点在 `_nv017105rm`（驱动自己的 set_link_speed）上，
#   每次新建 CUDA 上下文都命中，且 **DX = 0x1**（4/4）。
#   mode 1 → 写 `or $0x80000` → SPEED=2 → 2.5GT/s = **Gen1**。
#
#   ⇒ 驱动不是"照抄当前速率"，而是**在每次建上下文时显式要求 Gen1**。
#
# ## 本脚本要回答
#   谁调用它？调用点前的常量是 1 还是从能力位算出来的？
#   · 若调用点是硬编码 `mov $0x1,%edx` → 一个字节补丁就能改成 2，
#     驱动器会原生请求 Gen2，RM 内部也就一致了（远优于 kretprobe 硬推）
#   · 若 mode 由 obj[0x954] 等能力位算出来 → 要改的是能力位来源
#
# ## 做法
#   perf record 带 `-g`（抓调用栈）。因为 `_nv017105rm` 是经对象槽间接调用的，
#   光看 IP 只知道"进了 setter"，必须靠调用栈才知道来源。
#
# 用法：bash probe-caller.sh [秒数]
set -u
D=/data/labs/nvidia/pcie-gen2
OFF=0x127d60          # _nv017105rm

BASE=$(cat /sys/module/nvidia/sections/.text)
ADDR=$(python3 -c "print('0x%x' % (int('$BASE',16) + $OFF))")
OUT=/tmp/caller.data
rm -f "$OUT"

echo "=== set_link_speed @ $ADDR ；只跑 minictx（不碰驱动）==="
perf record -e "mem:$ADDR:x" -a -g --intr-regs=ip,dx,si,di -o "$OUT" -- \
  "$D/minictx" 1 2>&1 | tail -2

echo
echo "=== DX 值 ==="
perf script -i "$OUT" -F iregs 2>/dev/null | grep -oE 'DX:0x[0-9a-f]+' | sort | uniq -c

echo
echo "=== 调用栈（含调用者地址）==="
perf script -i "$OUT" --call-trace 2>/dev/null | head -40

echo
echo "=== 用 -g 解析（符号化调用链）==="
perf script -i "$OUT" -F comm,ip,callchain 2>/dev/null | head -30

echo
echo "=== 原始完整记录 ==="
perf script -i "$OUT" 2>/dev/null | head -40
