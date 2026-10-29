#!/bin/bash
# sweep-settle.sh —— 用 gen2kr 参数扫描 speed_settle_ms，找安全下限
#
# 为什么用参数扫描而不是 selfheal.py（reports/197）
#   `tools/probe-selfheal.sh` 用**用户态 mmap 直接写 0x8C040**，两次把设备打成
#   C. ZOMBIE（dmesg 无 Xid，硬件级静默失联）。而 gen2kr 的守卫用**内核 writel**
#   （ioremap 映射）跑了几十分钟、按回几十次都没事。
#   ⇒ 结论：验证 speed_settle_ms 应该走内核路径，而不是用户态 mmap。
#
#   本脚本给 gen2kr 传不同的 speed_settle_ms，每个值跑一次"降级→恢复"，
#   记录：能否恢复、耗时多少、是否掉卡（读 LNKSTA 连续 ffffffff）。
#
# ★ 判活：出现 0xffffffff 必须连续 20 次才算死（重训瞬态是常态）
#
# 用法：bash sweep-settle.sh "3000 1500 800"
set -u
DEV=0000:01:00.0
D=/data/labs/nvidia/pcie-gen2
KMOD=$D/kmod

VALUES="${1:-3000 1500 800}"

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

# 连续 20 次 0xffffffff 才判死
alive_check() {
  local bad=0
  for i in $(seq 1 20); do
    read -r SP _ <<<"$(lnk)"
    if [ "$SP" = "15" ]; then bad=$((bad+1)); else return 0; fi
    sleep 0.05
  done
  return 1
}

echo "###### 扫描 speed_settle_ms: $VALUES ######"
printf '%-8s %-10s %-10s %s\n' "settle" "恢复耗时" "结果" "备注"

for S in $VALUES; do
  # 重载模块，带指定参数
  rmmod gen2kr 2>/dev/null
  sleep 0.5
  if ! insmod "$KMOD/gen2kr.ko" speed_settle_ms="$S" >/dev/null 2>&1; then
    echo "  加载失败（speed_settle_ms=$S）"; continue
  fi
  sleep 0.5

  # 触发 push
  echo "$DEV" > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null
  sleep 0.8
  echo "$DEV" > /sys/bus/pci/drivers/nvidia/bind 2>/dev/null
  sleep 3

  # 等 Gen2
  ready=0
  for i in $(seq 1 20); do
    read -r SP _ <<<"$(lnk)"
    [ "$SP" = "2" ] && { ready=1; break; }
    sleep 0.5
  done
  if [ "$ready" = "0" ]; then
    printf '%-8s %-10s %-10s %s\n' "$S" "-" "未就绪" "push 后没到 Gen2"
    continue
  fi

  # 制造降级 + 计时
  T0=$(date +%s.%N)
  "$D/minictx" 1 >/dev/null 2>&1
  GOT=""
  for i in $(seq 1 300); do   # 最多 60s
    read -r SP _ <<<"$(lnk)"
    if [ "$SP" = "2" ]; then
      GOT=$(python3 -c "print(f'{$(date +%s.%N)-$T0:.2f}')")
      break
    fi
    if [ "$SP" = "15" ]; then
      if ! alive_check; then
        printf '%-8s %-10s %-10s %s\n' "$S" "-" "★掉卡" "ZOMBIE，需重启"
        break
      fi
    fi
    sleep 0.2
  done

  if [ -n "$GOT" ]; then
    printf '%-8s %-10s %-10s %s\n' "$S" "${GOT}s" "OK" ""
  elif [ -z "${GOT}" ] && [ "$(lnk | cut -d' ' -f1)" != "15" ]; then
    printf '%-8s %-10s %-10s %s\n' "$S" ">60s" "超时" ""
  fi

  # 若掉卡就停，别继续（设备不可用）
  if [ "$(lnk | cut -d' ' -f1)" = "15" ] && ! alive_check; then
    echo "设备已失效，停止扫描"
    break
  fi
done

echo
echo "###### 当前守卫日志 ######"
dmesg | grep -i gen2kr | tail -15
