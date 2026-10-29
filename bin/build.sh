#!/bin/bash
# ============================================================
# build.sh —— 编译用户态探针与内核模块
#
# 两类产物的编译要求不同：
#   · 用户态探针（minictx / bwtest2）：任何机器都能编，只依赖 gcc + libcuda
#   · 内核模块（gen2kr.ko）：**必须用目标机器的内核头**
#     （/lib/modules/$(uname -r)/build）
#     ⇒ 要在跑 P104 的那台机器上编，不能拿别的机器的内核头凑
#
# ★ 不要用 sudo 跑。root 编出来的产物归 root，之后普通用户改不动；
#   若仓库在 NFS 上（root_squash），root 甚至写不进去
#   （.gen2kr.o.d: 权限不够）。
#
# 用法：
#   bash bin/build.sh              # 全编
#   bash bin/build.sh --user-only  # 只编用户态探针（任意机器）
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

KERNEL_BUILD_DIR="/lib/modules/${KVER}/build"

DO_USER_ONLY=0          # 1 = 只编用户态探针
COMPILE_USER=""         # 用哪个用户编译内核模块（root 时降权）

# 用户态探针：产物名:说明（源文件固定为 src/<名>.c）
USER_PROBES=(
    "minictx:零 DMA，只建/销毁 CUDA 上下文"
    "bwtest2:带宽测试 H2D / D2H"
)

# ============================================================
# 2. 函数
# ============================================================

# 决定用哪个用户编译和写产物
#
# ★ 2026-10-29 踩的坑：NFS root_squash 下 root 被映射成 nobody，
#   既写不进 kmod 的 .gen2kr.o.d，也覆盖不了 build/ 里 union 拥有的文件。
#   有 SUDO_USER 就降权；没有就只能让用户自己用普通用户跑。
determine_compile_user() {
    if [ "$(id -u)" != "0" ]; then
        COMPILE_USER="$(id -un)"
        return 0
    fi
    if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
        COMPILE_USER="${SUDO_USER}"
        return 0
    fi
    return 1
}

# 以指定用户执行命令（root 时降权；本来就不是 root 就直跑）
run_as() {
    local user="$1"; shift
    if [ "${user}" = "$(id -un)" ]; then
        "$@"
    else
        sudo -u "${user}" "$@"
    fi
}

# 打印分节标题
section() {
    echo
    echo "=============================================="
    echo " $*"
    echo "=============================================="
}

# 编译所有用户态探针。任一失败则返回 1。
build_user_probes() {
    local item name desc
    section "1. 用户态探针"

    for item in "${USER_PROBES[@]}"; do
        name="${item%%:*}"
        desc="${item#*:}"

        if run_as "${COMPILE_USER}" gcc -O2 -o "build/${name}" "src/${name}.c" -ldl 2>/dev/null; then
            echo "  ${name}  ✓   （${desc}）"
        else
            echo "  ✗ ${name} 编译失败"
            echo "    源文件: src/${name}.c"
            echo "    若缺 libcuda.so.1：ls -l /usr/lib/x86_64-linux-gnu/libcuda.so.1"
            return 1
        fi
    done
    return 0
}

