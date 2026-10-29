#!/bin/bash
# recover-zombie.sh —— 用根端口 secondary bus reset 救回 C. ZOMBIE 状态的 P104
#
# 背景（pci-state.sh 三态）：C. ZOMBIE = 端点死锁，lspci 在、power_state=D0、
# 配置空间读 ffffffff。历史结论是"只能重启"。
# 但 kernel 6.12 的根端口提供 `reset_subordinate`（次级总线复位），
# 能在不重启整机的情况下把整条下游总线复位 —— 等价于热插拔上电。
#
# 前置条件：nvidia 驱动必须已卸载干净（否则复位后驱动握着陈旧状态）。
# 用法：sudo bash recover-zombie.sh [--dry]
set -u
DEV=0000:01:00.0
ROOT=0000:00:01.0
DRY="${1:-}"

say() { printf '\n=== %s ===\n' "$*"; }
run() {
  if [ "$DRY" = "--dry" ]; then echo "[dry] $*"; else echo "[run] $*"; eval "$@"; fi
}

say "0. 复位前状态"
lspci -Dn | grep -i 1b87 || echo "  设备不在总线上（B. GONE）"
echo -n "  cfg: "; setpci -s $DEV 0.l 2>&1
echo -n "  power_state: "; cat /sys/bus/pci/devices/$DEV/power_state 2>&1
echo -n "  reset_subordinate 存在: "; [ -e /sys/bus/pci/devices/$ROOT/reset_subordinate ] && echo yes || echo NO

say "1. 卸载 nvidia 全套（顺序敏感：先用户态持有者）"
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  if lsmod | grep -q "^$m "; then
    run "rmmod $m 2>&1 || echo '  rmmod $m 失败（可能仍被占用）'"
  else
    echo "  $m 未加载"
  fi
done
lsmod | grep -E '^nvidia' || echo "  nvidia 系列已全部卸载"

say "2. 把设备从总线移除"
if [ -e /sys/bus/pci/devices/$DEV/remove ]; then
  run "echo 1 > /sys/bus/pci/devices/$DEV/remove"
  sleep 1
fi
lspci -Dn | grep -i 1b87 && echo "  ★ 仍在（remove 未生效）" || echo "  已移除"

say "3. 根端口次级总线复位（本脚本的核心）"
run "echo 1 > /sys/bus/pci/devices/$ROOT/reset_subordinate"
sleep 2

say "4. 重新扫描总线"
run "echo 1 > /sys/bus/pci/rescan"
sleep 3

say "5. 恢复 BAR0 + Memory Space Enable（驱动未加载时必须手动）"
BDF=$(lspci -Dn | grep -i 1b87 | cut -d' ' -f1)
if [ -z "$BDF" ]; then
  echo "  ✗ 扫描后设备仍不存在 —— 复位未能救回，需要整机重启"
  exit 2
fi
echo "  设备回来了：$BDF"
BAR0=$(awk 'NR==1{print $1}' /sys/bus/pci/devices/$BDF/resource)
echo "  BAR0 物理地址: $BAR0"
if [ "$DRY" != "--dry" ]; then
  setpci -s $BDF 0x10.l="$BAR0"
  setpci -s $BDF COMMAND=0x6
fi
echo -n "  cfg 可读性: "; setpci -s $BDF 0.l 2>&1
echo -n "  power_state: "; cat /sys/bus/pci/devices/$BDF/power_state 2>&1

say "6. 加载驱动"
run "modprobe nvidia && modprobe nvidia_uvm && modprobe nvidia_modeset 2>&1"
sleep 3
echo -n "  nvidia-smi: "; nvidia-smi --query-gpu=name,pci.bus_id --format=csv,noheader 2>&1 | head -2

say "7. 结果"
echo -n "  LnkSta: "; lspci -vv -s $BDF 2>/dev/null | grep -o 'LnkSta:.*' | head -1
echo "  说明：复位后 LCAP2 会回到冷态 0x0002，需再跑 safe-gen2.py 推 Gen2"
