#!/bin/bash
# ============================================================
# utils/bdf.sh —— 目标 GPU 的 PCIe BDF 自动发现（**单一来源**）
#
# 被 utils/lib_v1.sh 和 utils/lib_v1.sh source。
#
# 为什么必须自动发现：
#   硬编码 01:00.0 时，多卡机上会**对着错卡读写**。同机还有一张已满速 Gen3 的
#   P100 时，脚本会读出 CAP2=0x0006 / TLS=2 / LNKSTA 恒 1041 —— 看起来像"锁死"，
#   实际是设备选错了；而 10-check 的设备检查用的是另一个来源，就会出现
#   "第 2 节说 P104 在总线、第 3 节却显示 P100 的 cfg"这种自相矛盾。
#
# 环境变量：
#   TARGET_IDS   目标 device id，空格分隔（默认 "1b87" = P104-100）
#                同 family 受限矿卡：1b07 P102-100 / 1b87 P104-100 /
#                1bc7 P104-101 / 1c07 P106-100 / 1c09 P106-090
#   TARGET_BDF   显式指定 BDF，跳过自动发现（多卡同型时必须用）
#
# 导出：DEV_BDF（0000:01:00.0） DEV_SHORT（01:00.0） ROOT_BDF（上游根端口）
#
# 用法：
#   source utils/bdf.sh              # 通常由 _lib.sh 代劳
#   bash utils/bdf.sh                # 单独跑：只打印发现的 BDF
# ============================================================

if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
    # 被 source：重复 source 直接返回
    [ -n "${_BDF_SH_LOADED:-}" ] && return 0
    _BDF_SH_LOADED=1
fi

_bdf_err() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; }

# 扫 /sys/bus/pci/devices，按 vendor:device 找目标卡
_bdf_scan() {
    local d vid did out=""
    for d in /sys/bus/pci/devices/*; do
        [ -r "${d}/vendor" ] || continue
        vid="$(cat "${d}/vendor" 2>/dev/null)" || continue
        [ "${vid}" = "0x10de" ] || continue
        did="$(cat "${d}/device" 2>/dev/null)"
        case " ${TARGET_IDS} " in
            *" ${did#0x} "*) out="${out} $(basename "${d}")" ;;
        esac
    done
    printf '%s' "${out# }"
}

TARGET_IDS="${TARGET_IDS:-1b87}"

if [ -n "${TARGET_BDF:-}" ]; then
    if [ ! -d "/sys/bus/pci/devices/${TARGET_BDF}" ]; then
        _bdf_err "TARGET_BDF=${TARGET_BDF} 不存在"
        echo "    列出所有 NVIDIA 设备：lspci -Dn | grep 10de" >&2
        exit 3
    fi
    DEV_BDF="${TARGET_BDF}"
else
    _bdf_cands="$(_bdf_scan)"
    if [ -z "${_bdf_cands}" ]; then
        _bdf_n=0
    else
        _bdf_n=$(printf '%s\n' ${_bdf_cands} | wc -l)
    fi

    if [ "${_bdf_n}" -eq 0 ]; then
        _bdf_err "找不到目标设备（10de:${TARGET_IDS// /,}）"
        echo "    本机所有 NVIDIA 设备：" >&2
        lspci -Dn 2>/dev/null | grep -i ' 10de:' | sed 's/^/      /' >&2
        echo "    若 device id 不同，用：TARGET_IDS=\"1b87 1c07\" sudo -E bash \$0" >&2
        exit 3
    elif [ "${_bdf_n}" -gt 1 ]; then
        _bdf_err "找到 ${_bdf_n} 张目标设备，无法自动选择：${_bdf_cands}"
        echo "    必须显式指定，否则可能操作错卡（会破坏另一张卡的链路）：" >&2
        echo "        TARGET_BDF=0000:02:00.0 sudo -E bash \$0" >&2
        exit 3
    fi
    DEV_BDF="${_bdf_cands}"
fi

DEV_SHORT="${DEV_BDF#0000:}"
# 上游根端口：用 readlink 解析出父设备，不再假设 00:01.0
ROOT_BDF="$(basename "$(dirname "$(readlink -f "/sys/bus/pci/devices/${DEV_BDF}")")")"

export DEV_BDF DEV_SHORT ROOT_BDF TARGET_IDS

# 直接执行时打印结果
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    echo "DEV_BDF   = ${DEV_BDF}"
    echo "DEV_SHORT = ${DEV_SHORT}"
    echo "ROOT_BDF  = ${ROOT_BDF}"
fi