# 编译内核模块 gen2kr.ko。失败时给出常见原因。
build_kernel_module() {
    local build_log

    section "2. 内核模块 gen2kr.ko"
    echo "  本机内核: ${KVER}"

    if [ "${COMPILE_USER}" != "$(id -un)" ]; then
        echo "  编译身份: ${COMPILE_USER}（root 编写不进产物，已降权）"
    fi

    # 前置条件二：必须有对应内核头
    if [ ! -d "${KERNEL_BUILD_DIR}" ]; then
        echo "  ✗ 缺少内核头：${KERNEL_BUILD_DIR}"
        echo "    安装：sudo apt install linux-headers-${KVER}"
        return 1
    fi

    if [ -z "${KMOD_DIR}" ]; then
        echo "  ✗ 找不到内核模块源码目录"
        echo "    期望: drivers/<驱动版本>/kernel/<内核版本>/gen2kr.c"
        return 1
    fi
    echo "  源码目录: ${KMOD_DIR#${PKG_ROOT}/}"

    # ★ 目录名是"这套模块验证过的内核版本"，与当前内核不一致要提示
    local dir_kver
    dir_kver="$(basename "${KMOD_DIR}")"
    if [ "${dir_kver}" != "${KVER_SHORT}" ]; then
        echo "  ⚠ 目录记录的内核版本是 ${dir_kver}，当前是 ${KVER_SHORT}"
        echo "    源码用稳定 API 通常能编过，但产物未经该内核验证"
    fi

    cd "${KMOD_DIR}" || return 1

    # ★ 必须清掉旧产物，否则 make 会说"无事可做"
    rm -f gen2kr.o gen2kr.ko .gen2kr.o.d \
          gen2kr.mod gen2kr.mod.c gen2kr.mod.o .gen2kr.mod.o.d \
          .module-common.o Module.symvers modules.order 2>/dev/null || true

    # 目录属主要让编译用户可写
    [ "${COMPILE_USER}" != "$(id -un)" ] && chown "${COMPILE_USER}" "${KMOD_DIR}" 2>/dev/null || true

    build_log="$(mktemp)"
    if run_as "${COMPILE_USER}" make -C "${KERNEL_BUILD_DIR}" M="${KMOD_DIR}" modules >"${build_log}" 2>&1; then
        echo "  编译成功"
    else
        echo "  ✗ 编译失败，最后 20 行："
        tail -20 "${build_log}" | sed 's/^/     /'
        rm -f "${build_log}"
        cd "${PKG_ROOT}" || true
        print_kmod_failure_hints
        return 1
    fi
    rm -f "${build_log}"
    cd "${PKG_ROOT}" || return 1

    # 确认产物真的生成了
    if [ ! -f "${KMOD_DIR}/gen2kr.ko" ]; then
        echo "  ✗ 编译\"成功\"但没有产物，检查 ${KMOD_DIR}/Makefile"
        return 1
    fi

    # 拷贝也降权，避免 build/ 里出现 root 拥有的产物
    mkdir -p build
    [ "${COMPILE_USER}" != "$(id -un)" ] && chown "${COMPILE_USER}" build 2>/dev/null || true
    run_as "${COMPILE_USER}" cp -f "${KMOD_DIR}/gen2kr.ko" build/
    echo "  gen2kr.ko ✓  ($(stat -c%s "${KMOD_DIR}/gen2kr.ko") 字节)"
    return 0
}

# 内核模块编译失败时的排查提示
print_kmod_failure_hints() {
    echo
    echo "  常见原因："
    echo "    1. 内核头版本不匹配 —— 必须在跑 P104 的那台机器上编译"
    echo "       （当前 ${KVER}，若目标机器内核不同，请在目标机器上运行）"
    echo "    2. 编译器与内核构建时不一致（clang 构建的内核 + gcc 编模块会报"
    echo "       unrecognized command-line option '-mstack-alignment'）"
    echo "    3. NFS root_squash —— 不要用 sudo"
}

# 列出产物并提示下一步
show_products() {
    section "产物"
    ls -la build/
    echo
    echo "下一步：sudo bash bin/10-check.sh    （自检，纯只读）"
}

# 解析命令行参数
parse_args() {
    case "${1:-}" in
        --user-only) DO_USER_ONLY=1 ;;
        "")          ;;
        *)
            echo "未知参数: $1" >&2
            echo "用法: bash bin/build.sh [--user-only]" >&2
            exit 2
            ;;
    esac
}

# ============================================================
# 3. main
# ============================================================
main() {
    parse_args "$@"

    cd "${PKG_ROOT}" || exit 1

    if ! determine_compile_user; then
        echo "✗ 当前是 root 且拿不到 SUDO_USER，无法确定编译身份" >&2
        echo "  请用普通用户跑：bash bin/build.sh" >&2
        exit 1
    fi

    mkdir -p build
    # build/ 若之前被 root 建过，转给编译用户，否则它写不进去
    if [ "${COMPILE_USER}" != "$(id -un)" ]; then
        chown "${COMPILE_USER}" build 2>/dev/null || true
    fi

    build_user_probes || exit 1

    if [ "${DO_USER_ONLY}" = "1" ]; then
        echo
        echo "已跳过内核模块（--user-only）"
        exit 0
    fi

    build_kernel_module || exit 1
    show_products
}

main "$@"
