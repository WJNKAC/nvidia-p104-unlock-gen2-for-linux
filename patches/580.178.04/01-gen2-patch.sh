#!/bin/bash
# ============================================================
# 01-gen2-patch.sh —— 修补 nvidia 编译输入，让驱动不再把 PCIe 速率降回 Gen1
#
# ★ 改的是 **nv-kernel.o_binary**（编译输入），不是最终的 nvidia.ko。
#   原因：直接改 .ko 会被任何一次重编覆盖（make / dkms install / 装新内核）。
#   改编译输入后再走 04-build-nvidia.sh，产物才是"从源头正确"的。
#
# 改什么：_nv017105rm（驱动的 set_link_speed(mode)）入口的 mode 白名单
#         从 {1,2,3} 收窄成 {2,3}，让 mode==1 走原有的 bail 分支。
#         改动 2 个字节：lea 的 imm8（ff→fe）、cmp 的 imm8（02→01）。
#
# 判定方式：**只看签名命中数**，不看 md5。
#   · 原始签名 1 处 + 补丁签名 0 处  ⇒ 未打
#   · 原始签名 0 处 + 补丁签名 1 处  ⇒ 已打
#   不用 md5 是因为这个文件可能被别的补丁（如 00 白名单）先改过，
#   md5 必然对不上，拿它判定只会制造假警报。
#
# 用法：
#   sudo bash 01-gen2-patch.sh check     # 只读
#   sudo bash 01-gen2-patch.sh apply     # 打补丁（自动备份）
#   sudo bash 01-gen2-patch.sh revert    # 还原
#
# 打完必须跑 04-build-nvidia.sh 重编并安装，否则内核里跑的还是旧代码。
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/../../utils/lib_v1.sh"
ACTION="${1:-}"

# 32 字节签名（全文件唯一），决定两个待改字节的位置
GEN2_SIG_HEX="8d42ff415641554989fd41544531e4534883ed404889751089550c83f8020f87"
GEN2_OFF_LEA=2      # lea 的 imm8：ff → fe
GEN2_OFF_CMP=0x1D   # cmp 的 imm8：02 → 01
GEN2_ORIG_LEA="ff"; GEN2_NEW_LEA="fe"
GEN2_ORIG_CMP="02"; GEN2_NEW_CMP="01"

# ============================================================
# 2. 函数
# ============================================================
usage() {
    cat <<'EOF'
用法: sudo bash 01-gen2-patch.sh <动作>

动作:
  check     只读，查看是否已打补丁（看签名命中，不看 md5）
  apply     打补丁（自动备份 nv-kernel.o_binary.orig_gen2）
  revert    还原

说明:
  改 nv-kernel.o_binary 的 2 个字节。这是**编译输入**，
  改完必须跑 04-build-nvidia.sh 重编并安装，再重启/重载才生效。
EOF
}

validate() {
    if [ "$#" -gt 1 ]; then
        err "多余的参数：${*:2}"
        echo "    本脚本没有 --dry-run 之类的开关，第二个参数起一律视为错误。"
        usage
        exit 2
    fi
    case "${ACTION}" in
        check|apply|revert) ;;
        "")  usage; exit 2 ;;
        *)   err "未知动作：${ACTION}"; usage; exit 2 ;;
    esac
}

# 生成 patch_v1.py 需要的 JSON 规格
build_spec() {
    cat <<EOF
{
  "name": "gen2",
  "desc": "让驱动不再把 PCIe 速率降回 Gen1（改编译输入）",
  "path": "${NVKERNEL_BIN}",
  "anchor": {"mode": "sig", "sig": "${GEN2_SIG_HEX}"},
  "edits": [
    {"off": $(( GEN2_OFF_LEA )), "old": "${GEN2_ORIG_LEA}", "new": "${GEN2_NEW_LEA}",
     "what": "lea -0x1(%rdx),%eax -> lea -0x2(%rdx),%eax  (ff -> fe)"},
    {"off": $(( GEN2_OFF_CMP )), "old": "${GEN2_ORIG_CMP}", "new": "${GEN2_NEW_CMP}",
     "what": "cmp \$0x2,%eax -> cmp \$0x1,%eax          (02 -> 01)"}
  ],
  "after_note": "改完必须跑 04-build-nvidia.sh 重编并安装，再重启/重载才生效"
}
EOF
}

run_patch() {
    hdr "nv-kernel.o_binary（编译输入，不是最终 .ko）"
    echo "   目标符号: _nv017105rm 入口的 mode 范围检查"
    echo "   效果: mode==1（Gen1）在入口直接 bail；mode 2/3 正常；>=4 与原逻辑等价"
    echo
    python3 "${PATCH_PY}" "${ACTION}" "$(build_spec)" "$0"
    return $?
}

print_next_steps() {
    [ "${ACTION}" = "check" ] && return 0

    echo
    section "下一步（必须做，否则补丁不生效）"
    echo "   1) 重编 nvidia 并安装（这会把 o_binary 的改动变成可加载的 .ko）："
    echo "        sudo bash 04-build-nvidia.sh apply"
    echo
    echo "   2) 重载驱动或重启（内核里跑的还是旧模块）："
    echo "        sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia"
    echo "        sudo modprobe nvidia nvidia_uvm"
    echo "      或直接重启（推荐）。"
    echo
    echo "   ⚠ 重载前确认没有程序占用 GPU：fuser -v /dev/nvidia*"
    echo
    echo "   3) 链路**还不会**是 Gen2 —— 补丁只做到「不再降级」。接着跑："
    echo "        sudo bash 02-gen2-push.sh start"
}

# ============================================================
# 3. main
# ============================================================
main() {
    validate "$@"

    section "01 · 修补 nvidia 编译输入（不掉速）"

    load_offsets 2>/dev/null || true

    if [ "${ACTION}" != "check" ]; then
        need_root
        echo "   改动量：2 字节"
    fi

    if [ ! -f "${NVKERNEL_BIN}" ]; then
        err "找不到 ${NVKERNEL_BIN}"
        echo "    需要先安装 nvidia 驱动源码包：/usr/src/nvidia-${DRIVER_VER}/"
        echo "    若源码在别处：NVKERNEL_DIR=/path/to/nvidia-<ver> sudo -E bash $0 ${ACTION}"
        exit 1
    fi

    echo "   驱动版本: ${DRIVER_VER}"
    echo "   源码目录: ${NVKERNEL_DIR}"

    run_patch || exit $?

    print_next_steps
}

main "$@"
