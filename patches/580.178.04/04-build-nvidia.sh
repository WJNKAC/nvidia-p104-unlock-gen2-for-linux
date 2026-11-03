#!/bin/bash
# ============================================================
# 04-build-nvidia.sh —— 编译 nvidia 内核模块并安装到装载位
#
# 为什么需要这一步：
#   01-gen2-patch.sh 改的是 **nv-kernel.o_binary**（编译输入）。
#   改完必须重编，才能得到带补丁的 nvidia.ko，再装到
#   /lib/modules/<内核>/updates/dkms/nvidia.ko（modprobe 真正读的位置）。
#
# ★ 直接改 .ko 的老做法已废弃：任何一次 make / dkms install / 装新内核
#   都会把 .ko 覆盖掉，补丁就没了。改编译输入才是从源头正确。
#
# ★ 三处 .ko 的坑（踩过，重启后 19fps 的根因）：
#     /usr/src/nvidia-<ver>/nvidia.ko                          make 产物
#     /var/lib/dkms/nvidia/<ver>/<kver>/x86_64/module/nvidia.ko  DKMS built 缓存
#     /lib/modules/<kver>/updates/dkms/nvidia.ko                装载位（modprobe 读这里）
#   三份独立文件，不是软链。dkms install 会用 built 缓存覆盖装载位。
#   ⇒ 本脚本编译+安装后会自动调 sync-dkms.sh adopt 固化。
#
# 用法：
#   sudo bash 04-build-nvidia.sh apply      # 编译 + 安装 + 同步 DKMS
#   sudo bash 04-build-nvidia.sh check      # 只读：看三处状态与签名
#   sudo bash 04-build-nvidia.sh status     # 同 check
#
# 注意：make 必须在 **root** 下跑（/usr/src/nvidia-<ver> 属主是 root，
#       降权会报 "cannot create conftest*.c: Permission denied"，
#       看起来像编译器坏了，其实是权限）。
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/../../utils/lib_v1.sh"
ACTION="${1:-apply}"

SRC_NV="${NVKERNEL_DIR}"
BUILT_KO="${SRC_NV}/nvidia.ko"
OUT_KO="${KMOD_OUT}/nvidia.ko"
DKMS_KO="/var/lib/dkms/nvidia/${DRIVER_VER}/${KVER}/x86_64/module/nvidia.ko"

# ============================================================
# 2. 函数
# ============================================================
usage() {
    cat <<'EOF'
用法: sudo bash 04-build-nvidia.sh <动作>

动作:
  apply      编译 nvidia 模块 + 安装到装载位 + 同步 DKMS（默认）
  check      只读：检查三处 .ko 的状态
  status     同 check

说明:
  编译输入是 nv-kernel.o_binary（01 改的就是它）。
  产物依次落到：
    output/nvidia.ko  →  DKMS built 缓存  →  /lib/modules/.../updates/dkms/nvidia.ko
EOF
}

need_root_or_die() {
    [ "$(id -u)" = "0" ] || { err "需要 root（sudo bash $0 ${ACTION}）"; exit 1; }
}

# 读三个位置的补丁状态：只看签名命中，不看 md5
# 输出 "<已打|未打|不存在|未知>"
sig_state() {
    local f="$1"
    [ -f "${f}" ] || { echo "不存在"; return; }
    python3 - "$f" <<'PY'
import sys
sig = bytes.fromhex(
    "8d42ff" "4156" "4155" "4989fd" "4154" "4531e4" "53" "4883ed40"
    "48897510" "89550c" "83f802" "0f87")
sig_new = sig[:2] + bytes([0xFE]) + sig[3:0x1D] + bytes([0x01]) + sig[0x1E:]
try:
    d = open(sys.argv[1], "rb").read()
except OSError:
    print("未知"); raise SystemExit
def cnt(p):
    n, i = 0, 0
    while True:
        i = d.find(p, i)
        if i < 0: return n
        n += 1; i += 1
o, n = cnt(sig), cnt(sig_new)
if o == 1 and n == 0:   print("未打")
elif o == 0 and n == 1: print("已打")
elif o == 0 and n == 0: print("未知")
else:                   print("未知(%d/%d)" % (o, n))
PY
}

# o_binary 里的状态
obinary_state() {
    local f="${NVKERNEL_BIN}"
    [ -f "${f}" ] || { echo "不存在"; return; }
    sig_state "${f}"
}

