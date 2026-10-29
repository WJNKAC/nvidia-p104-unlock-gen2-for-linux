#!/bin/bash
# probe-lock.sh —— 判定 SPEED 锁定环 `_nv017281rm` 到底会不会执行
#
# 背景（reports/192-194）：
#   `_nv017281rm` 内含"读 0x8C040 → 取 LNKSTA → 按当前速率回写 SPEED"的锁定环，
#   是"链路被打回 Gen1 后自己锁死"的疑似元凶。但 reports/194 证明：
#   静态零引用不能判死代码（RM 靠对象槽间接调用），**只能动态测**。
#
# 本脚本做三件事：
#   1. 算出各关键函数的运行时地址（.text 基址 + 节内偏移）
#   2. 用 kallsyms / available_filter_functions 校验可插桩性（notrace 会在这里暴露）
#   3. 提供 perf 执行断点命令模板（不受 notrace 限制，是唯一可靠手段）
#
# 用法：bash probe-lock.sh [info|bpftrace|perf]
set -u

# 节内偏移（来自 reports/191-194 的静态分析，kernel 6.12.113 上已验证未变）
declare -A OFF=(
  [_nv017281rm]=0x1269f0     # 锁定环所在（链路异常恢复）
  [_nv017282rm]=0x12d2f0     # 它的直接调用者
  [_nv005948rm]=0xb3a240     # HAL 安装器（装 6 个 0x8C040 访问函数）
  [_nv053907rm]=0x5ffda0     # 虚分派【读】
  [_nv053912rm]=0x5ff6c0     # 带重试循环的【写】
)

BASE=$(cat /sys/module/nvidia/sections/.text 2>/dev/null || true)
if [ -z "$BASE" ]; then
  echo "✗ 读不到 .text 基址（驱动未加载？或需要 root）"
  exit 1
fi

case "${1:-info}" in
info)
  echo "=== .text 基址: $BASE ==="
  printf '%-16s %-10s %s\n' 函数 节内偏移 运行时地址
  for n in _nv017281rm _nv017282rm _nv005948rm _nv053907rm _nv053912rm; do
    printf '%-16s %-10s 0x%x\n' "$n" "${OFF[$n]}" $(( BASE + ${OFF[$n]} ))
  done

  echo
  echo "=== kallsyms 里的名字（LOCAL 符号可能带后缀或被折叠）==="
  grep -iE "_nv017281|_nv017282|_nv005948|_nv053907|_nv053912" /proc/kallsyms | head -10 \
    || echo "  未出现在 kallsyms（LOCAL 符号常见，不代表不可插桩）"

  echo
  echo "=== 可追踪性（bpftrace/kprobe 会拒绝 notrace 函数）==="
  AF=/sys/kernel/debug/tracing/available_filter_functions
  if [ -r "$AF" ]; then
    for n in _nv017281rm _nv053907rm _nv053912rm; do
      if grep -q "^$n\b" "$AF"; then echo "  ✓ $n 可 kprobe"; else echo "  ✗ $n 不可 kprobe（notrace 或缺符号）"; fi
    done
  else
    echo "  $AF 不可读，用 bpftrace 直接试"
  fi
  ;;

bpftrace)
  # ★ 纪律（reports/187）：挂 kprobe 前必须先用 bpftrace 验证，否则可能 int3 破坏执行
  for n in _nv017281rm _nv053907rm _nv053912rm; do
    echo "--- 验证 $n ---"
    timeout 8 bpftrace -e "kprobe:$n { @hits = count(); } interval:s:3 { exit(); }" 2>&1 | tail -6
    echo
  done
  ;;

perf)
  # perf 硬件断点不受 notrace 限制（reports/191 的核心发现）
  # -e mem:ADDR:x  执行断点；--intr-regs 抓寄存器；-g 抓栈
  A=$(( BASE + ${OFF[_nv017281rm]} ))
  B=$(( BASE + ${OFF[_nv053907rm]} ))
  echo "=== A. 执行断点：_nv017281rm (0x$(printf %x $A)) ==="
  echo "  perf record -e mem:0x$(printf %x $A):x -a --intr-regs=ip,sp -o /tmp/lock.data -- sleep 30"
  echo "  perf script -i /tmp/lock.data | head -50"
  echo
  echo "=== B. 函数计数：用 kprobe（若能过 bpftrace）==="
  echo "  perf probe -x $BASE --add '_nv017281rm=_nv017281rm'  # 可能失败(notrace)"
  echo
  echo "=== C. 关键：_nv053907rm 的 CX 是否是 0x8C040（谁在读 SPEED）==="
  echo "  perf record -e mem:0x$(printf %x $B):x -a --intr-regs=cx,dx,si,ip -o /tmp/rd.data -- sleep 30"
  echo "  perf script -i /tmp/rd.data -F comm,iregs | head -50"
  ;;

*)
  echo "用法: bash probe-lock.sh [info|bpftrace|perf]"
  exit 2
  ;;
esac
