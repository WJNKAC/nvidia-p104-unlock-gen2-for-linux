#!/bin/bash
# soak.sh —— 长跑稳定性验证（默认 30 分钟）
#
# 目的（reports/197 §8 待办）
#   守卫已验证单次恢复 3.84s、带宽 1.56/1.71。但"连续几十次降级-恢复后是否退化"
#   没测过。本脚本循环做：
#       新建 CUDA 上下文（触发降级）→ 等恢复 → 记录耗时 → 定期测带宽
#   并在每轮检查：
#       · LNKSTA 是否真的回到 Gen2
#       · 设备是否仍应答（连续 20 次 0xffffffff 才判死）
#       · 是否新出现 Xid
#       · 恢复耗时是否随轮次增长（退化趋势）
#
# ★ 只用只读 MMIO + gen2kr 的内部动作，**不做任何用户态 MMIO 写**
#   （reports/197：用户态写 0x8C040 在链路动态时 2/2 打挂设备）
#
# 用法：bash soak.sh [分钟数] [每轮间隔秒]
# 日志：/tmp/log-soak-<时间>.log（原因见下方注释）
set -u
MIN="${1:-30}"
GAP="${2:-3}"
D="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
# ★ 日志默认写 /tmp：本脚本用 root 跑（读 /dev/mem），若仓库在 NFS 上
#   （root_squash），root 写不进仓库目录。跑完把日志拷回 logs/ 即可。
LOGF="/tmp/log-soak-$(date +%Y%m%d-%H%M%S).log"

lnk() {
python3 - <<'PY'
import mmap,os,struct
try:
    p=int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0],16)
    f=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
    m=mmap.mmap(f,0x1000000,mmap.MAP_SHARED,mmap.PROT_READ,offset=p)
    v=struct.unpack_from('<I',m,0x8C040)[0]
    l=struct.unpack_from('<I',m,0x88088)[0]
    c=struct.unpack_from('<I',m,0x880A4)[0]
    m.close(); os.close(f)
    print((l>>16)&0xF, (v>>18)&3, f"{c:04x}")
except Exception:
    print("15 3 dead")
PY
}

# 连续 20 次 0xffffffff 才判死（重训瞬态是常态）
alive() {
  local bad=0
  for i in $(seq 1 20); do
    read -r SP _ _ <<<"$(lnk)"
    if [ "$SP" = "15" ]; then bad=$((bad+1)); else return 0; fi
    sleep 0.05
  done
  return 1
}

log() { echo "$*" | tee -a "$LOGF"; }

log "================ soak 开始 $(date '+%F %T') ================"
log "时长 ${MIN} 分钟，每轮间隔 ${GAP}s"
log "内核: $(uname -r)   gen2kr 参数:"
for p in stage_gap_ms debounce_cnt retrain_min_ms speed_settle_ms guard_period_ms allow_busy_retrain; do
  log "  $p = $(cat /sys/module/gen2kr/parameters/$p 2>/dev/null || echo N/A)"
done
log ""
printf '%-5s %-9s %-8s %-7s %-6s %-6s %s\n' 轮次 时刻 恢复耗时 链路 SPEED LCAP2 Xid | tee -a "$LOGF"

XID0=$(dmesg | grep -c "Xid" || true)
log "起始 Xid 计数: $XID0"
log ""

END=$(( $(date +%s) + MIN * 60 ))
N=0
FAIL=0
MAXT=0
SUMT=0

while [ "$(date +%s)" -lt "$END" ]; do
  N=$((N + 1))

  # 起点必须是 Gen2，否则先等
  ready=0
  for i in $(seq 1 40); do
    read -r SP _ _ <<<"$(lnk)"
    [ "$SP" = "2" ] && { ready=1; break; }
    sleep 0.25
  done
  if [ "$ready" = "0" ]; then
    log "$(printf '%-5s %-9s %-8s %-7s' "$N" "$(date +%H:%M:%S)" "-" "起点非Gen2")"
    FAIL=$((FAIL + 1))
    if ! alive; then log "  ★★ 设备失联，终止长跑"; break; fi
    sleep "$GAP"; continue
  fi

  # 触发降级并计时
  T0=$(date +%s.%N)
  "$D/build/minictx" 1 >/dev/null 2>&1
  GOT=""
  for i in $(seq 1 250); do    # 最多 50s
    read -r SP SD CP <<<"$(lnk)"
    if [ "$SP" = "2" ]; then
      GOT=$(python3 -c "print(f'{$(date +%s.%N)-$T0:.2f}')")
      break
    fi
    if [ "$SP" = "15" ]; then
      if ! alive; then
        log "  ★★ 第 $N 轮设备失联（连续 20 次 ffffffff），终止"
        GOT="DEAD"; break
      fi
    fi
    sleep 0.2
  done

  XID=$(dmesg | grep -c "Xid" || true)
  XIDN=$((XID - XID0))

  if [ "$GOT" = "DEAD" ]; then break; fi
  if [ -z "$GOT" ]; then
    log "$(printf '%-5s %-9s %-8s %-7s %-6s %-6s %s' "$N" "$(date +%H:%M:%S)" ">50s" "$SP" "$SD" "$CP" "$XIDN")"
    FAIL=$((FAIL + 1))
  else
    log "$(printf '%-5s %-9s %-8s %-7s %-6s %-6s %s' "$N" "$(date +%H:%M:%S)" "${GOT}s" "$SP" "$SD" "$CP" "$XIDN")"
    SUMT=$(python3 -c "print($SUMT + $GOT)")
    MAXT=$(python3 -c "print(max($MAXT, $GOT))")
  fi

  # 每 10 轮测一次带宽（用 256MB，比 128 更不容易落在恢复窗口）
  if [ $((N % 10)) -eq 0 ]; then
    for i in $(seq 1 40); do
      read -r SP _ _ <<<"$(lnk)"
      [ "$SP" = "2" ] && break
      sleep 0.25
    done
    BW=$("$D/build/bwtest2" 256 8 2>&1 | grep -E "pinned" | head -1)
    log "      [第 $N 轮带宽] $BW"
  fi

  sleep "$GAP"
done

log ""
log "================ 汇总 ================"
OK=$((N - FAIL))
log "总轮次 $N   成功 $OK   失败 $FAIL"
if [ "$OK" -gt 0 ]; then
  log "平均恢复 $(python3 -c "print(f'{$SUMT/$OK:.2f}')")s   最慢 ${MAXT}s"
fi
XIDF=$(dmesg | grep -c "Xid" || true)
log "Xid: 起始 $XID0 → 结束 $XIDF（新增 $((XIDF - XID0))）"
read -r SP SD CP <<<"$(lnk)"
log "最终: LNKSTA[3:0]=$SP  SPEED=$SD  LCAP2=$CP"
log "结束于 $(date '+%F %T')"
log "日志: $LOGF"
