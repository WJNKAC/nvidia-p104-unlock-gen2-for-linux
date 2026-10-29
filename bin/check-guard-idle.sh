#!/bin/bash
# check-guard-idle.sh —— 补丁 + 守卫共存时，守卫是否完全不动
#
# ## 为什么查这个
#   补丁让驱动不再降 Gen1（reports/199）。那守卫（gen2kr 的 guard_fn）
#   在链路一直 Gen2 的情况下应该**一次都不动作**。
#   若仍看到 "[guard] ..." 日志，说明还有别的降级路径，补丁没完全接管。
#
# ## 判据
#   · dmesg 中 "gen2kr.*guard" 条数 == 0  ⇒ 补丁完全接管，守卫纯安全网
#   · > 0                                  ⇒ 链路掉过，需查原因
#
# 用法：bash check-guard-idle.sh [轮数]
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
N="${1:-4}"

st() {
python3 - <<'PY'
import mmap,os,struct
try:
    p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
    f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
    m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
    a=struct.unpack_from('<I',m,0x880A4)[0]
    l=struct.unpack_from('<I',m,0x88088)[0]
    print(f"CAP2={a:#06x} LNKSTA={(l>>16)&0xF}")
    m.close(); os.close(f)
except Exception:
    print("读不到")
PY
}

dmesg -C >/dev/null 2>&1
if ! lsmod | grep -q '^gen2kr'; then
  cd "$D/kmod" && insmod gen2kr.ko && echo "  已加载 gen2kr（作为安全网）"
else
  echo "  gen2kr 已在"
fi
sleep 2

echo "  起点: $(st)"
echo
echo "  跑 $N 轮 CUDA 上下文（每轮 1 次 cuInit）："
for i in $(seq 1 "$N"); do
  "$D/build/minictx" 1 >/dev/null 2>&1
  sleep 1
  echo "    第 $i 轮后: $(st)"
done

echo
echo "  再等 8s 确认不是延迟降级..."
sleep 8
echo "    8s 后: $(st)"

echo
G=$(dmesg | grep -c 'gen2kr.*guard' || true)
echo "======== guard 日志条数: $G ========"
if [ "$G" = "0" ]; then
  echo "  ★ 守卫零动作 ⇒ 补丁完全接管，守卫只是安全网"
else
  echo "  ⚠ 守卫动作过 $G 次，说明链路掉过。最近几条："
  dmesg | grep 'gen2kr.*guard' | tail -6
fi

echo
echo "======== gen2kr 全部日志 ========"
dmesg | grep gen2kr | tail -10 || echo "  （无）"
