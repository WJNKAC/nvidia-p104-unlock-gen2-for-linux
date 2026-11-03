#!/bin/bash
# ============================================================
# utils/lib_v1.sh —— 公共库 v1
#
# ## 合并来源
#   本文件合并了原先重复的两个库：
#     · bin/_common.sh           （bin/ 下脚本用的）
#     · drivers/<ver>/utils/utils/lib_v1.sh（补丁脚本用的）
#   两者都定义 PKG_ROOT / KVER / ok-warn-err / _r / read_regs / cur_speed / need_root，
#   且各自维护一份路径，改一处忘一处就会互相打脸（多卡 BDF 问题就是这么来的）。
#   ⇒ 现在只有这一个库。
#
# ## 版本命名约定
#   文件名带 _vN，函数也带 _vN。规则：
#     · 新增选项 / 新增功能  → 叠版本号（lib_v2.sh，函数 xxx_v2()）
#     · 纯重构（不改行为）    → **不**叠版本号，原地改
#   旧版本在没有引用后删除，但要在 docs/utils-版本.md 里留一行记录（保留空位），
#   新版本号继续往上叠，不复用旧号。
#
# ## 目录假设（新结构）
#   <repo>/
#     utils/lib_v1.sh          ← 本文件
#     utils/bdf_v1.sh
#     utils/patch_v1.py
#     patches/<驱动版本>/       版本专属脚本 + src/kernel + output
#     check/                   只读检查
#     src/                     探针源码
#     build/                   探针编译产物
#     docs/ history/
#
# 被 source，不直接执行。
# ============================================================

