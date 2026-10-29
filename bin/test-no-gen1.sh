#!/bin/bash
# test-no-gen1.sh —— 决定性验证：补丁能否在「没有守卫」的情况下阻止降级
#
# ## 逻辑
#   旧方案（gen2kr 守卫）：驱动降 Gen1 → 守卫发现 → 3.8s 后拉回。是"事后救"。
#   新方案（补丁）：让 `_nv017105rm(mode=1)` 在入口 bail，驱动**不会再降**。是"不降"。
#
#   要证明补丁独立有效，必须**把守卫摘掉**再测：
#     · 有守卫 → 测不出补丁的作用（守卫会把链路拉回来）
#     · 没守卫 → 链路若保持 Gen2，只可能是补丁的功劳
#
# ## 流程
#   1. 卸 gen2kr + nvidia
#   2. modprobe nvidia（读补丁后的 .ko）
#   3. insmod gen2kr → unbind/bind 触发 push 到 Gen2
#   4. ★ rmmod gen2kr（摘掉守卫）
#   5. 跑 N 轮 minictx（每轮 1 个 CUDA 上下文 = 1 次 cuInit）
#   6. 对比每轮前后的 LNKSTA
#
# ## 判定（只用寄存器，不看 nvidia-smi）
#   · 每轮后 LNKSTA[3:0] 仍 == 2  ⇒ ★补丁有效，驱动不再降级
#   · 掉回 1                        ⇒ 补丁无效，另有降级路径
#
# 用法：bash test-no-gen1.sh [轮数]
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
DEV=0000:01:00.0
N="${1:-3}"

st() {
python3 - <<'PY'
import mmap,os,struct
try:
    p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
    f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
    m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
    a=struct.unpack_from('<I',m,0x880A4)[0]
    l=struct.unpack_from('<I',m,0x88088)[0]
    t=struct.unpack_from('<I',m,0x880A8)[0]
    s=struct.unpack_from('<I',m,0x8C040)[0]
    m.close(); os.close(f)
    print(f"CAP2={a:#06x} LNKSTA={(l>>16)&0xF} TLS={t&0xF} SPEED={(s>>18)&3}")
except Exception:
    print("读不到（设备可能异常）")
PY
}

echo "======== 0. 确认补丁在位 ========"
python3 "$D/tools/patch_no_gen1.py" check 2>&1 | tail -3

echo
echo "======== 1. 卸 gen2kr + nvidia ========"
rmmod gen2kr 2>/dev/null && echo "  gen2kr 已卸" || echo "  gen2kr 未加载"
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do rmmod "$m" 2>/dev/null; done
sleep 1
lsmod | grep -E '^nvidia' && echo "  ⚠ 还有残留" || echo "  已卸干净"

echo
echo "======== 2. 加载补丁后的 nvidia ========"
modprobe nvidia && modprobe nvidia_uvm && echo "  OK"
sleep 2
echo "  起点: $(st)"

echo
echo "======== 3. gen2kr push 到 Gen2 ========"
cd "$D" && insmod build/gen2kr.ko && echo "  insmod OK"
sleep 1
echo "$DEV" > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null
sleep 0.8
echo "$DEV" > /sys/bus/pci/drivers/nvidia/bind 2>/dev/null
sleep 6

OK2=0
for i in $(seq 1 25); do
  case "$(st)" in *LNKSTA=2*) OK2=1; break;; esac
  sleep 0.4
done
echo "  push 后: $(st)"
if [ "$OK2" = "0" ]; then
  echo "  ✗ 没到 Gen2，后续无法判定。看 dmesg | grep gen2kr"
  dmesg | grep -i gen2kr | tail -6
  exit 2
fi

echo
echo "======== 4. ★ 摘掉守卫（只留补丁）========"
rmmod gen2kr && echo "  gen2kr 已卸载 —— 现在没有任何东西会在降级后把链路拉回"
sleep 1
lsmod | grep -q '^gen2kr' && echo "  ⚠ 还在" || echo "  确认已无守卫"

echo
echo "  摘守卫前: $(st)"

echo
echo "======== 5. 连跑 $N 轮 CUDA 上下文，每轮看链路 ========"
FAIL=0
for i in $(seq 1 "$N"); do
  "$D/build/minictx" 1 >/dev/null 2>&1
  sleep 0.8
  S=$(st)
  echo "  第 $i 轮后: $S"
  case "$S" in
    *LNKSTA=2*) ;;
    *) FAIL=$((FAIL+1));;
  esac
done

echo
echo "======== 6. 再等 10s，确认不是延迟降级 ========"
sleep 10
S=$(st)
echo "  10s 后: $S"
case "$S" in *LNKSTA=2*) ;; *) FAIL=$((FAIL+1));; esac

echo
echo "======== 7. Xid 检查 ========"
dmesg | grep -i "Xid" | tail -3 || echo "  无 Xid"

echo
echo "======== 结论 ========"
if [ "$FAIL" = "0" ]; then
  echo "  ★★★ 补丁有效！摘掉守卫后链路仍稳定保持 Gen2。"
  echo "      ⇒ 降级被完全堵住，不再需要 gen2kr 的 3.8s 守卫。"
  echo "      ⇒ gen2kr 只承担「解锁声明 + 探针时机首次 push」，可以退化为一次性模块。"
else
  echo "  有 $FAIL 次掉回 Gen1 ⇒ 补丁不足，仍有别的降级路径。"
  echo "      ⇒ 用 trace-mode1.sh 复查 mode=1 是否真的 bail（entry 命中但没有 or80000）"
fi
