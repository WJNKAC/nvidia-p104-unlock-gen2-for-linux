#!/bin/bash
# probe-run.sh —— 用 perf 硬件执行断点统计 `_nv017281rm` 执行次数
#
# 为什么用 perf 硬件断点（reports/187 / 191）
#   `_nv017281rm` / `_nv053907rm` / `_nv053912rm` 都是 **notrace**：
#   bpftrace 拒绝、available_filter_functions 里没有、perf probe 也会失败。
#   而 `register_kprobe` 不检查 notrace 属性 —— 硬挂会 int3 破坏执行，
#   曾造成 9.26 亿次调用 / load 5.95 的灾难。
#   ⇒ 唯一安全手段是 perf 的硬件断点（x86 debug register，不动指令字节）。
#
# 用法：bash probe-run.sh <场景名> <秒数> [函数名]
#   场景名只用于打标签；函数名默认 _nv017281rm
set -u

SCEN="${1:-idle}"
DUR="${2:-20}"
FN="${3:-_nv017281rm}"

declare -A OFF=(
  [_nv017281rm]=0x1269f0
  [_nv017282rm]=0x12d2f0
  [_nv005948rm]=0xb3a240
  [_nv053907rm]=0x5ffda0
  [_nv053912rm]=0x5ff6c0
)
O=${OFF[$FN]:-}
if [ -z "$O" ]; then echo "未知函数 $FN"; exit 2; fi

BASE=$(cat /sys/module/nvidia/sections/.text)
ADDR=$(printf '0x%x' $(( BASE + O )))
OUT=/tmp/probe-$SCEN.data
echo "=== 场景 [$SCEN]  函数 $FN @ $ADDR  时长 ${DUR}s ==="

rm -f "$OUT"
# --intr-regs 抓寄存器：ip 证明命中点，cx/r8 是 BAR0 偏移/值（reports/191 的用法）
perf record -e "mem:$ADDR:x" -a --intr-regs=ip,sp,cx,dx,r8 -o "$OUT" -- sleep "$DUR" 2>/tmp/perf-$SCEN.err
RC=$?

echo "perf 退出码 $RC"
if [ $RC -ne 0 ]; then
  echo "--- stderr ---"; cat /tmp/perf-$SCEN.err
  exit $RC
fi

# 统计
TOTAL=$(perf script -i "$OUT" 2>/dev/null | grep -c . || true)
echo "采样行数（≈执行次数）: $TOTAL"
if [ "$TOTAL" -gt 0 ]; then
  echo "--- 前 15 条 ---"
  perf script -i "$OUT" 2>/dev/null | head -15
  echo "--- 按进程聚合 ---"
  perf script -i "$OUT" 2>/dev/null | awk '{print $1}' | sort | uniq -c | sort -rn | head -10
else
  echo "★ 该函数在 ${DUR}s 内【一次都没执行】"
fi
