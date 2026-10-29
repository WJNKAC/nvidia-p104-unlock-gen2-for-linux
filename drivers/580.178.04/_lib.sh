#!/bin/bash
# ============================================================
# _lib.sh —— drivers/<版本>/ 下脚本的公共部分
#
# 被 00-whitelist.sh / 01-gen2-patch.sh / 02-gen2-push.sh source。
# 只放「通用」的东西：路径、颜色、权限、寄存器读取、GPU 占用检查、模块编译。
# 具体业务（改哪个字节、怎么推链路）在各脚本里。
#
# 设计约束（踩过的坑）：
#   · 不用 $(( )) 处理 >2^63 的数（dash 报 Illegal number）→ 交给 python
#   · 读 /dev/mem 必须 O_RDWR（O_RDONLY + PROT_WRITE 会 PermissionError）
# ============================================================

# ============================================================
# 1. 路径
# ============================================================
VER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # drivers/<版本>/
PKG_ROOT="$(cd "${VER_DIR}/../.." && pwd)"                  # 仓库根
OFFSETS_FILE="${VER_DIR}/offsets.sh"
PATCH_PY="${VER_DIR}/_patch.py"

KVER="$(uname -r)"
KVER_SHORT="${KVER%%+*}"                                    # 6.12.113+deb13 → 6.12.113
KERNEL_SRC_DIR="${VER_DIR}/kernel/${KVER_SHORT}"            # 模块源码目录

KO="/lib/modules/${KVER}/updates/dkms/nvidia.ko"
DEV_BDF="0000:01:00.0"
DEV_SHORT="01:00.0"

# 驱动源码目录（whitelist 补丁的输入）
NVKERNEL_DIR="/usr/src/nvidia-580.178.04"

