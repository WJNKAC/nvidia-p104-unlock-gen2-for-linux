#!/bin/bash
# trace-mode1.sh —— 判定「mode=1 到底走哪条路、写没写 SPEED=2」
#
# ## 为什么必须重测（reports/199）
#
# reports/195 曾记录：`_nv017105rm` 内部两处 `or $0x80000`（0x127e65 / 0x12803b）
# 在 minictx 时**各 0 次命中**，据此推断"SPEED 变 2 是端点内部回写，CPU 不可见"。
#
# 但本轮 perf 已确证：`cuInit` 每次都以 **DX=0x1（mode=1）** 进入 `_nv017105rm`。
# 静态看 mode=1 是**顺序落入** → 必然经过 127e65 的 `or $0x80000`。
# 两个结论冲突，必须现场判定。
#
# ## 静态路径（已核对）
#   entry 127d60
#   127d81 cmp $0x2,%eax ; ja 1280c8      ; (mode-1)>2 → bail（mode ∉{1,2,3}）
#   127d8a cmp $0x2,%edx ; je 1280d9      ; mode==2 → 能力门禁 obj[0x954]
#   127d93 cmpl $0x3,0xc(%rbp) ; je 128169 ; mode==3 → 能力门禁 obj[0x955]
#   127d9d ← mode==1 顺序落入
#   127ded cmpb $0,0x4de5(%r13) ; jne 1281a8   ; 只是**打警告日志**，之后 jmp 127dfb 继续
#   127dfb..127e49  循环读 0x8C040，直到 (al & 0x1f)==0
#   127e4b mov 0xc(%rbp),%ecx     ; ecx = mode
#   127e4e and $0xfff3ffff,%eax    ; 清 SPEED[19:18]
#   127e53 cmp $0x2,%ecx ; je 12819b   → or $0x40000  (SPEED=1, Gen2)
#   127e5c cmp $0x3,%ecx ; je 12817f   → 保持 eax    (SPEED=0, Gen3)
#   127e65 or  $0x80000,%eax           → SPEED=2 (Gen1)  ★ mode==1 落在这里
#   127e6a movl $0x1,0xc(%rbp)         ; ★★ 把存储的 mode 改成 1
#   127e71 mov %eax,0x20(%rbp)         ; 待写值
#   127edd.. 写 0x8c040
#   1280c8 ← 唯一的 return 路径（mov %r12d,%eax; pop×4; add $0x40,%rbp; ret）
#
# ## x86 只有 4 个调试寄存器 ⇒ 最多 4 个执行断点
#
# 用法：bash trace-mode1.sh
set -u
D=/data/labs/nvidia/pcie-gen2

declare -A OFF=(
  [entry]=0x127d60
  [or80000]=0x127e65      # mode==1 的 SPEED=2
  [wr_shared]=0x127edd    # 所有 mode 汇合后的写点
  [bailret]=0x1280c8      # 唯一 return
  [warnpath]=0x1281a8     # obj[0x4de5]!=0 时的警告日志分支
)
BASE=$(cat /sys/module/nvidia/sections/.text)
addr() { python3 -c "print('0x%x' % (int('$BASE',16) + ${OFF[$1]}))"; }

echo "=== 探针（.text 基址 $BASE）==="
for k in entry or80000 wr_shared bailret; do
  printf '  %-10s %s\n' "$k" "$(addr $k)"
done

A_E=$(addr entry); A_O=$(addr or80000); A_W=$(addr wr_shared); A_B=$(addr bailret)
OUT=/tmp/mode1.data
rm -f "$OUT"

echo
echo "=== 跑 1 轮 minictx（新建 1 个 CUDA 上下文 = 1 次 cuInit）==="
perf record -e "mem:$A_E:x" -e "mem:$A_O:x" -e "mem:$A_W:x" -e "mem:$A_B:x" \
  -a --intr-regs=ip,dx,si,di,r8,ax -o "$OUT" -- "$D/minictx" 1 2>&1 | tail -2

echo
echo "=== 各断点命中次数 ==="
for k in entry or80000 wr_shared bailret; do
  A=$(addr $k)
  C=$(perf script -i "$OUT" 2>/dev/null | grep -c "mem:$A:x" || true)
  printf '  %-10s %-14s %s 次\n' "$k" "$A" "$C"
done

echo
echo "=== entry 的 DX（mode）==="
perf script -i "$OUT" -F iregs 2>/dev/null | grep "IP:$A_E" \
  | grep -oE 'DX:0x[0-9a-f]+' | sort | uniq -c

echo
echo "=== ★ or\\$0x80000 命中时的 AX（= 即将置位前的值）==="
perf script -i "$OUT" -F iregs 2>/dev/null | grep "IP:$A_O" \
  | grep -oE 'AX:0x[0-9a-f]+|R8:0x[0-9a-f]+' | sort | uniq -c | head

echo
echo "=== 共享写点的 R8（= 实际准备写入 0x8C040 的值）==="
perf script -i "$OUT" -F iregs 2>/dev/null | grep "IP:$A_W" \
  | grep -oE 'R8:0x[0-9a-f]+' | sort | uniq -c | head

echo
echo "=== 时间序（谁先谁后）==="
perf script -i "$OUT" -F time,ip,sym 2>/dev/null | head -20

echo
echo "=== 判定 ==="
if [ "$(perf script -i "$OUT" 2>/dev/null | grep -c "mem:$A_O:x" || true)" -gt 0 ]; then
  echo "  ★ mode=1 **确实**执行了 or \$0x80000 ⇒ SPEED 是被软件写成 2 的"
  echo "    ⇒ 补丁思路成立：拦住 mode==1 就能阻止降级"
  echo "    ⇒ 同时说明 reports/195 的『端点内部回写』结论错误（那次断点没生效）"
else
  echo "  mode=1 没有执行到 or \$0x80000 ⇒ 走了别的分支，需看 warnpath/wr_shared"
fi
