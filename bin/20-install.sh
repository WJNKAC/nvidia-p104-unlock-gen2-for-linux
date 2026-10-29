#!/bin/bash
# 20-install.sh —— 应用补丁并让链路跑到 Gen2
#
# 做两件事，顺序不能反：
#   ① 给 nvidia.ko 打 2 字节补丁：让 _nv017105rm(mode=1) 在入口 bail
#      ⇒ 驱动不再在每次 cuInit 时把链路降回 Gen1
#   ② 加载 gen2kr 在 nv_pci_probe 返回瞬间解锁声明并推一次 → 链路升到 5GT/s
#      之后补丁负责"保持"，gen2kr 的守卫退化为安全网（补丁丢失时才动手）
#
# ★ 什么时候需要重载 nvidia，什么时候不需要：
#   · 刚 apply 过补丁 ⇒ **必须重载**（改的是 .ko 文件，不重载读的还是旧代码）
#   · 补丁早就在 .ko 里（例如重启后）⇒ **不必重载**，只要让设备重新 probe 一次
#   后者用 bin/push.sh 更快、更轻。本脚本会自动判断：
#   若检测到"补丁已打且当前加载的就是补丁后的模块"，会跳过重载。
#
# 用法：
#   sudo bash bin/20-install.sh --dry-run     # 只打印计划，不改任何东西
#   sudo bash bin/20-install.sh               # 执行（自动决定是否重载）
#   sudo bash bin/20-install.sh --no-patch    # 不打补丁，只加载 gen2kr（旧方案）
#   sudo bash bin/20-install.sh --no-kmod     # 只打补丁（用于已解锁过的场景）
#   sudo bash bin/20-install.sh --no-reload   # 强制不重载 nvidia（同 push.sh）
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_root

DRY=0; DO_PATCH=1; DO_KMOD=1; FORCE_NO_RELOAD=0
RELOAD_AUTO=0      # 1 = 需要重载（刚 apply 过 或 用户强制）
for a in "$@"; do
    case "$a" in
        --dry-run)    DRY=1 ;;
        --no-patch)   DO_PATCH=0 ;;
        --no-kmod)    DO_KMOD=0 ;;
        --no-reload)  FORCE_NO_RELOAD=1 ;;
        *) err "未知参数 $a"; exit 2 ;;
    esac
done

run() {
    if [ "$DRY" = "1" ]; then
        echo "   ${C_Y}[dry-run]${C_0} $*"
    else
        echo "   $ $*"
        eval "$@"
    fi
}

echo "======================================================"
echo " 安装 PCIe Gen2 解锁"
[ "$DRY" = "1" ] && echo " ${C_Y}模式：dry-run（不会改动任何东西）${C_0}"
echo "======================================================"

# ---------------- 0. 前置检查 ----------------
hdr "0. 前置检查"
[ -f "$KO" ] || { err "找不到 $KO"; exit 1; }
ok "驱动 $KO"
lspci -Dn | grep -q '10de:1b87' || { err "找不到 P104-100"; exit 1; }
ok "设备在总线"
if alive; then ok "MMIO 应答"; else
    err "MMIO 无应答 —— 设备可能已 ZOMBIE，先整机重启"
    echo "     判定工具：python3 bin/linkcap-audit.py"
    exit 1
fi
ensure_build check || { err "探针未编译 → bash bin/build.sh"; exit 1; }

# ★ 状态污染预检
# 注意：不能只看 "CAP2=0x0002 + TLS=1 + Gen1" —— 那是**原厂未解锁**的正常样子。
# 锁死态的特征是「写过解锁位但没生效」，用 is_stuck 判断。
if is_stuck; then
    warn "检测到驱动侧锁死态（解锁位已置位，但 CAP2 仍 0x0002、链路仍 Gen1）"
    echo "     实测该状态无法自愈（unbind/bind ×6、重训 ×13、重写 TLS ×20 全失败），"
    echo "     ${C_R}只能整机重启${C_0}。"
    echo "     判据：0x8841C=$(printf '0x%08x' "$(_r 0x8841C)")（bit15/13/11 已置位）"
fi

# ---------------- 1. 打补丁 ----------------
PATCH_CHANGED=0
if [ "$DO_PATCH" = "1" ]; then
    hdr "1. 应用 2 字节补丁"
    echo "   目标：_nv017105rm 入口的 mode 范围检查"
    echo "     127d66: lea -0x1(%rdx),%eax   →  lea -0x2(%rdx),%eax"
    echo "     127d81: cmp \$0x2,%eax         →  cmp \$0x1,%eax"
    echo "   效果：mode==1（Gen1）在入口直接 bail；mode 2/3 正常；≥4 与原逻辑等价"
    if [ "$DRY" = "1" ]; then
        python3 "$PKG_ROOT/bin/patch_no_gen1.py" check 2>&1 | sed 's/^/   /'
        echo "   ${C_Y}[dry-run]${C_0} python3 bin/patch_no_gen1.py apply"
    else
        BEFORE="$(md5sum "$KO" | cut -d' ' -f1)"
        python3 "$PKG_ROOT/bin/patch_no_gen1.py" apply || { err "补丁失败"; exit 1; }
        AFTER="$(md5sum "$KO" | cut -d' ' -f1)"
        if [ "$BEFORE" != "$AFTER" ]; then
            PATCH_CHANGED=1
            ok "补丁是新写入的 ⇒ 需要重载 nvidia 才生效"
        else
            ok "补丁本来就在（.ko 未变）⇒ 不需要重载"
        fi
    fi
