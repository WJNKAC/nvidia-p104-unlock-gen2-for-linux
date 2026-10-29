#!/bin/bash
# probe-multi.sh —— 在 Gen2 状态下同时给多个候选函数打执行断点，找真正的降速元凶
#
# 背景（reports/195，已实测）
#   从 Gen2 起点采样 320 万次，抓到同一瞬间（0.090s）：
#       0x8C040  0x20545800(SPEED=1) → 0x20585800(SPEED=2)
#       LNKSTA   0x1042(sp=2)        → 0x1041(sp=1)
#   ⇒ 链路降级是**软件改写 SPEED** 造成的，不是硬件协商回退。
#   而 `_nv017281rm` 已被补丁改成只写 SPEED=1，所以另有写入者。
#
# 候选（reports/194 的 11 个 0x8C040 访问函数里，除已 patch 的 `_nv017281rm` 外，
# 其余"读-改-写"型）：
#   _nv017102rm 0x130110   _nv017105rm 0x127d60
#   _nv017628rm 0x128200   _nv017283rm 0x12db20
#   _nv017629rm 0x12e850   _nv049698rm 0x94710
#
# ★ x86 只有 4 个调试寄存器，同时最多 4 个执行断点 ⇒ 分批。
#
# 用法：bash probe-multi.sh [A|B]      A=前 4 个  B=后 2 个
set -u
BATCH="${1:-A}"
D=/data/labs/nvidia/pcie-gen2
DEV=0000:01:00.0

case "$BATCH" in
A) NAMES=(_nv017102rm _nv017105rm _nv017628rm _nv017283rm) ;;
B) NAMES=(_nv017629rm _nv049698rm) ;;
*) echo "用法: bash probe-multi.sh [A|B]"; exit 2 ;;
esac
declare -A OFF=(
  [_nv017102rm]=0x130110 [_nv017105rm]=0x127d60 [_nv017628rm]=0x128200
  [_nv017283rm]=0x12db20 [_nv017629rm]=0x12e850 [_nv049698rm]=0x94710
)

BASE=$(cat /sys/module/nvidia/sections/.text)

echo "=== 1. 先推到 Gen2 ==="
python3 $D/tools/unlock-decl.py --retrain 2>&1 | tail -6

# ★ 判据必须用寄存器 LNKSTA[3:0]，不能用 lspci 文本：
#   grep 'Speed 5GT/s' 会误匹配 "Speed 2.5GT/s"（子串），导致判断永远为真。
LNK=$(python3 -c "
import mmap,os,struct
b=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=b)
print((struct.unpack_from('<I',m,0x88088)[0]>>16)&0xF)
" 2>/dev/null)
echo "  寄存器判定 LNKSTA[3:0] = ${LNK:-?}  (2=Gen2)"
if [ "${LNK:-0}" != "2" ]; then
  echo "✗ 未到 Gen2，终止（只有 Gen2 状态下执行并写 SPEED=2 的才是元凶）"
  echo "  提示：重训可能需多轮，可先手动跑 unlock-decl.py --retrain 确认"
  exit 2
fi

echo
echo "=== 2. 打点：${NAMES[*]} ==="
ARGS=()
for n in "${NAMES[@]}"; do
  A=$(python3 -c "print('0x%x'%(int('$BASE',16)+${OFF[$n]}))")
  echo "  $n @ $A"
  ARGS+=(-e "mem:$A:x")
done

OUT=/tmp/multi-$BATCH.data
rm -f "$OUT"
echo
echo "=== 3. 跑 minictx（新建 1 个上下文，触发降速）==="
perf record "${ARGS[@]}" -a -o "$OUT" -- "$D/minictx" 1 2>&1 | tail -2

echo
echo "=== 4. 各断点命中次数 ==="
for n in "${NAMES[@]}"; do
  A=$(python3 -c "print('0x%x'%(int('$BASE',16)+${OFF[$n]}))")
  c=$(perf script -i "$OUT" 2>/dev/null | grep -c "mem:$A:x" || true)
  echo "  $n  $c"
done
echo
echo "=== 5. 明细（时间序）==="
perf script -i "$OUT" 2>/dev/null | head -30
echo
echo -n "结束链路: "; lspci -vv -s $DEV 2>/dev/null | grep -o 'LnkSta:.*' | head -1
