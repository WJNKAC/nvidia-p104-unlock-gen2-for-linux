#!/bin/bash
# probe-cuda.sh —— 在「跑 CUDA」的同时统计锁定环执行次数
#
# 背景：probe-run.sh 测到空闲 20s 内 `_nv017281rm` 执行 0 次。
# 但这不能证明它不执行 —— 必须跑能触发链路活动的负载。
#
# 本脚本对比三个场景：
#   A. 冷态 Gen1 下跑 bwtest2（新建 CUDA 上下文）
#   B. 推上 Gen2 后跑 bwtest2（驱动会写回 SPEED=2 → 链路掉 Gen1）
#   C. 空闲对照
#
# 用法：bash probe-cuda.sh <A|B|C> [函数名]
set -u

SCEN="${1:-A}"
FN="${2:-_nv017281rm}"
BW=/data/labs/nvidia/pcie-gen2/bwtest2

declare -A OFF=([_nv017281rm]=0x1269f0 [_nv017282rm]=0x12d2f0
                [_nv005948rm]=0xb3a240 [_nv053907rm]=0x5ffda0 [_nv053912rm]=0x5ff6c0)
BASE=$(cat /sys/module/nvidia/sections/.text)
ADDR=$(printf '0x%x' $(( BASE + ${OFF[$FN]} )))
OUT=/tmp/probe-cuda-$SCEN-$FN.data
rm -f "$OUT"

echo "=== 场景 $SCEN  函数 $FN @ $ADDR ==="

# 先记录链路起点
echo -n "链路初始: "; lspci -vv -s 01:00.0 2>/dev/null | grep -o 'LnkSta:.*' | head -1

case "$SCEN" in
  A)
    # 冷态 Gen1，直接跑
    perf record -e "mem:$ADDR:x" -a --intr-regs=ip,sp,cx,dx,r8 -o "$OUT" -- \
      "$BW" 256 6 >/tmp/bw-$SCEN.log 2>&1
    ;;
  B)
    # 推 Gen2，再跑（预期驱动把链路打回 Gen1，此时最可能触发恢复逻辑）
    echo "--- 先推 Gen2 ---"
    python3 /data/labs/nvidia/pcie-gen2/safe-gen2.py 2>&1 | tail -8
    sleep 2
    echo -n "推完链路: "; lspci -vv -s 01:00.0 2>/dev/null | grep -o 'LnkSta:.*' | head -1
    perf record -e "mem:$ADDR:x" -a --intr-regs=ip,sp,cx,dx,r8 -o "$OUT" -- \
      "$BW" 256 6 >/tmp/bw-$SCEN.log 2>&1
    ;;
  C)
    perf record -e "mem:$ADDR:x" -a --intr-regs=ip,sp,cx,dx,r8 -o "$OUT" -- sleep 20
    ;;
  *)
    echo "场景必须是 A/B/C"; exit 2;;
esac

TOTAL=$(perf script -i "$OUT" 2>/dev/null | grep -c . || true)
echo
echo "★ $FN 执行次数: $TOTAL"
if [ "$TOTAL" -gt 0 ]; then
  echo "--- 前 20 条（ip/sp/cx/dx/r8）---"
  perf script -i "$OUT" 2>/dev/null | head -20
  echo "--- CX 值分布（若函数把寄存器偏移当参数，这里能看出读的是哪个寄存器）---"
  perf script -i "$OUT" 2>/dev/null | grep -oE 'cx: *0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -15
else
  echo "★ 该函数在本场景【一次都没执行】"
fi
echo
echo -n "链路结束: "; lspci -vv -s 01:00.0 2>/dev/null | grep -o 'LnkSta:.*' | head -1
[ -f /tmp/bw-$SCEN.log ] && { echo "--- 带宽 ---"; grep -iE "GB/s|H2D|D2H" /tmp/bw-$SCEN.log | tail -8; }