# ============================================================
# 2. 输出
# ============================================================
if [ -t 1 ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
    C_R=; C_G=; C_Y=; C_B=; C_0=
fi
ok()   { echo "${C_G}✓${C_0} $*"; }
warn() { echo "${C_Y}⚠${C_0} $*"; }
err()  { echo "${C_R}✗${C_0} $*"; }
hdr()  { echo; echo "${C_B}── $* ──${C_0}"; }

# 分节大标题
section() {
    echo
    echo "======================================================"
    echo " $*"
    echo "======================================================"
}

need_root() {
    [ "$(id -u)" = "0" ] || { err "需要 root：sudo bash $0 $*"; exit 1; }
}

# 加载偏移定义（offsets.sh 只定义变量，不做任何事）
load_offsets() {
    if [ ! -f "${OFFSETS_FILE}" ]; then
        err "找不到 ${OFFSETS_FILE}"
        exit 1
    fi
    # shellcheck source=offsets.sh
    source "${OFFSETS_FILE}"
}

# ============================================================
# 3. 寄存器读取（只读，用于判定状态）
# ============================================================
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

# 一行摘要：CAP2=0x0006 LNKSTA=2 ...
read_regs() {
python3 - <<'PY'
import mmap, os, struct
try:
    p = int(open('/sys/bus/pci/devices/0000:01:00.0/resource').readline().split()[0], 16)
    f = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    g = lambda o: struct.unpack_from('<I', m, o)[0]
    cap = g(0x88084); cap2 = g(0x880A4)
    lsta = g(0x88088); ctl2 = g(0x880A8); cfg = g(0x8C040)
    m.close(); os.close(f)
    print(f"CAP2=0x{cap2:04x} LNKSTA={(lsta>>16)&0xF} TLS={ctl2&0xF} "
          f"SPEED={(cfg>>18)&3} MAXSPD={cap&0xF}")
except Exception:
    print("CAP2=? LNKSTA=? TLS=? SPEED=? MAXSPD=?")
PY
}

# 当前链路速率（唯一可信判据：LNKSTA[3:0]）
cur_speed() {
    local s
    s="$(_r 0x88088)"
    python3 -c "print((${s:-0}>>16)&0xF)" 2>/dev/null || echo 0
}

# 是否已是 Gen2
is_gen2() { [ "$(cur_speed)" = "2" ]; }

# 等链路升到 Gen2，参数为最长秒数
wait_gen2() {
    local n="${1:-20}" i
    for i in $(seq 1 $((n * 5))); do
        is_gen2 && return 0
        sleep 0.2
    done
    return 1
}

# ============================================================
# 4. 安全检查
# ============================================================

# GPU 是否空闲
#
# ★ 2026-10-29 事故：GPU 被浏览器占用时写 unbind 不会失败、也不会返回，
#   而是卡在 device_release_driver_internal 里，进程落入 D/R 状态、
#   kill -9 无效，只能整机重启。所以这是硬阻断。
check_gpu_idle() {
    local holders
    holders="$(fuser /dev/nvidia* 2>/dev/null | tr -s ' ')"
    [ -z "${holders}" ] && return 0

    err "有进程正在使用 GPU，unbind 会卡死内核（只能重启）"
    echo "     占用者："
    fuser -v /dev/nvidia* 2>&1 | sed 's/^/       /'
    echo
    echo "     请先关掉这些程序（浏览器 / Electron 应用最常见），然后重试。"
    echo "     ⚠ 实测 unbind 会停在 device_lock 不动，进程落入 D 状态、kill -9 无效。"
    return 1
}

# ============================================================
# 5. 内核模块
# ============================================================

# 模块目录对应哪套内核（用于提示）
kmod_kernel_version() {
    basename "${KERNEL_SRC_DIR}" 2>/dev/null || echo "?"
}

# 模块是否已加载
kmod_loaded() { lsmod | grep -q '^gen2kr'; }

# 编译模块。成功后 .ko 在 ${KERNEL_SRC_DIR}/gen2kr.ko
build_kmod() {
    hdr "编译内核模块"

    if [ ! -d "${KERNEL_SRC_DIR}" ]; then
        err "找不到模块源码目录：${KERNEL_SRC_DIR}"
        echo "    期望: kernel/<内核版本>/gen2kr.c"
        echo "    仓库里现有的："
        ls -1 "${VER_DIR}/kernel/" 2>/dev/null | sed 's/^/      /'
        return 1
    fi
    echo "   源码目录: kernel/$(basename "${KERNEL_SRC_DIR}")"

    local dir_kver
    dir_kver="$(kmod_kernel_version)"
    if [ "${dir_kver}" != "${KVER_SHORT}" ]; then
        warn "该目录记录的验证内核是 ${dir_kver}，当前是 ${KVER_SHORT}"
        echo "     源码用稳定 API 通常能编过，但产物未经该内核验证"
    fi

    if [ ! -d "/lib/modules/${KVER}/build" ]; then
        err "缺少内核头：/lib/modules/${KVER}/build"
        echo "    安装：sudo apt install linux-headers-${KVER}"
        return 1
    fi

    # ★ 必须清掉旧产物，否则 make 会说"无事可做"
    rm -f "${KERNEL_SRC_DIR}"/gen2kr.o \
          "${KERNEL_SRC_DIR}"/gen2kr.ko \
          "${KERNEL_SRC_DIR}"/.gen2kr.o.d 2>/dev/null || true

    local log
    log="$(mktemp)"
    if make -C "/lib/modules/${KVER}/build" M="${KERNEL_SRC_DIR}" modules >"${log}" 2>&1; then
        ok "编译成功  ($(stat -c%s "${KERNEL_SRC_DIR}/gen2kr.ko" 2>/dev/null || echo ?) 字节)"
        rm -f "${log}"
        return 0
    fi

    err "编译失败，最后 20 行："
    tail -20 "${log}" | sed 's/^/     /'
    rm -f "${log}"
    echo
    echo "   常见原因："
    echo "     1. 内核头版本不匹配 —— 必须在跑这张卡的那台机器上编"
    echo "     2. 编译器与内核构建时不一致（clang 内核 + gcc 编模块会报"
    echo "        unrecognized command-line option '-mstack-alignment'）"
    echo "     3. NFS root_squash —— 不要用 sudo 编"
    return 1
}

# 模块 .ko 路径
kmod_path() { echo "${KERNEL_SRC_DIR}/gen2kr.ko"; }

# 模块是否已编好
kmod_built() { [ -f "$(kmod_path)" ]; }
