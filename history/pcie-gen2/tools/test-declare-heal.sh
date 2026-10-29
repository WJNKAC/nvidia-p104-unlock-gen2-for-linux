#!/bin/bash
# test-declare-heal.sh —— 验证守卫的「声明自愈」是否有效
#
# ## 背景（reports/198）
#   实测：`rmmod nvidia && modprobe nvidia` 之后，驱动会把端点声明**重置回 Gen1**：
#       LnkCap  = 0x00453c41  MaxSpeed=1
#       LnkCap2 = 0x00000002  vector=0x02（仅 Gen1）
#       LnkCtl2 = 0x0001      TLS=1
#   旧版守卫只按回 SPEED + 重训，从不重新解锁声明 ⇒ 一旦 push 失败就永远救不回来
#   （实测连续 13 次重训全部 LNKSTA=1041）。
#
#   新守卫（gen2kr.c）在 stage0 增加了：
#       if (CAP2 != 0x06) { 重新做一次声明解锁; }   // 频率限制 declare_min_ms
#
# ## 本脚本
#   故意在"声明已被重置"的状态下加载新守卫，只触发一次 push，
#   看它能否靠自己把声明补回来并升到 Gen2。
#
# 用法：bash test-declare-heal.sh
set -u
D=/data/labs/nvidia/pcie-gen2
DEV=0000:01:00.0

state() {
python3 - <<'PY'
import mmap,os,struct
try:
    p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
    f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
    m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
    cap2=struct.unpack_from('<I',m,0x880A4)[0]
    lk  =struct.unpack_from('<I',m,0x8C040)[0]
    ls  =struct.unpack_from('<I',m,0x88088)[0]
    m.close(); os.close(f)
    print(f"CAP2=0x{cap2:04x}  SPEED={(lk>>18)&3}  LNKSTA={(ls>>16)&0xF}")
except Exception:
    print("CAP2=?  SPEED=?  LNKSTA=? (读不到)")
PY
}

echo "=== 0. 卸载旧守卫、清 dmesg ==="
rmmod gen2kr 2>/dev/null && echo "  旧守卫已卸载" || echo "  旧守卫未加载"
dmesg -C >/dev/null 2>&1

echo
echo "=== 1. 加载新守卫（带声明自愈）==="
cd "$D/kmod" || exit 1
insmod gen2kr.ko || { echo "  ✗ insmod 失败"; exit 2; }
echo "  OK   declare_min_ms=$(cat /sys/module/gen2kr/parameters/declare_min_ms)"

echo
echo "=== 2. 起点状态（声明应是被驱动重置的 Gen1）==="
echo "  $(state)"

echo
echo "=== 3. 触发一次 push（守卫随后接手）==="
echo "$DEV" > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null
sleep 0.8
echo "$DEV" > /sys/bus/pci/drivers/nvidia/bind 2>/dev/null

echo "  跟踪 30s（每 5s 打一次状态）："
for i in $(seq 1 6); do
  sleep 5
  echo "   ${i}0s: $(state)"
done

echo
echo "=== 4. 守卫日志 ==="
dmesg | grep -i gen2kr | tail -20

echo
echo "=== 5. 结论 ==="
S=$(state)
case "$S" in
  *LNKSTA=2*) echo "  ✓ 声明自愈生效，链路到 Gen2" ;;
  *) echo "  ✗ 未到 Gen2 —— 看日志里有没有『声明被重置』+『重新解锁』两条" ;;
esac
