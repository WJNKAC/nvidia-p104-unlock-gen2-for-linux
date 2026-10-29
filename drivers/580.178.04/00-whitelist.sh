#!/bin/bash
# ============================================================
# 00-whitelist.sh —— 【前置，可选】解除驱动的「受限 SKU」判定
#
# 这一步可以不做。做与不做只影响「帧率/功耗」那个问题，不影响 PCIe Gen2。
#
# 改什么：nv-kernel.o_binary 里的「受限设备表」，把 P1XX 的 device ID
#         替换成 0xFFFF（哨兵），等于把卡从表里抹掉。
#
# ⚠ 改完必须重建内核模块：cd /usr/src/nvidia-580.178.04 && sudo make
#   然后重载驱动或重启（脚本会打印命令）。
#
# 用法：
#   sudo bash 00-whitelist.sh check      # 只读，看当前状态
#   sudo bash 00-whitelist.sh apply      # 打补丁
#   sudo bash 00-whitelist.sh revert     # 还原
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

ACTION="${1:-}"

# ============================================================
# 2. 函数
# ============================================================

usage() {
    cat <<'EOF'
用法: sudo bash 00-whitelist.sh <动作>

动作:
  check     只读，查看当前是否已打补丁
  apply     打补丁（自动备份 .orig）
  revert    还原

说明:
  这一步【可选】。它解决的是"帧率卡 ~40fps / 功耗上不去"，
  与 PCIe Gen2 解锁无关。只想要带宽的话跳过本步，直接跑 01。
EOF
}

validate() {
    case "${ACTION}" in
        check|apply|revert) ;;
        "")  usage; exit 2 ;;
        *)   err "未知动作：${ACTION}"; usage; exit 2 ;;
    esac
}

# 生成 _patch.py 需要的 JSON 规格
build_spec() {
    local edits="" i off dev
    for i in "${!WHITELIST_DEV_IDS[@]}"; do
        dev="${WHITELIST_DEV_IDS[$i]}"
        off=$(( WHITELIST_OFFSET + i * 4 ))
        # 只改低 16 位（2 字节），保留 0x0007 前缀
        edits+=$(printf '{"off": %d, "old": "%02x%02x", "new": "ffff", "what": "device 0x%04x -> 0xffff（抹掉）"},' \
                 "${off}" $(( dev & 0xff )) $(( (dev >> 8) & 0xff )) "${dev}")
    done
    edits="${edits%,}"

    cat <<EOF
{
  "name": "whitelist",
  "desc": "从内核受限设备表里抹掉 P1XX 的 device ID",
  "path": "${NVKERNEL_DIR}/nvidia/nv-kernel.o_binary",
  "anchor": {"mode": "offset", "off": 0},
  "edits": [${edits}],
  "md5_before": "${WHITELIST_MD5_BEFORE}",
  "md5_after": "${WHITELIST_MD5_AFTER}",
  "after_note": "改完必须重建内核模块（见下方下一步），否则改动不生效"
}
EOF
}

# 调用通用补丁器
run_patch() {
    hdr "受限设备表（$(basename "${NVKERNEL_DIR}")/nvidia/nv-kernel.o_binary）"
    python3 "${PATCH_PY}" "${ACTION}" "$(build_spec)" "$0"
    return $?
}

# apply/revert 之后打印下一步该做什么
print_next_steps() {
    [ "${ACTION}" = "check" ] && return 0

    echo
    section "下一步（必须做，否则改动不生效）"
    echo "   改的是 nv-kernel.o_binary（编译输入），不是最终模块。必须重建："
    echo
    echo "     cd ${NVKERNEL_DIR}"
    echo "     sudo make -j\$(nproc)"
    echo
    echo "   然后二选一："
    echo "     A) 重载驱动（快，会中断图形会话）"
    echo "        sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia"
    echo "        sudo modprobe nvidia nvidia_uvm"
    echo "     B) 直接重启（推荐，最干净）"
    echo
    echo "   ⚠ 重载前先确认没有程序占用 GPU：fuser -v /dev/nvidia*"
    echo "      有输出就先关掉（浏览器最常见），否则可能卡死内核。"
}

# ============================================================
# 3. main
# ============================================================
main() {
    validate

    section "00 · 解除受限 SKU 判定（可选）"

    load_offsets

    if [ "${ACTION}" != "check" ]; then
        need_root "${ACTION}"
        echo "   改动量：${#WHITELIST_DEV_IDS[@]} 处 device ID → 0xFFFF"
    fi

    if [ ! -f "${NVKERNEL_DIR}/nvidia/nv-kernel.o_binary" ]; then
        err "找不到 ${NVKERNEL_DIR}/nvidia/nv-kernel.o_binary"
        echo "    这个补丁只适用于驱动装在 /usr/src/nvidia-<版本>/ 的情况。"
        echo "    若你的驱动用 .run 安装且未保留源码树，本步跳过即可（它不影响 Gen2）。"
        exit 1
    fi

    run_patch || exit $?

    print_next_steps
}

main "$@"
