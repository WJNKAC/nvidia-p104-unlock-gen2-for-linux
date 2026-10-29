#!/bin/bash
# trace-downgrade.sh —— 抓「Gen2 → Gen1 掉速」瞬间，判定是软件改写还是硬件降级
#
# 背景（reports/195）
#   · 补丁已验证生效：`_nv017281rm` 把 SPEED 写成 1（5.0GT/s）而非 2（2.5GT/s）
#   · 声明解锁 + 根端口 Retrain 后，链路确实升到 Gen2（LNKSTA=0x1042, lspci 5GT/s）
#   · 但跑一次新 CUDA 上下文，链路又回到 Gen1（lspci 标注 "downgraded"）
#
# 两种可能的机制，本脚本用**高频采样**区分：
#   A. 软件把 0x8C040.SPEED 改回 2  → 采样会看到 0x8C040 的写变化
#   B. 硬件协商失败后自行降级      → 0x8C040 保持不变（仍=SPEED=1），只有 LNKSTA 变
#
#   ★ 这个区分决定补丁路线是否还有意义：
#     A ⇒ 继续找第二个写入者并 patch 它
#     B ⇒ 软件侧已无可做，瓶颈在 GPU 内部数据面（SKP OS / credits 仍按 Gen1 配）
#
# 用法：bash trace-downgrade.sh
set -u
DEV=0000:01:00.0
D=/data/labs/nvidia/pcie-gen2

echo "=== 1. 确保在 Gen2（解锁声明 + 根端口重训）==="
python3 $D/tools/unlock-decl.py --retrain 2>&1 | tail -6
echo
echo -n "推完后: "; lspci -vv -s $DEV 2>/dev/null | grep -o 'LnkSta:.*' | head -1

if ! lspci -vv -s $DEV 2>/dev/null | grep -q 'Speed 5GT/s'; then
  echo "✗ 没能推到 Gen2，终止（后续采样无意义）"
  exit 2
fi

echo
echo "=== 2. 从 Gen2 起点采样，同时新建 1 个 CUDA 上下文 ==="
# watch_reg 会打印起点/终点/变化明细；这里只跑 1 轮 ctx，尽量缩短窗口
python3 $D/tools/watch_reg.py 10 --during "$D/minictx 1" 2>&1