# 防重复 source
if [ -n "${_LIB_V1_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
_LIB_V1_LOADED=1

# ============================================================
# 1. 路径
# ============================================================
LIB_V1_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # <repo>/utils
PKG_ROOT="$(cd "${LIB_V1_DIR}/.." && pwd)"                   # <repo>

CHECK_DIR="${PKG_ROOT}/check"
SRC_DIR="${PKG_ROOT}/src"
BUILD_DIR="${PKG_ROOT}/build"
DOCS_DIR="${PKG_ROOT}/docs"
LOG_DIR="${PKG_ROOT}/history/logs"

# 驱动版本：默认 580.178.04，可用 DRIVER_VER 覆盖
DRIVER_VER="${DRIVER_VER:-580.178.04}"
PATCH_DIR="${PKG_ROOT}/patches/${DRIVER_VER}"
OFFSETS_FILE="${PATCH_DIR}/offsets.sh"
PATCH_PY="${PKG_ROOT}/utils/patch_v1.py"
BDF_SH="${PKG_ROOT}/utils/bdf_v1.sh"

KVER="$(uname -r)"
KVER_SHORT="${KVER%%+*}"                                     # 6.12.113+deb13 → 6.12.113

# 内核源码目录（nvidia 驱动源码，patch 编译输入的所在）
NVKERNEL_DIR="${NVKERNEL_DIR:-/usr/src/nvidia-${DRIVER_VER}}"
NVKERNEL_BIN="${NVKERNEL_DIR}/nvidia/nv-kernel.o_binary"

# 最终模块（装载位）
KO="/lib/modules/${KVER}/updates/dkms/nvidia.ko"

# gen2kr 模块源码与产物
KMOD_DIR="${PATCH_DIR}/src/kernel"
KMOD_OUT="${PATCH_DIR}/output"

# BAR0 寄存器（只读用途）
R_LNKCAP=0x88084
R_LNKCAP2=0x880A4
R_LNKSTA=0x88088
R_LNKCTL2=0x880A8
R_CONFIG=0x8841C
R_LTSSM=0x8872C
R_LINKCFG0=0x8C040
R_CYA=0x8C2C0

# ============================================================
# 2. 输出
# ============================================================
if [ -t 1 ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
    C_R=; C_G=; C_Y=; C_B=; C_0=
fi
ok()      { echo "${C_G}✓${C_0} $*"; }
warn()    { echo "${C_Y}⚠${C_0} $*"; }
err()     { echo "${C_R}✗${C_0} $*"; }
hdr()     { echo; echo "${C_B}=== $* ===${C_0}"; }
section() { echo; echo "${C_B}── $* ──${C_0}"; }

# ============================================================
# 3. 目标设备（单一来源：utils/bdf_v1.sh）
# ============================================================
if [ -r "${BDF_SH}" ]; then
    # shellcheck source=/dev/null
    source "${BDF_SH}"
else
    err "找不到 utils/bdf_v1.sh（仓库结构不完整）"
    exit 3
fi

# ============================================================
# 4. 权限
# ============================================================
need_root() {
    [ "$(id -u)" = "0" ] || { err "需要 root（sudo bash $0）"; exit 1; }
}

# ============================================================
# 5. 偏移定义
# ============================================================
load_offsets() {
    [ -r "${OFFSETS_FILE}" ] || { err "找不到 ${OFFSETS_FILE}"; return 1; }
    # shellcheck source=/dev/null
    source "${OFFSETS_FILE}"
}

# ============================================================
# 6. 只读：读 BAR0 寄存器
# ============================================================
# 读单个 32 位寄存器；失败返回 0
_r() {
python3 - "$1" <<'PY'
import mmap, os, struct, sys
off = int(sys.argv[1], 16)
_dev = os.environ.get('DEV_BDF', '0000:01:00.0')
try:
    p = int(open('/sys/bus/pci/devices/%s/resource' % _dev).readline().split()[0], 16)
    f = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(f, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ, offset=p)
    print(struct.unpack_from('<I', m, off)[0])
    m.close(); os.close(f)
except Exception:
    print(0)
PY
}

# 一行摘要：CAP2=0x0006 LNKSTA=2 TLS=2 SPEED=1 MAXSPD=2
read_regs() {
python3 - <<'PY'
import mmap, os, struct
_dev = os.environ.get('DEV_BDF', '0000:01:00.0')
try:
    p = int(open('/sys/bus/pci/devices/%s/resource' % _dev).readline().split()[0], 16)
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

# 当前速率（只取 LNKSTA[3:0]，唯一可信判据；绝不用 lspci 文本 / nvidia-smi）
cur_speed() {
    local s
    s="$(_r "${R_LNKSTA}")"
    python3 -c "print((${s:-0}>>16)&0xF)" 2>/dev/null || echo 0
}

# 声明向量（0x06 = 已解锁支持 Gen2）
cap2() {
    local v
    v="$(_r "${R_LNKCAP2}")"
    echo "${v:-0}"
}

is_gen2() { [ "$(cur_speed)" = "2" ]; }

wait_gen2() {
    local n="${1:-25}" i
    for i in $(seq 1 $((n * 5))); do
        is_gen2 && return 0
        sleep 0.2
    done
    return 1
}

# 判活：连续 20 次 0xffffffff 才算失效（单次是重训瞬态）
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

# 判定是否处于「驱动侧锁死态」
#
# ★ 不能只看 "CAP2=0x0002 + TLS=1 + Gen1" —— 那正是原厂未解锁的样子。
# ★ 也不能单次采样就判死：写完解锁位、重训未完成时，0x8841C 的 bit15/13/11
#   已置位而 LNKSTA 仍是 1，此时会误报（外部 issue 实测：成功那版也报"锁死"）。
#   ⇒ 连续 3 次（间隔 0.7s，共约 1.4s，超过重训窗口）全部满足才算。
is_stuck() {
    local i cfg cap spd
    for i in 1 2 3; do
        cfg="$(_r "${R_CONFIG}")"
        cap="$(cap2)"
        spd="$(cur_speed)"
        if [ "$(( cfg & 0xA800 ))" = "0" ] || [ "${cap}" != "2" ] || [ "${spd}" != "1" ]; then
            return 1
        fi
        [ "${i}" -lt 3 ] && sleep 0.7
    done
    return 0
}

# ============================================================
# 7. GPU 占用检查
# ============================================================
# ★ 2026-10-29 事故：GPU 被浏览器占用时写 unbind 不会失败、也不会返回，
#   而是卡在 device_release_driver_internal 里，进程落入 D 状态、kill -9 无效，
#   只能整机重启。所以这是硬阻断，不是警告。
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
# 8. 探针（minictx / bwtest2）
# ============================================================
ensure_build() {
    local mode="${1:-check}"
    if [ -x "${BUILD_DIR}/minictx" ] && [ -x "${BUILD_DIR}/bwtest2" ]; then
        return 0
    fi
    if [ "$mode" = "auto" ]; then
        warn "build/ 里缺少探针，正在编译..."
        bash "${SRC_DIR}/build.sh" || return 1
        return 0
    fi
    warn "build/ 里缺少探针（minictx / bwtest2）→ 先跑: bash src/build.sh"
    return 1
}

# ============================================================
# 9. gen2kr 内核模块
# ============================================================
kmod_loaded()      { lsmod | grep -q '^gen2kr'; }
kmod_path()        { echo "${KMOD_OUT}/gen2kr.ko"; }
kmod_built()       { [ -f "$(kmod_path)" ]; }
kmod_kernel_version() { modinfo -F vermagic "$(kmod_path)" 2>/dev/null | awk '{print $1}'; }

# 编译模块的那个用户（NFS 上 root 写不进 .o.d，必须降权）
build_user() {
    local u="${SUDO_USER:-}"
    if [ -n "${u}" ] && [ "${u}" != "root" ]; then
        echo "${u}"; return 0
    fi
    u="$(stat -c %U "${PKG_ROOT}" 2>/dev/null || echo root)"
    echo "${u}"
}

run_as() {
    local u; u="$(build_user)"
    if [ "${u}" = "root" ]; then
        "$@"
    else
        sudo -u "${u}" -- "$@"
    fi
}

# 编译 gen2kr.ko 到 patches/<ver>/output/
#
# ★ 源码在 src/kernel/，产物在 output/，两者分开。
#   kbuild 的 M=<dir> 要求 Makefile 与 .c 同目录，所以用临时目录编译，
#   编译完把 .ko 拷回 output/ —— 不在 output/ 里留 .c 副本。
build_kmod() {
    local log="${LOG_DIR}/kmod-build.log"
    mkdir -p "${KMOD_OUT}" "${LOG_DIR}"

    [ -f "${KMOD_DIR}/gen2kr.c" ]  || { err "找不到 ${KMOD_DIR}/gen2kr.c"; return 1; }
    [ -f "${KMOD_DIR}/Makefile" ]  || { err "找不到 ${KMOD_DIR}/Makefile"; return 1; }

    local kdir="/lib/modules/${KVER}/build"
    [ -d "${kdir}" ] || { err "缺少内核构建目录 ${kdir}（装 linux-headers-${KVER}）"; return 1; }

    local tmp
    tmp="$(mktemp -d /tmp/gen2kr-build.XXXXXX)" || { err "无法创建临时目录"; return 1; }
    cp "${KMOD_DIR}/gen2kr.c" "${KMOD_DIR}/Makefile" "${tmp}/"

    if run_as make -C "${kdir}" M="${tmp}" modules >"${log}" 2>&1 \
       && [ -f "${tmp}/gen2kr.ko" ]; then
        cp -f "${tmp}/gen2kr.ko" "${KMOD_OUT}/gen2kr.ko"
        rm -rf "${tmp}"
        ok "gen2kr.ko  ($(stat -c%s "${KMOD_OUT}/gen2kr.ko") 字节)"
        return 0
    fi

    err "模块编译失败，详见 ${log}"
    tail -25 "${log}" | sed 's/^/     /'
    rm -rf "${tmp}"
    return 1
}
