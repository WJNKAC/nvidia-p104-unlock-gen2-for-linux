#!/bin/bash
# test-patch-alone.sh —— 验证「只有补丁、没有推送」时链路到不了 Gen2
#
# ## 结论（reports/200 §1，已用整机重启实测过）
#   补丁在位、gen2kr 未加载时：
#     LNKSTA=1、LCAP2=0x0002、TLS=1、0x8841C=0xc0b40500、带宽 0.78
#   ⇒ **补丁只能"保持" Gen2，不能"建立" Gen2。**
#
# ## ★ 怎么才能复位到"声明未解锁"
#   **`unbind/bind` 不行！** 实测声明解锁是跨 unbind/bind 保持的
#   （reports/200 §3：解绑/绑定前后 CAP2 都是 0x0006、链路都是 Gen2）。
#
#   能复位的只有两条：
#     · `rmmod nvidia && modprobe nvidia`（快，但反复 reload 有锁死风险）
#     · 整机重启（最干净）
#
#   本脚本默认走 **模块重载**（快），但会先警告。
#   想跳过复位、只观察当前状态，用 `--no-reset`。
#
# 用法：
#   bash test-patch-alone.sh              # 模块重载复位后，用补丁独立尝试
#   bash test-patch-alone.sh --no-reset   # 只看现状，不复位
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
DEV=0000:01:00.0
RESET=1
[ "${1:-}" = "--no-reset" ] && RESET=0

st() {
python3 - <<'PY'
import mmap,os,struct
try:
    p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
    f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
    m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
    g=lambda o: struct.unpack_from('<I',m,o)[0]
    cap=g(0x88084); cap2=g(0x880A4); ls=g(0x88088); ctl2=g(0x880A8)
    cfg=g(0x8C040); priv=g(0x8841C)
    m.close(); os.close(f)
    print(f"CAP2=0x{cap2:04x} LNKSTA={(ls>>16)&0xF} TLS={ctl2&0xF} "
          f"SPEED={(cfg>>18)&3} MAXSPD={cap&0xF} 0x8841C=0x{priv:08x}")
except Exception:
    print("读不到")
PY
}

echo "======== 0. 起点 ========"
echo "  $(st)"
echo -n "  gen2kr: "; lsmod | grep -q '^gen2kr' && echo "已加载" || echo "未加载"
echo -n "  补丁  : "; python3 "$D/bin/patch_no_gen1.py" check 2>/dev/null | grep -oE "未打补丁|已打补丁"

echo
echo "======== 1. 摘掉 gen2kr（确保没有任何推送者）========"
if lsmod | grep -q '^gen2kr'; then
    rmmod gen2kr && echo "  已卸载"
fi
sleep 1
lsmod | grep -q '^gen2kr' && echo "  ⚠ 还在" || echo "  确认已无 gen2kr"

if [ "$RESET" = "1" ]; then
    echo
    echo "======== 2. 重载 nvidia 模块，把声明复位回 Gen1 ========"
    echo "  ★ 不能用 unbind/bind —— 它不会重置声明（reports/200 §3）"
    echo "  ★ 反复 reload 有进入驱动侧锁死态的风险；若失败请整机重启"
    for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
        rmmod "$m" 2>/dev/null || true
    done
    sleep 1
    if lsmod | grep -qE '^nvidia'; then
        echo "  ✗ nvidia 未卸干净（查 fuser -v /dev/nvidia*），中止"
        exit 1
    fi
    modprobe nvidia && modprobe nvidia_uvm && echo "  已重载"
    sleep 3
    echo "  复位后: $(st)"
else
    echo
    echo "======== 2. 跳过复位（--no-reset）========"
fi

echo
echo "======== 3. 观察 10 秒（只有补丁，没人推送）========"
for i in 1 2 3 4 5; do
    sleep 2
    echo "   $((i*2))s: $(st)"
done

echo
echo "======== 4. 结论 ========"
S="$(st)"
case "$S" in
  *LNKSTA=2*)
    if [ "$RESET" = "0" ]; then
      echo "  链路是 Gen2 —— 但很可能是之前推送的残留"
      echo "  （声明解锁跨 unbind/bind 保持，见 reports/200 §3）。"
      echo "  要真正测「只有补丁」，去掉 --no-reset 重跑，或整机重启后再测。"
    else
      echo "  ★ 复位后仍到 Gen2 —— 与 reports/200 的结论不一致，需复查"
    fi
    ;;
  *)
    echo "  ✓ 符合预期：链路仍是 Gen1"
    echo "    ⇒ 补丁只能【保持】Gen2，不能【建立】Gen2。"
    echo "    ⇒ 光装 patch 过的驱动不够，必须有 gen2kr 做那次推送。"
    ;;
esac
echo "  当前: $S"

echo
echo "======== 5. 恢复（重新加载 gen2kr 并推送）========"
if ! lsmod | grep -q '^gen2kr'; then
    insmod "$D/build/gen2kr.ko" 2>/dev/null && echo "  gen2kr 已加载"
fi
sleep 1
echo "$DEV" > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null
sleep 1
echo "$DEV" > /sys/bus/pci/drivers/nvidia/bind 2>/dev/null
for i in $(seq 1 30); do
    case "$(st)" in *LNKSTA=2*) break;; esac
    sleep 0.5
done
echo "  恢复后: $(st)"
case "$(st)" in
  *LNKSTA=2*) echo "  ✓ 已恢复 Gen2" ;;
  *) echo "  ⚠ 未恢复。若连续失败，按 reports/198：整机重启，不要再试" ;;
esac
