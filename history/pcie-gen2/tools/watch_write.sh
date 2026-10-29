#!/bin/bash
# watch_write.sh —— 在 BAR0+0x8C040 的物理地址上设硬件写断点，找出谁在写
# ★ 必须用 sudo 之外的可靠来源：sysfs resource 表（无需 root，且不受 setpci 权限限制）
R=$(cat /sys/bus/pci/devices/0000:01:00.0/resource | head -1 | awk '{print $1}')
BAR0=$(printf '%x' $((R)))
if [ -z "$BAR0" ] || [ "$BAR0" = "ffffffff" ] || [ "$BAR0" = "0" ]; then
  echo "!! BAR0 取不到（$BAR0），中止"; exit 1
fi
TGT=$(printf '0x%x' $((0x$BAR0 + 0x8C040)))
echo "BAR0=0x$BAR0  目标物理地址=$TGT"

echo "--- 基线值 ---"
python3 /data/labs/nvidia/pcie-gen2/bar0peek.py 基线 2>&1 | grep -oE '0x8C040 LINKCFG0=0x[0-9a-f]+'
echo "--- 链路 ---"
lspci -vv -s 01:00.0 2>/dev/null | grep 'LnkSta:' | head -1

rm -f /tmp/hw5.data
perf record -e "mem:$TGT:w" -a --intr-regs=ip,cx,r8 -o /tmp/hw5.data -- \
  sh -c 'for i in 1 2 3; do /data/labs/nvidia/pcie-gen2/bwtest2 32 2 >/dev/null 2>&1; sleep 1; done' > /tmp/pr5.log 2>&1
echo "perf rc=$?"
grep -o '[0-9]* samples' /tmp/pr5.log | tail -1

echo "=== 命中（谁写的）==="
perf script -i /tmp/hw5.data -F comm,ip,iregs 2>/dev/null | head -12

echo "--- 之后的值 ---"
python3 /data/labs/nvidia/pcie-gen2/bar0peek.py 之后 2>&1 | grep -oE '0x8C040 LINKCFG0=0x[0-9a-f]+'
lspci -vv -s 01:00.0 2>/dev/null | grep 'LnkSta:' | head -1
