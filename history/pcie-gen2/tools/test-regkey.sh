#!/bin/bash
# test-regkey.sh —— 用 registry 键改变驱动「要 Gen1 还是 Gen2」的策略
#
# ## 已确证的事实链（reports/198）
#
# 1. 驱动的 `_nv017105rm` = `set_link_speed(mode)`：
#        mode 1 → SPEED=2 (2.5GT/s)   mode 2 → SPEED=1 (5.0GT/s)   mode 3 → SPEED=0 (8.0GT/s)
#
# 2. perf 实测：每次 `cuInit`（在 `rm_init_adapter` 里）都以 **mode=1** 调用它。
#    调用栈：rm_init_adapter → _nv000790rm → _nv031502rm → _nv031218rm → _nv031217rm
#            → _nv031219rm → _nv050988rm → _nv015094rm → _nv050526rm → _nv014904rm
#            → _nv014838rm → _nv050280rm → _nv021137rm → _nv021141rm → _nv021133rm
#            → _nv017755rm+0x98 → _nv017105rm
#
# 3. `_nv017755rm(mode)` 里有：`if (mode == 当前速率) return;` —— 现在是 Gen2 而 mode=1，
#    所以每次都会**主动降回 Gen1**。
#
# 4. 静态定位到 `_nv017157rm` 读三个 registry 键，其中：
#        RMPcieLinkSpeed        → 写入 obj[0xa38]（策略位域，reports/192 里那个）
#        RMPcieStickyGenSpeed   → 也是"粘住 Gen 速率"语义
#
# ⇒ 若 `RMPcieLinkSpeed` 是"期望的链路代数"，把它设成 2，
#   驱动就应该请求 Gen2，**RM 内部也一致**，从而不需要 gen2kr 的守卫硬按。
#
# ## 本脚本做什么
#   1. 卸干净 nvidia
#   2. 用 `NVreg_RegistryDwords="RMPcieLinkSpeed=2"` 重新加载
#   3. 用 gen2kr 做解锁 + 初始推送（声明必须解锁，否则 5GT 请求会被硬件拒绝）
#   4. 触发一次 CUDA 上下文，看链路**会不会被打回 Gen1**
#
#   ★ 判据只用寄存器 LNKSTA，不看 nvidia-smi（它读驱动缓存）
#
# 用法：bash test-regkey.sh [RMPcieLinkSpeed 值，默认 2]
set -u
D=/data/labs/nvidia/pcie-gen2
DEV=0000:01:00.0
VAL="${1:-2}"
# 也支持直接给完整 dword 串，如 "RMPcieStickyGenSpeed=1"
RAW="${2:-}"

lnk() {
python3 - <<'PY'
import mmap,os,struct
try:
    p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
    f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
    m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
    v=struct.unpack_from('<I',m,0x8C040)[0]
    l=struct.unpack_from('<I',m,0x88088)[0]
    m.close(); os.close(f)
    print((l>>16)&0xF, (v>>18)&3)
except Exception:
    print("15 3")
PY
}

wait_gen2() {
  local n="${1:-20}"
  for i in $(seq 1 $((n*5))); do
    read -r SP _ <<<"$(lnk)"
    [ "$SP" = "2" ] && return 0
    sleep 0.2
  done
  return 1
}

echo "======== 基线：当前（无 registry 键）======"
read -r SP SD <<<"$(lnk)"
echo "  LNKSTA[3:0]=$SP  SPEED=$SD"

echo
echo "======== 1. 卸干净 nvidia ======"
rmmod gen2kr 2>/dev/null
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  rmmod "$m" 2>/dev/null
done
sleep 1
lsmod | grep -E '^nvidia' && echo "  ⚠ 还有残留" || echo "  已全部卸载"

echo
echo "======== 2. 带 registry 键重新加载（RMPcieLinkSpeed=$VAL）======"
DWORD="RMPcieLinkSpeed=$VAL"
if [ -n "$RAW" ]; then DWORD="$RAW"; fi
echo "  dword: $DWORD"
if modprobe nvidia "NVreg_RegistryDwords=$DWORD" 2>/tmp/mp.err; then
  echo "  modprobe OK"
else
  echo "  ✗ modprobe 失败："; cat /tmp/mp.err
  modprobe nvidia 2>/dev/null && echo "  回退为无键加载"
fi
modprobe nvidia_uvm 2>/dev/null
sleep 2
echo -n "  已加载参数: "; cat /sys/module/nvidia/parameters/RegistryDwords 2>/dev/null || echo "(读不到)"
echo -n "  当前链路: "; read -r SP SD <<<"$(lnk)"; echo "LNKSTA=$SP SPEED=$SD"

echo
echo "======== 3. gen2kr 解锁声明 + 初始推送 ======"
cd "$D/kmod" || exit 1
insmod gen2kr.ko 2>&1 && echo "  insmod OK"
sleep 1
echo "$DEV" > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null
sleep 0.8
echo "$DEV" > /sys/bus/pci/drivers/nvidia/bind 2>/dev/null
sleep 5
dmesg | grep -i gen2kr | tail -6
if wait_gen2 15; then
  echo "  ✓ 已到 Gen2"
else
  read -r SP SD <<<"$(lnk)"
  echo "  ✗ 未到 Gen2（LNKSTA=$SP）—— 后续观察无意义"
  exit 2
fi

echo
echo "======== 4. ★ 决定性：触发 CUDA 上下文，看是否被打回 Gen1 ======"
echo "   （若 registry 键生效，驱动应认为目标已是 Gen2，不会再降）"
read -r SP0 SD0 <<<"$(lnk)"
echo "  触发前: LNKSTA=$SP0 SPEED=$SD0"
"$D/minictx" 1 >/dev/null 2>&1
sleep 1
read -r SP1 SD1 <<<"$(lnk)"
echo "  触发后: LNKSTA=$SP1 SPEED=$SD1"

echo
echo "  再等 12s，看守卫有没有动作（守卫动作=驱动确实降过级）"
sleep 12
read -r SP2 SD2 <<<"$(lnk)"
echo "  12s 后: LNKSTA=$SP2 SPEED=$SD2"
echo
echo "  守卫日志（有 [guard] 就说明驱动降级了）："
dmesg | grep -i 'gen2kr.*guard' | tail -6 || echo "    （无 guard 动作）"

echo
echo "======== 结论 ======"
if [ "$SP1" = "2" ] && [ "$SP2" = "2" ]; then
  echo "  ★★★ RMPcieLinkSpeed=$VAL 生效：驱动不再把链路打回 Gen1！"
  echo "      守卫一次都没动作 ⇒ 可能可以彻底去掉守卫。"
elif [ "$SP1" = "2" ] && [ "$SP2" = "1" ]; then
  echo "  触发后仍是 Gen2，但 12s 内掉了 ⇒ 驱动在稍后才降级（或端点内部降级）"
  echo "      看上面 guard 日志区分：有 guard 动作 = 驱动降级；无 = 端点内部降级"
else
  echo "  RMPcieLinkSpeed=$VAL 没有阻止降级 ⇒ 这个键不是控制点，或需要配合别的键"
  echo "      下一步可试 RMPcieStickyGenSpeed=1（静态分析里另一个候选）"
fi
