#!/bin/bash
# measure-recovery.sh —— 测量「链路掉 Gen1 → 守卫拉回 Gen2」的耗时
#
# 背景（reports/195 §4.5）
#   实测发现守卫恢复要 ~20s，而 `push_fn` 一次就成功（104ms）。
#   静态对比两者时序，差异在 `0x8872C=6 → 根端口 Retrain` 的间隔：
#       push_fn : msleep(50)                 → 成功
#       guard_fn: 受 retrain_min_ms=2000 限制 → 4 轮全失败
#   ⇒ 怀疑该间隔不能长（0x8872C=6 的效果会失效）。
#
# 本脚本负责量化：从检测到 Gen1 开始，到 LNKSTA 回到 2 用了多少秒。
#
# 用法：bash measure-recovery.sh [超时秒数]
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
DEV=0000:01:00.0
BW="$D/build/bwtest2"
TIMEOUT="${1:-60}"

lnk() {
python3 - <<'PY'
import mmap,os,struct
p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
l=struct.unpack_from('<I',m,0x88088)[0]
v=struct.unpack_from('<I',m,0x8C040)[0]
print((l>>16)&0xF, f"{(v>>18)&3}")
PY
}

echo "=== 0. 等链路先到 Gen2 ==="
for i in $(seq 1 "$TIMEOUT"); do
  read -r SP SPEED <<<"$(lnk)"
  if [ "$SP" = "2" ]; then echo "  就绪（${i}s，SPEED=$SPEED）"; break; fi
  sleep 1
done
if [ "$SP" != "2" ]; then echo "✗ 等不到 Gen2，终止"; exit 2; fi

echo
echo "=== 1. 触发一次掉速（跑 1 轮 bwtest2，它会新建上下文）==="
T0=$(date +%s.%N)
"$BW" 64 1 >/dev/null 2>&1

echo "=== 2. 立即开始轮询，记录回到 Gen2 的时刻 ==="
GOT=0
for i in $(seq 1 $((TIMEOUT * 5))); do
  read -r SP SPEED <<<"$(lnk)"
  NOW=$(date +%s.%N)
  E=$(python3 -c "print(f'{$NOW-$T0:.2f}')")
  if [ "$SP" = "2" ]; then
    echo "  ★ ${E}s 回到 Gen2（SPEED=$SPEED）"
    GOT=1
    break
  fi
  # 每 2 秒打一行，避免刷屏
  if [ $((i % 10)) -eq 0 ]; then echo "  ${E}s  sp=$SP SPEED=$SPEED"; fi
  sleep 0.2
done
[ "$GOT" = "0" ] && echo "  ✗ ${TIMEOUT}s 内未恢复"

echo
echo "=== 3. 守卫日志（本次）==="
dmesg | grep -i gen2kr | tail -12
