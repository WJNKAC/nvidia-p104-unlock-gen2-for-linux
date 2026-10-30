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

  ⚠ 没有 --dry-run / --force 之类的开关。第二个参数起一律报错，
    以免把"预览"误当成"只看看"，结果真把补丁打了或还原了。
EOF
}

validate() {
    # 多余参数一律拒绝：曾经 `revert --dry-run` 被静默忽略，直接真还原了
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

# 打印表里到底有哪些设备
# 打补丁后当前值全是 0x0007ffff，光看它认不出原来是哪张卡
# ⇒ 从 .orig 备份读原始 ID 来展示
show_table() {
    local bin="${NVKERNEL_DIR}/nvidia/nv-kernel.o_binary"
    local orig="${bin}.orig"

    hdr "受限设备表里有哪些设备"

    local ids="" names="" i
    for i in "${!WHITELIST_DEV_IDS[@]}"; do
        ids+="$(printf '%04x' "${WHITELIST_DEV_IDS[$i]}") "
        names+="${WHITELIST_DEV_NAMES[$i]}|"
    done

    python3 - "$bin" "$orig" "$WHITELIST_OFFSET" "$ids" "$names" <<'PY'
import sys, struct, os

binp, origp, off, ids_s, names_s = sys.argv[1], sys.argv[2], int(sys.argv[3], 0), sys.argv[4], sys.argv[5]
ids   = [int(x, 16) for x in ids_s.split()]
names = names_s.rstrip("|").split("|")

def read5(path):
    with open(path, "rb") as f:
        f.seek(off)
        return struct.unpack("<5I", f.read(20))

cur = read5(binp)
orig = read5(origp) if os.path.exists(origp) else None

# 本机这张卡
local = None
try:
    with open("/sys/bus/pci/devices/0000:01:00.0/vendor") as f:
        v = int(f.read().strip(), 16)
    with open("/sys/bus/pci/devices/0000:01:00.0/device") as f:
        local = int(f.read().strip(), 16)
except Exception:
    pass

for i, c in enumerate(cur):
    o = orig[i] if orig else None
    oid = None
    if o is not None:
        oid = o & 0xFFFF
    else:
        oid = ids[i] if (c & 0xFFFF) == 0xFFFF else (c & 0xFFFF)

    name = names[i] if i < len(names) else "?"
    mark = ""
    if local is not None and oid == local:
        mark = "   ★ 本机这张"

    if oid == 0xFFFF:
        origtxt = "原值未知（无 .orig 备份）"
    else:
        origtxt = f"原 0x{oid:04x}"

    print(f"   0x{off + i*4:08x}  当前=0x{c:08x}   {origtxt}  {name:<9}{mark}")

print()
if orig is None:
    print("   ⚠ 没找到 .orig 备份，原始 ID 只能按 offsets.sh 的已知列表显示")
else:
    print(f"   原始值来自备份：{os.path.basename(origp)}")
    print("   打补丁后低 16 位变 0xffff（哨兵），所以当前文件里看不出是哪张卡")
PY
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
    echo "   ★ make 必须在 root 下跑（/usr/src 属主 root），否则会报"
    echo "     「cannot create conftest*.c: Permission denied」而被误认成编译器问题。"
    echo
    echo "   ★★ 编完还要同步到 DKMS —— 否则重启就被覆盖回去（最容易踩的坑）："
    echo
    echo "     sudo cp ${NVKERNEL_DIR}/nvidia.ko \\"
    echo "             /var/lib/dkms/nvidia/580.178.04/\$(uname -r)/x86_64/module/nvidia.ko"
    echo "     sudo cp ${NVKERNEL_DIR}/nvidia.ko \\"
    echo "             /lib/modules/\$(uname -r)/updates/dkms/nvidia.ko"
    echo
    echo "   为什么：启动时 modprobe 读的是 /lib/modules 那份，而 DKMS 的"
    echo "   「built」缓存在 /var/lib/dkms/... 下，是它自己 10-28 编的原始版。"
    echo "   dkms install / 装新内核时的 autoinstall 会拿缓存**覆盖**掉你编的。"
    echo "   只 make 不同步 ⇒ 重启后照旧是老版本（补丁白打）。"
    echo
    echo "   然后二选一让它生效："
    echo "     A) 重载驱动（快，会中断图形会话）"
    echo "        sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia"
    echo "        sudo modprobe nvidia && sudo modprobe nvidia_uvm nvidia_modeset nvidia_drm"
    echo "     B) 直接重启（最干净；同步过 DKMS 缓存的话重启不会丢）"
    echo
    echo "   ⚠ 重载前先确认没有程序占用 GPU：fuser -v /dev/nvidia*"
    echo "      有输出就先关掉（浏览器最常见），否则可能卡死内核。"
    echo
    echo "   ⚠ 重编会重新生成 nvidia.ko，**gen2 的 01 补丁会一起丢掉**。"
    echo "      顺序应该是：00 白名单 → make → 同步 DKMS → 01 gen2 → 同步 DKMS。"
}

# ============================================================
# 3. main
# ============================================================
main() {
    validate "$@"

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

    show_table

    print_next_steps
}

main "$@"
