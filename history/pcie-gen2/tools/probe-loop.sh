#!/bin/bash
# probe-loop.sh —— 验证「新建 CUDA 上下文 → 锁定环执行」是否一一对应
#
# 为什么这个实验最关键（reports/195）
#   场景 A 测到：`_nv017281rm` 在 bwtest2 里执行 1 次（此前空闲 20s 是 0 次）。
#   如果"N 次新建上下文 ⇒ N 次执行"，就证明它是 **CUDA 上下文初始化链路**的一部分，
#   而不是"链路异常恢复"路径 —— 这直接推翻 reports/192 的性质判定。
#
# 用法：bash probe-loop.sh [轮数] [MB] [函数名]
set -u

N="${1:-5}"
MB="${2:-64}"
FN="${3:-_nv017281rm}"
BW=/data/labs/nvidia/pcie-gen2/bwtest2

declare -A OFF=([_nv017281rm]=0x1269f0 [_nv017282rm]=0x12d2f0
                [_nv005948rm]=0xb3a240 [_nv053907rm]=0x5ffda0 [_nv053912rm]=0x5ff6c0
                [lock]=0x127000 [lockgen2]=0x126c93 [prechk]=0x126c87)
O=${OFF[$FN]:-}
# 也允许直接给节内偏移（0x...）
if [ -z "$O" ] && [ "${FN#0x}" != "$FN" ]; then O=$FN; fi
[ -z "$O" ] && { echo "未知函数 $FN"; exit 2; }

# ★ 必须用 python 算：dash 的 $(( )) 对 >2^63 的十六进制会报 Illegal number
ADDR=$(python3 -c "
b=int(open('/sys/module/nvidia/sections/.text').read().strip(),16)
print('0x%x' % (b + $O))")
echo "=== $FN @ $ADDR  轮数=$N  MB=$MB ==="

OUT=/tmp/loop-$FN.data
rm -f "$OUT"

perf record -e "mem:$ADDR:x" -a --intr-regs=ip,sp,cx,dx,r8 -o "$OUT" -- \
  bash -c "for i in \$(seq 1 $N); do $BW $MB 2 >/dev/null 2>&1; done" 2>&1 | tail -2

TOTAL=$(perf script -i "$OUT" 2>/dev/null | grep -c . || true)
echo
echo "★ 新建上下文 $N 次，$FN 执行 $TOTAL 次"
if [ "$TOTAL" -gt 0 ]; then
  echo "--- 每次命中的进程与时间 ---"
  perf script -i "$OUT" 2>/dev/null | awk '{printf "  %-12s pid=%-7s t=%s\n", $1, $3, $4}' | head -30
  echo "--- 寄存器（cx=可能为 BAR0 偏移）---"
  perf script -i "$OUT" 2>/dev/null | grep -oE 'cx: *0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -10
  echo "--- 栈指针分布（区分不同调用点）---"
  perf script -i "$OUT" 2>/dev/null | grep -oE 'sp: *0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -10
fi
