#!/bin/bash
# ============================================================
# 03-verify.sh —— 入口：验证链路与带宽
#
# 实际逻辑在 check/30-verify.sh。
#   bash 03-verify.sh            基本验证（链路 + 带宽）
#   bash 03-verify.sh --full     加做「补丁独立性」验证
# ============================================================
set -u
source "$(dirname "${BASH_SOURCE[0]}")/utils/lib_v1.sh"
exec bash "${CHECK_DIR}/30-verify.sh" "$@"
