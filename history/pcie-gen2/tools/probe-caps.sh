#!/bin/bash
# probe-caps.sh —— 直接读驱动内部的「Gen2 能力位」，不依赖 nvidia-smi
#
# ## 为什么要这个（reports/198）
#
# `nvidia-smi` 的 `pcie.link.gen.max/current` **读的是驱动缓存**，
# 寄存器已经是 Gen2 它照样报 1（这是 reports/176-178 就定过的铁律，本轮又踩了一次）。
# 唯一可信的是寄存器（LnkCap/LnkCap2/LnkSta/LnkCtl2）。
#
# 但"寄存器是 Gen2" ≠ "驱动认为 Gen2 可用"。驱动内部有个能力位：
#
#   `_nv017279rm`（能力求交）= 5 路能力按位与：
#       1 个直接调用 + 4 个 vtable 槽 obj+0x1a8/0x1b0/0x1b8/0x1c0
#   `_nv017812rm(obj, flags)`（能力落盘）：纯位标志 →
#       flags & 0x04 → obj[0x954] = 1   （cap Gen2）
#       flags & 0x08 → obj[0x955] = 1   （cap Gen3）
#       flags & 0x10 → obj[0x956] = 1   （cap mode4）
#       flags & 0x20 → obj[0x957] = 1   （cap mode5）
#
# 而 `_nv017105rm`/`_nv017102rm`（驱动的 set_link_speed(mode)）在 mode>=2 时
# 先查 obj[0x954]，为 0 就**直接 bail**。
#
# ⇒ 用 perf 硬件执行断点抓 `_nv017812rm` 的 DX（第 3 参数 = flags），
#   就能直接读到"驱动到底认为支持不支持 Gen2"。
#
# ## 为什么必须 perf
#   这些函数都是 notrace（kprobe/bpftrace 会被拒），
#   且我们在 reports/187 被 `register_kprobe` 不检查 notrace 属性坑过（9 亿次调用）。
#
# ## 注意
#   本脚本**不写任何寄存器**，只读。触发 probe 用 unbind/bind。
#
# 用法：bash probe-caps.sh [秒数]
set -u
D=/data/labs/nvidia/pcie-gen2
DEV=0000:01:00.0
DUR="${1:-25}"

declare -A OFF=(
  [_nv017812rm]=0x11a230    # 能力落盘（flags 就是 DX）
  [_nv017279rm]=0x13c320    # 能力求交（调用者）
  [_nv017105rm]=0x127d60    # set_link_speed（mode 就是 DX）
)
BASE=$(cat /sys/module/nvidia/sections/.text)
A_SETTER=$(python3 -c "print('0x%x' % (int('$BASE',16) + ${OFF[_nv017812rm]}))")
A_MERGE=$(python3 -c "print('0x%x' % (int('$BASE',16) + ${OFF[_nv017279rm]}))")
A_SETSPD=$(python3 -c "print('0x%x' % (int('$BASE',16) + ${OFF[_nv017105rm]}))")

echo "=== 探针地址（.text 基址 $BASE）==="
echo "  _nv017812rm(能力落盘) $A_SETTER   ← DX = flags"
echo "  _nv017279rm(能力求交) $A_MERGE"
echo "  _nv017105rm(set_speed) $A_SETSPD  ← DX = mode"

echo
echo "=== 抓 ${DUR}s，期间触发一次 probe（unbind/bind）==="
OUT=/tmp/caps.data
rm -f "$OUT"

# 后台：15 秒后触发 probe（给 perf 留出启动时间）
( sleep 8
  echo "$DEV" > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null
  sleep 1
  echo "$DEV" > /sys/bus/pci/drivers/nvidia/bind 2>/dev/null
) &
TRIG=$!

perf record -e "mem:$A_SETTER:x" -e "mem:$A_MERGE:x" -e "mem:$A_SETSPD:x" \
     -a --intr-regs=ip,dx,si,di -o "$OUT" -- sleep "$DUR" 2>&1 | tail -3
wait $TRIG 2>/dev/null

echo
echo "=== 命中统计 ==="
for pair in "$A_SETTER:_nv017812rm(flags)" "$A_MERGE:_nv017279rm" "$A_SETSPD:_nv017105rm(mode)"; do
  A="${pair%%:*}"; NM="${pair#*:}"
  C=$(perf script -i "$OUT" 2>/dev/null | grep -c "mem:$A:x" || true)
  printf '  %-24s %s 次\n' "$NM" "$C"
done

echo
echo "=== ★ 能力落盘调用的 flags（DX）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | grep -A0 "IP:$A_SETTER" \
  | grep -oE 'DX:0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -10
echo
echo "  （若为空，说明该地址没命中或用别的寄存器传参，看下面的原始记录）"

echo
echo "=== ★ set_link_speed 的 mode（DX）==="
perf script -i "$OUT" -F comm,iregs 2>/dev/null | grep "IP:$A_SETSPD" \
  | grep -oE 'DX:0x[0-9a-f]+' | sort | uniq -c | sort -rn | head -10

echo
echo "=== 原始记录（前 12 条）==="
perf script -i "$OUT" -F comm,ip,iregs 2>/dev/null | head -12

echo
echo -n "=== 事后链路: "; lspci -vv -s ${DEV#0000:} 2>/dev/null | grep -o 'LnkSta:.*' | head -1