do_check() {
    section "nvidia 编译输入"
    printf "  %-52s %s\n" "${NVKERNEL_BIN}" "$(obinary_state)"

    section "三处 .ko（★ 独立文件，不是软链）"
    printf "  %-52s %s\n" "make 产物   ${BUILT_KO}" "$(sig_state "${BUILT_KO}")"
    printf "  %-52s %s\n" "DKMS 缓存   ${DKMS_KO}" "$(sig_state "${DKMS_KO}")"
    printf "  %-52s %s\n" "装载位      ${KO}" "$(sig_state "${KO}")"
    echo
    echo "  （判定只看 32 字节签名命中数；md5 会因别的补丁变化，不作判据）"

    local ob; ob="$(obinary_state)"
    local load; load="$(sig_state "${KO}")"
    echo
    if [ "${ob}" = "未打" ]; then
        warn "编译输入还没打补丁 → 先跑 01-gen2-patch.sh apply"
    elif [ "${ob}" = "已打" ] && [ "${load}" = "未打" ]; then
        warn "编译输入已打，但装载位还没有 → 需要 apply 重编安装"
    elif [ "${ob}" = "已打" ] && [ "${load}" = "已打" ]; then
        ok "编译输入与装载位都已打补丁"
    fi
}

do_apply() {
    need_root_or_die

    [ -d "${SRC_NV}" ] || { err "找不到驱动源码目录 ${SRC_NV}"; exit 1; }

    local ob; ob="$(obinary_state)"
    section "1. 检查编译输入"
    echo "   ${NVKERNEL_BIN}"
    echo "   状态: ${ob}"
    case "${ob}" in
        已打) ok "已经打过，继续重编" ;;
        未打) warn "还没打补丁 —— 建议先跑 01-gen2-patch.sh apply（仍会继续编译）" ;;
        *)   warn "签名状态未知（可能已被其它工具改过），仍继续" ;;
    esac

    section "2. 编译（make -j）"
    echo "   ★ 必须以 root 跑（/usr/src 属主 root，降权会报 conftest 权限错误）"
    local log="${LOG_DIR}/nvidia-build.log"
    mkdir -p "${LOG_DIR}"
    local nproc; nproc="$(nproc)"

    if ( cd "${SRC_NV}" && make -j"${nproc}" modules ) >"${log}" 2>&1; then
        ok "编译完成"
    else
        err "编译失败，详见 ${log}"
        tail -30 "${log}" | sed 's/^/     /'
        exit 1
    fi

    [ -f "${BUILT_KO}" ] || { err "make 未产出 ${BUILT_KO}"; exit 1; }

    section "3. 归档产物到 output/"
    mkdir -p "${KMOD_OUT}"
    cp -f "${BUILT_KO}" "${OUT_KO}"
    ok "已复制 → ${OUT_KO}  ($(stat -c%s "${OUT_KO}") 字节)"

    section "4. 安装到装载位"
    install -m 644 -o root -g root "${OUT_KO}" "${KO}"
    ok "已安装 → ${KO}"
    echo "   签名状态: $(sig_state "${KO}")"

    section "5. 同步 DKMS（防 dkms install 覆盖）"
    local sync="${PATCH_DIR}/sync-dkms.sh"
    if [ -f "${sync}" ]; then
        bash "${sync}" adopt 2>&1 | sed 's/^/   /' || warn "sync-dkms 失败，请手动检查"
    else
        warn "找不到 ${sync}，请手动同步 DKMS 缓存"
    fi

    section "6. 结果"
    do_check

    echo
    section "下一步"
    echo "   重载驱动或重启才生效："
    echo "     sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia"
    echo "     sudo modprobe nvidia nvidia_uvm"
    echo "   ⚠ 重载前确认没有程序占用 GPU：fuser -v /dev/nvidia*"
    echo
    echo "   之后推链路：sudo bash 02-gen2-push.sh start"
}

# ============================================================
# 3. main
# ============================================================
main() {
    case "${ACTION}" in
        check|status) need_root_or_die; do_check ;;
        apply)        do_apply ;;
        -h|--help|help) usage ;;
        *)            err "未知动作：${ACTION}"; usage; exit 2 ;;
    esac
}

main "$@"
