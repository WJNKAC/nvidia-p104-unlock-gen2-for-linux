#!/bin/bash
# _common.sh —— 公共路径、寄存器读取与判定
# 所有脚本 source 这个文件，保证包可整体搬移。
#
# 设计约束（踩过的坑）：
#   · 不用 $(( )) 处理 0xffffffffc185a000（dash 报 Illegal number，>2^63）
#   · 地址一律交给 python 算
#   · 读 /dev/mem 必须 O_RDWR（O_RDONLY + PROT_WRITE 会 PermissionError）

PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PKG_ROOT/build"
SRC_DIR="$PKG_ROOT/src"
KMOD_DIR="$PKG_ROOT/kmod"
LOG_DIR="$PKG_ROOT/logs"

KVER="$(uname -r)"
KO="/lib/modules/${KVER}/updates/dkms/nvidia.ko"

DEV_BDF="0000:01:00.0"
DEV_SHORT="01:00.0"
ROOT_BDF="00:01.0"

# BAR0 寄存器（只读用途）
R_LNKCAP=0x88084
R_LNKCAP2=0x880A4
R_LNKSTA=0x88088
R_LNKCTL2=0x880A8
R_CONFIG=0x8841C
R_LTSSM=0x8872C
R_LINKCFG0=0x8C040
R_CYA=0x8C2C0

# ---------- 颜色（非 tty 自动关闭） ----------
if [ -t 1 ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
    C_R=; C_G=; C_Y=; C_B=; C_0=
fi
ok()   { echo "${C_G}✓${C_0} $*"; }
warn() { echo "${C_Y}⚠${C_0} $*"; }
err()  { echo "${C_R}✗${C_0} $*"; }
hdr()  { echo; echo "${C_B}=== $* ===${C_0}"; }

# ---------- 只读：读 BAR0 一组寄存器 ----------
# 输出一行：CAP2=0x0006 LNKSTA=2 TLS=2 SPEED=1 MAXSPD=2
read_regs() {
python3 - <<'PY'
import mmap, os, struct, sys
try:
    p = int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0], 16)
    f = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    g = lambda o: struct.unpack_from('<I', m, o)[0]
    cap  = g(0x88084); cap2 = g(0x880A4)
    lsta = g(0x88088); ctl2 = g(0x880A8); cfg = g(0x8C040)
    m.close(); os.close(f)
    print(f"CAP2=0x{cap2:04x} LNKSTA={(lsta>>16)&0xF} TLS={ctl2&0xF} "
          f"SPEED={(cfg>>18)&3} MAXSPD={cap&0xF}")
except Exception:
    print("CAP2=? LNKSTA=? TLS=? SPEED=? MAXSPD=?")
PY
}

# 只取 LNKSTA[3:0]（唯一可信的"当前速率"判据）
cur_speed() {
    local s
    s="$(_r 0x88088)"
    python3 -c "print((${s:-0}>>16)&0xF)" 2>/dev/null || echo 0
}

# 只取 CAP2（声明向量，0x06 = 已解锁支持 Gen2）
cap2() {
    local v
    v="$(_r 0x880A4)"
    echo "${v:-0}"
}

_r() {
python3 - "$1" <<'PY'
import mmap, os, struct, sys
off = int(sys.argv[1], 16)
try:
    p = int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0], 16)
    f = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    print(struct.unpack_from('<I', m, off)[0])
    m.close(); os.close(f)
except Exception:
    print(0)
PY
}

# 判活：连续 20 次 0xffffffff 才算失效（单次是重训瞬态，不是异常）
alive() {
    local bad=0 i v
    for i in $(seq 1 20); do
        v="$(cap2)"
        if [ "$v" = "4294967295" ]; then
            bad=$((bad + 1)); sleep 0.05
        else
            return 0
        fi
    done
    return 1
}

# 判 Gen2（★ 只看寄存器，绝不用 nvidia-smi，也绝不用 grep 'Speed 5GT/s'）
is_gen2() { [ "$(cur_speed)" = "2" ]; }

wait_gen2() {
    local n="${1:-25}" i
    for i in $(seq 1 $((n * 5))); do
        is_gen2 && return 0
        sleep 0.2
    done
    return 1
}

need_root() {
    [ "$(id -u)" = "0" ] || { err "需要 root（sudo bash $0）"; exit 1; }
}

# 判定是否处于「驱动侧锁死态」
#
# ★ 不能只看 "CAP2=0x0002 + TLS=1 + Gen1" —— 那正是**原厂未解锁**的样子，
#   刚重启的干净机器也是这个状态（踩过：干净重启后误报警告）。
#
# 真正的锁死态特征：**我们写过解锁位，但没生效**
#   · 0x8841C 的 bit15|bit13|bit11 已置位（= 写过解锁）
#   · 但 CAP2 仍是 0x0002（= 没锁存成功）
#   · 且链路仍是 Gen1
#
# 原厂 0x8841C = 0xc0b40500 → (v & 0xA800) == 0
# 解锁 0x8841C = 0xc0b4ad00 → (v & 0xA800) == 0xA800
is_stuck() {
    local cfg cap
    cfg="$(_r 0x8841C)"
    cap="$(cap2)"
    # 解锁位已置位，但声明没锁存、链路还是 Gen1 ⇒ 锁死
    if [ "$(( cfg & 0xA800 ))" != "0" ] && [ "$cap" = "2" ] && [ "$(cur_speed)" = "1" ]; then
        return 0
    fi
    return 1
}

# 检查探针是否就绪。默认只检查不编译（保持"只读脚本"的承诺）。
# 传 auto 时才自动编译。
ensure_build() {
    local mode="${1:-check}"
    if [ -x "$BUILD_DIR/minictx" ] && [ -x "$BUILD_DIR/bwtest2" ]; then
        return 0
    fi
    if [ "$mode" = "auto" ]; then
        warn "build/ 里缺少探针，正在编译..."
        bash "$PKG_ROOT/bin/build.sh" || return 1
        return 0
    fi
    warn "build/ 里缺少探针（minictx / bwtest2）→ 先跑: bash $PKG_ROOT/bin/build.sh"
    return 1
}
