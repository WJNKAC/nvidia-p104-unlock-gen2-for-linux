#!/bin/bash
# ============================================================
# 01-gen2-patch.sh —— 修补 nvidia.ko：让驱动不再把 PCIe 速率降回 Gen1
#
# 改什么：_nv017105rm（驱动的 set_link_speed(mode)）入口的 mode 白名单
#         从 {1,2,3} 收窄成 {2,3}，让 mode==1 走原有的 bail 分支。
#         改动 2 个字节：lea 的 imm8（ff→fe）、cmp 的 imm8（02→01）。
#
# ⚠ 这一步只做了一半：改完驱动不再降级，但**链路还没升上去**。
#   升上去要靠 02-gen2-push.sh。
#
# ⚠ 改完必须重载 nvidia 模块（或重启）才生效（脚本会打印命令）。
#
# 用法：
#   sudo bash 01-gen2-patch.sh check     # 只读，看当前状态
#   sudo bash 01-gen2-patch.sh apply     # 打补丁
#   sudo bash 01-gen2-patch.sh revert    # 还原
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
用法: sudo bash 01-gen2-patch.sh <动作>

动作:
  check     只读，查看当前是否已打补丁
  apply     打补丁（自动备份 .orig_nogen1）
  revert    还原

说明:
  改的是 nvidia.ko 的 2 个字节，让驱动不再把链路降回 Gen1。
  改完必须重载模块/重启，然后再跑 02-gen2-push.sh 把链路推上去。
EOF
}

validate() {
    # 多余参数一律拒绝：避免 `revert --dry-run` 这类"想预览"结果真执行
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

# 生成 _patch.py 需要的 JSON 规格
#
# 用 32 字节签名定位（全文件唯一），不依赖绝对偏移：
#   8d42ff 4156 4155 4989fd 4154 4531e4 53 4883ed40 48897510 89550c 83f802 0f87
# 签名内两个位置要改：索引 0x02（ff→fe）、索引 0x1D（02→01）
build_spec() {
    cat <<EOF
{
  "name": "gen2",
  "desc": "让驱动不再把 PCIe 速率降回 Gen1",
  "path": "${KO}",
  "anchor": {"mode": "sig", "sig": "${GEN2_SIG_HEX}"},
  "edits": [
    {"off": $(( GEN2_OFF_LEA )), "old": "${GEN2_ORIG_LEA}", "new": "${GEN2_NEW_LEA}",
     "what": "lea -0x1(%rdx),%eax -> lea -0x2(%rdx),%eax  (ff -> fe)"},
    {"off": $(( GEN2_OFF_CMP )), "old": "${GEN2_ORIG_CMP}", "new": "${GEN2_NEW_CMP}",
     "what": "cmp \$0x2,%eax -> cmp \$0x1,%eax          (02 -> 01)"}
  ],
  "md5_before": "${GEN2_MD5_BEFORE}",
  "md5_after": "${GEN2_MD5_AFTER}",
  "after_note": "改完必须重载 nvidia 模块（或重启）才生效；链路还要靠 02-gen2-push.sh 推上去"
}
EOF
}

# 调用通用补丁器
run_patch() {
    hdr "nvidia.ko（$(basename "${KO}")）"
    echo "   目标符号: _nv017105rm 入口的 mode 范围检查"
    echo "   效果: mode==1（Gen1）在入口直接 bail；mode 2/3 正常；>=4 与原逻辑等价"
    echo
    python3 "${PATCH_PY}" "${ACTION}" "$(build_spec)" "$0"
    return $?
}

# apply/revert 之后打印下一步该做什么
print_next_steps() {
    [ "${ACTION}" = "check" ] && return 0

    echo
    section "下一步（必须做，否则补丁不生效）"
    echo "   改的是磁盘上的 nvidia.ko，内核里跑的还是旧代码。二选一："
    echo
    echo "     A) 重载模块（快，会中断图形会话）"
    echo "        sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia"
    echo "        sudo modprobe nvidia nvidia_uvm"
    echo "     B) 直接重启（推荐，最干净）"
    echo
    echo "   ⚠ 重载前先确认没有程序占用 GPU：fuser -v /dev/nvidia*"
    echo "      有输出就先关掉（浏览器最常见），否则可能卡死内核。"
    echo
    echo "   重载/重启完成后，链路**还不会**是 Gen2 —— 补丁只做到「不再降级」。"
    echo "   接着跑: sudo bash 02-gen2-push.sh start"
}

# ============================================================
# 3. main
# ============================================================
main() {
    validate "$@"

    section "01 · 修补 nvidia.ko（不掉速）"

    load_offsets

    if [ "${ACTION}" != "check" ]; then
        need_root "${ACTION}"
        echo "   改动量：2 字节"
    fi

    if [ ! -f "${KO}" ]; then
        err "找不到 ${KO}"
        echo "    若驱动装在别处，请改 _lib.sh 里的 KO"
        exit 1
    fi

    echo "   驱动版本: ${DRIVER_VERSION}（本目录只对这个版本做过验证）"

    run_patch || exit $?

    print_next_steps
}

main "$@"
