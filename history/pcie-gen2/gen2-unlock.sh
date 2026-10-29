#!/usr/bin/env bash
# ============================================================
#  P104-100 PCIe Gen2 解锁（Linux 版，setpci 实现）
#
#  参考：github.com/skbskhud/p104-gen2-unlock（Windows 工具）
#  原理：该卡 LnkCap 把 Max Link Speed 声明成 Gen1(1)，改声明为 Gen2(2)
#        并置 TargetLinkSpeed=Gen2，再触发链路重训练。
#        不碰 VBIOS/EEPROM，寄存器级改动，重启即恢复。
#
#  前提：显卡的 Express 能力必须是 v2（有 LnkCap2）。
#        P106-100 实测是 v1 且 LnkCap 硬只读 → 本脚本会拒绝执行。
#
#  用法：
#     sudo ./gen2-unlock.sh check    只读探测（不改任何东西）
#     sudo ./gen2-unlock.sh apply    解锁
#     sudo ./gen2-unlock.sh revert   还原
# ============================================================
set -u

TARGET_VENDOR_DEV="10de:1b87"      # P104-100
RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; CYN=$'\e[36m'; BLD=$'\e[1m'; RST=$'\e[0m'

need_root() { [ "$(id -u)" -ne 0 ] && { echo "${RED}需要 root${RST}：sudo $0 $*"; exit 1; }; }

find_dev() {
    lspci -Dn 2>/dev/null | awk -v t="$TARGET_VENDOR_DEV" 'tolower($3)==t || tolower($3)=="10de:1b87"{print $1; exit}'
}

# 读取 Express capability 基址与版本
probe() {
    local dev="$1"
    local vv; vv=$(lspci -s "$dev" -vv 2>/dev/null)
    local line; line=$(echo "$vv" | grep -m1 "Express")
    local capoff ver=""
    capoff=$(echo "$line" | grep -oE "\[[0-9a-f]{2}\]" | head -1 | tr -d '[]')
    echo "$line" | grep -q "Express (v2)" && ver="v2"
    echo "$line" | grep -q "Express (v1)" && ver="v1"
    echo "$line" | grep -q "Express (v3)" && ver="v3"
    echo "$capoff|$ver"
}

rd() { setpci -s "$1" "$2" 2>/dev/null | tail -1; }
wr() { setpci -s "$1" "$2=$3" 2>/dev/null; }

show_state() {
    local dev="$1" base=$2
    local lnkcap_lo lnkcap_hi lnksta lnkctl lnkcap2 lnkctl2
    lnkcap_lo=$(rd "$dev" "$(printf '%x' $((base+0x0c))).w")
    lnkcap_hi=$(rd "$dev" "$(printf '%x' $((base+0x0e))).w")
    lnkctl=$(rd "$dev" "$(printf '%x' $((base+0x10))).w")
    lnksta=$(rd "$dev" "$(printf '%x' $((base+0x12))).w")
    lnkcap2=$(rd "$dev" "$(printf '%x' $((base+0x2c))).l")
    lnkctl2=$(rd "$dev" "$(printf '%x' $((base+0x30))).w")

    python3 - "$lnkcap_lo" "$lnkcap_hi" "$lnksta" "$lnkctl" "$lnkcap2" "$lnkctl2" <<'PY'
import sys
lo, hi, sta, ctl, cap2, ctl2 = [int(x or 0, 16) for x in sys.argv[1:7]]
cap = (hi << 16) | lo
speed_names = {1: "2.5GT/s (Gen1)", 2: "5GT/s (Gen2)", 3: "8GT/s (Gen3)"}
print(f"  LnkCap      = 0x{cap:08x}   MaxSpeed={cap & 0xF} ({speed_names.get(cap & 0xF,'?')})  MaxWidth=x{(cap>>4)&0x3F}")
print(f"  LnkSta      = 0x{sta:04x}       CurSpeed={sta & 0xF} ({speed_names.get(sta & 0xF,'?')})  CurWidth=x{(sta>>4)&0x3F}")
print(f"  LnkCtl      = 0x{ctl:04x}")
print(f"  LnkCap2     = 0x{cap2:08x}   SupportedSpeedsVec=0x{cap2 & 0xFE:02x}" + ("  (0x02=仅Gen1, 0x06=Gen1+Gen2)" if cap2 else ""))
print(f"  LnkCtl2     = 0x{ctl2:04x}     TargetSpeed={ctl2 & 0xF}")
PY
}

