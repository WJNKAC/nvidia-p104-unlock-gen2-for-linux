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
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KVER="$(uname -r)"
KERNEL_BUILD_DIR="/lib/modules/${KVER}/build"

DO_USER_ONLY=0          # 1 = 只编用户态探针

# 用户态探针：产物名:说明（源文件固定为 src/<名>.c）
USER_PROBES=(
    "minictx:零 DMA，只建/销毁 CUDA 上下文"
    "bwtest2:带宽测试 H2D / D2H"
)

# ============================================================
# 2. 函数
# ============================================================

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

        if gcc -O2 -o "build/${name}" "src/${name}.c" -ldl 2>/dev/null; then
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

    # 前置条件一：不能用 root
    if [ "$(id -u)" = "0" ]; then
        echo "  ⚠ 当前是 root。若仓库在 NFS 上（root_squash），编译会失败。"
        echo "    请用普通用户重跑本脚本。"
        return 1
    fi

    # 前置条件二：必须有对应内核头
    if [ ! -d "${KERNEL_BUILD_DIR}" ]; then
        echo "  ✗ 缺少内核头：${KERNEL_BUILD_DIR}"
        echo "    安装：sudo apt install linux-headers-${KVER}"
        return 1
    fi

    cd "${PKG_ROOT}/kmod" || return 1

    # ★ 必须清掉旧产物，否则 make 会说"无事可做"
    rm -f gen2kr.o gen2kr.ko .gen2kr.o.d 2>/dev/null || true

    build_log="$(mktemp)"
    if make -C "${KERNEL_BUILD_DIR}" M="${PWD}" modules >"${build_log}" 2>&1; then
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
    if [ ! -f kmod/gen2kr.ko ]; then
        echo "  ✗ 编译\"成功\"但没有产物，检查 kmod/Makefile"
        return 1
    fi

    cp -f kmod/gen2kr.ko build/
    echo "  gen2kr.ko ✓  ($(stat -c%s kmod/gen2kr.ko) 字节)"
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
    mkdir -p build

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