else
    hdr "1. 跳过补丁（--no-patch）"
fi

# ---------------- 2. 视情况重载 nvidia ----------------
# ★ 只有「刚改了 .ko」才必须重载 —— 不重载的话内核里跑的还是旧代码。
#   补丁早在 .ko 里时（例如重启后），只要让设备重新 probe 一次就够，不碰 nvidia。
DO_RELOAD=0
if [ "$FORCE_NO_RELOAD" = "1" ]; then
    DO_RELOAD=0
elif [ "$PATCH_CHANGED" = "1" ]; then
    DO_RELOAD=1
elif [ "$DRY" = "1" ] && [ "$DO_PATCH" = "1" ]; then
    # dry-run 下不知道会不会改，按"可能要重载"展示
    DO_RELOAD=1
fi

if [ "$DO_RELOAD" = "1" ]; then
    hdr "2. 重载 nvidia 驱动（让新写入的补丁生效）"
    if lsmod | grep -q '^gen2kr'; then
        run "rmmod gen2kr"
    fi
    for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
        lsmod | grep -q "^$m " && run "rmmod $m 2>/dev/null || true"
    done
    sleep 1
    if [ "$DRY" = "0" ]; then
        if lsmod | grep -qE '^nvidia'; then
            err "nvidia 未能卸载，检查占用："
            echo "     fuser -v /dev/nvidia* 2>/dev/null"
            echo "     （Xorg / nvitop / 任何 CUDA 进程都会占住）"
            exit 1
        fi
        ok "已卸干净"
    fi
    run "modprobe nvidia"
    run "modprobe nvidia_uvm"
    [ "$DRY" = "0" ] && sleep 2
else
    hdr "2. 不重载 nvidia（补丁已在 .ko 里，无需重载）"
    if lsmod | grep -q '^nvidia '; then
        ok "nvidia 保持加载中"
    else
        err "nvidia 未加载 —— 请先 modprobe nvidia"
        exit 1
    fi
fi

[ "$DRY" = "0" ] && ok "当前：$(read_regs)"

# ---------------- 3. gen2kr 解锁声明 + 首次 push ----------------
if [ "$DO_KMOD" = "1" ]; then
    hdr "3. 加载 gen2kr 并触发一次 probe"
    echo "   它在 nv_pci_probe 上挂 kretprobe，在 probe 返回的瞬间："
    echo "     ① 解锁端点声明（XP3G + 0x8841C + 0x88610）使 LCAP2 → 0x06"
    echo "     ② 写 0x8C040.SPEED=1（5.0GT/s）"
    echo "     ③ 两端 TLS=2，从根端口 Retrain"
    # 已在内存里就先卸掉，清掉 triggered 标志（否则 push 不会再触发）
    if lsmod | grep -q '^gen2kr'; then
        run "rmmod gen2kr 2>/dev/null || true"
        [ "$DRY" = "0" ] && sleep 1
    fi
    run "insmod $BUILD_DIR/gen2kr.ko"
    [ "$DRY" = "0" ] && sleep 1
    run "echo $DEV_BDF > /sys/bus/pci/drivers/nvidia/unbind"
    [ "$DRY" = "0" ] && sleep 1
    run "echo $DEV_BDF > /sys/bus/pci/drivers/nvidia/bind"

    if [ "$DRY" = "0" ]; then
        echo "   等待链路升到 Gen2（最多 20s）..."
        if wait_gen2 20; then
            ok "链路 = Gen2    $(read_regs)"
        else
            warn "push 未成功（$(read_regs)）"
            echo "     gen2kr 日志："
            dmesg | grep -i gen2kr | tail -8 | sed 's/^/       /'
            echo
            echo "     若守卫正在自动重试，再等 25s；仍不行则参考 docs/故障排查.md"
        fi
    fi
else
    hdr "3. 跳过 gen2kr（--no-kmod）"
fi

# ---------------- 4. 结果 ----------------
hdr "4. 结果"
if [ "$DRY" = "1" ]; then
    echo "   ${C_Y}dry-run 结束，未改动任何东西${C_0}"
    exit 0
fi
R="$(read_regs)"
echo "   $R"
if is_gen2; then
    ok "链路已是 Gen2"
    echo
    echo "   下一步：sudo bash bin/30-verify.sh    （验证带宽 + 补丁是否真的接管）"
else
    err "链路仍是 Gen1"
    echo "   可能原因："
    echo "     1. 设备处于驱动侧锁死态 → 整机重启后重试"
    echo "     2. gen2kr push 失败 → dmesg | grep gen2kr"
    echo "     3. 声明未解锁 → CAP2 应为 0x0006，当前 $(cap2)"
fi
echo "======================================================"
