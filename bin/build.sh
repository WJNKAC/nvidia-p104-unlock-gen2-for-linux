#!/bin/bash
# build.sh —— 编译用户态探针与内核模块
#
# ★ 两者的编译要求不同：
#   · 用户态探针（minictx / bwtest2）：任何机器都能编，只依赖 gcc + libcuda
#   · 内核模块（gen2kr.ko）：**必须用目标机器的内核头**（/lib/modules/$(uname -r)/build）
#     ⇒ 要在跑 P104 的那台机器上编，不能拿别的机器的内核头凑
#
# ★ NFS root_squash：/data/labs 属主是 union，`sudo make` 会因为 root_squash
#   写不进去（.gen2kr.o.d: 权限不够）⇒ **本脚本不要用 sudo 跑**。
#
# 用法：
#   bash bin/build.sh              # 全编
#   bash bin/build.sh --user-only  # 只编用户态探针（任意机器）
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG_ROOT"
mkdir -p build

USER_ONLY=0
[ "${1:-}" = "--user-only" ] && USER_ONLY=1

KVER="$(uname -r)"

echo "=============================================="
echo " 1. 用户态探针"
echo "=============================================="
gcc -O2 -o build/minictx src/minictx.c -ldl || { echo "  ✗ minictx 编译失败"; exit 1; }
echo "  minictx  ✓   （零 DMA，只建/销毁 CUDA 上下文）"

if gcc -O2 -o build/bwtest2 src/bwtest2.c -ldl 2>/dev/null; then
    echo "  bwtest2  ✓   （带宽测试）"
else
    echo "  ✗ bwtest2 编译失败（需要 libcuda.so.1，通常由 nvidia 驱动提供）"
    echo "    检查：ls -l /usr/lib/x86_64-linux-gnu/libcuda.so.1"
    exit 1
fi

if [ "$USER_ONLY" = "1" ]; then
    echo
    echo "已跳过内核模块（--user-only）"
    exit 0
fi

echo
echo "=============================================="
echo " 2. 内核模块 gen2kr.ko"
echo "=============================================="
echo "  本机内核: $KVER"

if [ "$(id -u)" = "0" ]; then
    echo "  ⚠ 当前是 root。若 /data 在 NFS 上（root_squash），编译会失败。"
    echo "    请用普通用户重跑本脚本。"
    exit 1
fi

if [ ! -d "/lib/modules/$KVER/build" ]; then
    echo "  ✗ 缺少内核头：/lib/modules/$KVER/build"
    echo "    安装：sudo apt install linux-headers-$KVER"
    exit 1
fi

cd kmod
# ★ 必须清掉旧的 .o/.ko，否则 make 会说"无事可做"
rm -f gen2kr.o gen2kr.ko .gen2kr.o.d 2>/dev/null || true

BUILD_LOG="$(mktemp)"
if make -C "/lib/modules/$KVER/build" M="$PWD" modules >"$BUILD_LOG" 2>&1; then
    echo "  编译成功"
else
    echo "  ✗ 编译失败，最后 20 行："
    tail -20 "$BUILD_LOG" | sed 's/^/     /'
    rm -f "$BUILD_LOG"
    cd ..
    echo
    echo "  常见原因："
    echo "    1. 内核头版本不匹配 —— 必须在跑 P104 的那台机器上编译"
    echo "       （当前 $KVER，若你的目标机器内核不同，请在目标机器上运行本脚本）"
    echo "    2. 编译器与内核构建时不一致（clang 构建的内核 + gcc 编译模块会报"
    echo "       unrecognized command-line option '-mstack-alignment'）"
    echo "    3. NFS root_squash —— 不要用 sudo"
    exit 1
fi
rm -f "$BUILD_LOG"
cd ..

if [ -f kmod/gen2kr.ko ]; then
    cp -f kmod/gen2kr.ko build/
    echo "  gen2kr.ko ✓  ($(stat -c%s kmod/gen2kr.ko) 字节)"
else
    echo "  ✗ 编译"成功"但没有产物，检查 kmod/Makefile"
    exit 1
fi

echo
echo "=============================================="
echo " 产物"
echo "=============================================="
ls -la build/
echo
echo "下一步：sudo bash bin/10-check.sh    （自检，纯只读）"