do_check() {
    local dev; dev=$(find_dev)
    if [ -z "$dev" ]; then
        echo "${RED}未找到 P104-100 (10DE:1B87)${RST}"
        echo "当前 NVIDIA 设备："
        lspci -Dn 2>/dev/null | grep -i "10de:" | sed 's/^/    /'
        exit 1
    fi
    local info; info=$(probe "$dev"); local base_hex=${info%%|*} ver=${info##*|}
    local base=$((16#$base_hex))
    echo "$CYN══════════════════════════════════════════${RST}"
    echo "${BLD}  P104-100 PCIe 链路探测${RST}"
    echo "$CYN══════════════════════════════════════════${RST}"
    echo "  设备     : $dev"
    echo "  Express  : ${ver:-未知}  (cap @0x${base_hex})"
    echo
    show_state "$dev" "$base"
    echo
    if [ "${ver:-}" != "v2" ] && [ "${ver:-}" != "v3" ]; then
        echo "  ${YLW}注意：该卡是 Express ${ver:-?}，没有 LnkCap2 字段${RST}"
        echo "  ${YLW}P106-100 实测就是这种情况，LnkCap 硬只读，改不动。${RST}"
    fi
    echo "$CYN══════════════════════════════════════════${RST}"
    echo "  解锁：sudo $0 apply     还原：sudo $0 revert"
}

do_apply() {
    need_root apply
    local dev; dev=$(find_dev)
    [ -z "$dev" ] && { echo "${RED}未找到 P104-100${RST}"; exit 1; }
    local info; info=$(probe "$dev"); local base_hex=${info%%|*} ver=${info##*|}
    local base=$((16#$base_hex))

    echo "  设备: $dev  Express: ${ver:-?}  cap@0x${base_hex}"
    if [ "${ver:-}" = "v1" ]; then
        echo "${YLW}注意：lspci 报为 Express v1（无 LnkCap2 字段）${RST}"
        echo "        但 P104-100 的 0x2c 偏移实测有值（0x02），仍可尝试；"
        echo "        写后会自动读回校验，写不进就中止。"
    fi

    # 备份当前值
    local bk="/var/tmp/p104-gen2-backup-$dev.txt"
    [ -f "$bk" ] || { show_state "$dev" "$base" > "$bk"; echo "  已备份原值 → $bk"; }

    local off_cap=$((base+0x0c)) off_ctl2=$((base+0x30)) off_ctl=$((base+0x10)) off_cap2=$((base+0x2c))
    local lo hi cap speed
    lo=$(rd "$dev" "$(printf '%x' $off_cap).w"); hi=$(rd "$dev" "$(printf '%x' $((off_cap+2))).w")
    cap=$(( (16#${hi:-0} << 16) | 16#${lo:-0} ))
    speed=$(( cap & 0xF ))
    echo "  当前 LnkCap=0x$(printf '%08x' $cap)  MaxSpeed=$speed"

    if [ "$speed" -eq 2 ]; then
        echo "${YLW}已经是 Gen2 声明，无需改动${RST}"; exit 0
    fi

    echo
    echo "  [1/4] LnkCap: MaxSpeed 1 → 2"
    local newcap=$(( (cap & ~0xF) | 0x2 ))
    wr "$dev" "$(printf '%x' $off_cap).w" "$(printf '%04x' $(( newcap & 0xFFFF )))"
    local chk; chk=$(rd "$dev" "$(printf '%x' $off_cap).w")
    if [ "$((16#${chk:-0} & 0xF))" != "2" ]; then
        echo "        ${RED}✗ 写入被忽略（读回 0x$chk）——该字段只读，此卡不支持此法${RST}"
        exit 1
    fi
    echo "        ${GRN}✓${RST} 读回 0x$chk"

    echo "  [2/4] LnkCap2: SupportedSpeeds 加入 Gen2"
    wr "$dev" "$(printf '%x' $off_cap2).l" "00000006"
    echo "        读回 0x$(rd "$dev" "$(printf '%x' $off_cap2).l")"

    echo "  [3/4] LnkCtl2: TargetLinkSpeed = Gen2"
    wr "$dev" "$(printf '%x' $off_ctl2).w" "0002"
    echo "        读回 0x$(rd "$dev" "$(printf '%x' $off_ctl2).w")"

    echo "  [4/4] 触发链路重训练"
    local ctl; ctl=$(rd "$dev" "$(printf '%x' $off_ctl).w")
    local newctl=$(( (16#${ctl:-0}) | 0x20 ))
    wr "$dev" "$(printf '%x' $off_ctl).w" "$(printf '%04x' $newctl)"
    sleep 2
    echo
    show_state "$dev" "$base"
    echo
    local sta; sta=$(rd "$dev" "$(printf '%x' $((base+0x12))).w")
    if [ "$((16#${sta:-0} & 0xF))" = "2" ]; then
        echo "  ${GRN}${BLD}成功：链路已到 Gen2${RST}"
        echo "  验证：nvidia-smi --query-gpu=pcie.link.gen.current --format=csv"
    else
        echo "  ${YLW}未升到 Gen2（LnkSta 仍为 Gen$((16#${sta:-0} & 0xF))）${RST}"
        echo "  可能原因：需要同时改上游根端口、或该卡 PHY 被 VBIOS 锁死"
        echo "  还原：sudo $0 revert"
    fi
}

do_revert() {
    need_root revert
    local dev; dev=$(find_dev)
    [ -z "$dev" ] && { echo "${RED}未找到 P104-100${RST}"; exit 1; }
    local info; info=$(probe "$dev"); local base_hex=${info%%|*}
    local base=$((16#$base_hex))
    echo "  还原 $dev 到 Gen1 声明…"
    wr "$dev" "$(printf '%x' $((base+0x30))).w" "0000"
    local lo hi cap
    lo=$(rd "$dev" "$(printf '%x' $((base+0x0c))).w"); hi=$(rd "$dev" "$(printf '%x' $((base+0x0e))).w")
    cap=$(( (16#${hi:-0} << 16) | 16#${lo:-0} ))
    wr "$dev" "$(printf '%x' $((base+0x0c))).w" "$(printf '%04x' $(( (cap & ~0xF) | 1 )))"
    local ctl; ctl=$(rd "$dev" "$(printf '%x' $((base+0x10))).w")
    wr "$dev" "$(printf '%x' $((base+0x10))).w" "$(printf '%04x' $(( 16#${ctl:-0} | 0x20 )))"
    sleep 2
    show_state "$dev" "$base"
    echo "  （重启也能完全恢复，寄存器改动不持久）"
}

MODE="${1:-check}"
case "$MODE" in
    check)  do_check ;;
    apply)  do_apply ;;
    revert) do_revert ;;
    *) echo "用法: $0 {check|apply|revert}"; exit 1 ;;
esac
