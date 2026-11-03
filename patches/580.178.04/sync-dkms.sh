#!/bin/bash
# ============================================================
# sync-dkms.sh —— 把打过补丁的 nvidia.ko 同步到 DKMS，防重启被覆盖
#
# 【为什么要它】
#   启动时 modprobe 读的是 /lib/modules/<kver>/updates/dkms/nvidia.ko。
#   而 DKMS 另有一份「built」缓存在
#       /var/lib/dkms/nvidia/<版本>/<kver>/x86_64/module/nvidia.ko
#   这两份是**独立的文件**，不是软链。
#
#   `dkms install`（以及装新内核时的 autoinstall）会把 built **复制**到
#   /lib/modules —— 如果 built 还是老的原始版，你的补丁就被覆盖掉了。
#
#   ★ 所以：make 出 /usr/src 的 nvidia.ko 之后，必须同步这两个位置。
#
# 【实测的坑】
#   2026-10-30：打完 00 白名单、按提示 make，重启后 19fps 依旧。
#   原因就是只 make 了 /usr/src，DKMS built 缓存还是 10-28 的原始版
#   （md5 7f6805ed…，P104-100 出现 2 次），重启时被它覆盖。
#
# 用法：
#   sudo ./sync-dkms.sh status   看三处状态（只读）
#   sudo ./sync-dkms.sh apply    从 /usr/src 同步到两个 DKMS 位置
#   sudo ./sync-dkms.sh restore  从 DKMS built 反向恢复 /usr/src（回滚用）
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================
KVER="$(uname -r)"
SRC="/usr/src/nvidia-580.178.04/nvidia.ko"
DKMS_BUILT="/var/lib/dkms/nvidia/580.178.04/${KVER}/x86_64/module/nvidia.ko"
INSTALLED="/lib/modules/${KVER}/updates/dkms/nvidia.ko"

ACTION="${1:-status}"

# P104-100 = 0x1b87，出现在「受限 SKU 表」里说明白名单没打
# 出现在「PCI device ID 表」里是正常的（那是设备识别用的，必须保留）
# 判据：<=1 次 = 白名单已打；>=2 次 = 未打

if [ -t 1 ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
    C_R=; C_G=; C_Y=; C_B=; C_0=
fi
ok()   { echo "${C_G}✓${C_0} $*"; }
warn() { echo "${C_Y}⚠${C_0} $*"; }
err()  { echo "${C_R}✗${C_0} $*"; }
hdr()  { echo; echo "${C_B}── $* ──${C_0}"; }

# ============================================================
# 2. 函数
# ============================================================

usage() {
    cat <<EOF
用法: sudo ./sync-dkms.sh <动作>

动作:
  status    看三处 .ko 的 md5 / 补丁状态（只读，默认）
  adopt     ★ 把【当前装载位】那份固化到 DKMS 缓存 + /usr/src
              —— 场景：现在跑得挺好（补丁都在），但担心重启被覆盖
  apply     从 /usr/src 推到 DKMS 两处
              —— 场景：刚 make 完新 .ko，想让它成为标准产物
  restore   从 DKMS built 缓存恢复 /usr/src 的副本（回滚参考）

说明:
  启动加载的是 $INSTALLED
  DKMS 的缓存是   $DKMS_BUILT
  两者独立，dkms install 会用缓存覆盖前者 ⇒ 打完补丁必须同步。

  ⚠ adopt 与 apply 方向相反：
     /usr/src 的 nvidia.ko 是 make 出来的，**不含 gen2 的 01 补丁**
     （01 是直接改 .ko，重编就没了）。所以「刚 make 完」别直接 apply，
     先把 01 补丁打到 /usr/src 那份，或者用 adopt 把当前生效的拷回去。
EOF
}

need_root() {
    [ "$(id -u)" = "0" ] || { err "需要 root：sudo $0 $*"; exit 1; }
}

# 打印三处的状态
show_status() {
    hdr "三处 nvidia.ko 的状态"
    python3 - "${SRC}" "${DKMS_BUILT}" "${INSTALLED}" <<'PY'
import sys, os, time, hashlib

pat = bytes([0x87, 0x1b, 0x07, 0x00])          # P104-100
labels = ["/usr/src（编译产物）", "DKMS built（缓存）", "装载位（modprobe 读这个）"]

for label, p in zip(labels, sys.argv[1:4]):
    if not os.path.exists(p):
        print(f"   {label:<26} 不存在")
        continue
    d = open(p, "rb").read()
    n = d.count(pat)
    st = time.strftime("%m-%d %H:%M", time.localtime(os.stat(p).st_mtime))
    wl = "已打" if n <= 1 else "未打"
    flag = "  ★" if label.startswith("装载位") else ""
    print(f"   {label:<26} {st}  md5={hashlib.md5(d).hexdigest()[:10]}  "
          f"白名单={wl}(P104 x{n}){flag}")

print()
print("   判据：P104-100 出现 <=1 次 = 白名单已打（剩那 1 次是 PCI device ID 表，必须保留）")
print("         出现 2 次 = 未打（受限 SKU 表里还留着）")
PY
}

do_status() {
    show_status
    echo
    echo "   时钟检查（需重启后确认）："
    echo "     目前装载位和 DKMS 缓存是否一致，决定重启后补丁会不会丢。"
    if [ -f "${SRC}" ] && [ -f "${DKMS_BUILT}" ]; then
        if cmp -s "${SRC}" "${DKMS_BUILT}"; then
            ok "两者一致 —— 重启安全"
        else
            warn "两者不一致 —— 建议 apply 同步，否则 dkms install 会覆盖"
        fi
    fi
}

do_apply() {
    need_root apply

    if [ ! -f "${SRC}" ]; then
        err "找不到 ${SRC}，先 make 编译"
        exit 1
    fi

    # 防呆：/usr/src 是 make 出来的，天然不含 01 的 gen2 补丁
    if ! python3 - "${SRC}" <<'PY'
import sys
d = open(sys.argv[1], "rb").read()
new_sig = bytes.fromhex("8d42fe415641554989fd41544531e4534883ed404889751089550c83f8010f87")
sys.exit(0 if d.count(new_sig) == 1 else 1)
PY
    then
        warn "${SRC} 上没有 gen2（01）补丁"
        echo "      它是 make 出来的，01 补丁打在 .ko 上、重编就没了。"
        echo "      直接 apply 会把 gen2 补丁一起冲掉。"
        echo
        echo "      两种正确做法："
        echo "        a) 对 ${SRC} 重新跑一次 01-gen2-patch.sh（改成指向它）"
        echo "        b) 若当前装载位已经是好的，改用：sudo $0 adopt"
        echo
        read -r -p "      仍要继续 apply 吗？(yes/no) " ans
        [ "${ans}" = "yes" ] || { echo "      已取消。"; exit 1; }
    fi

    hdr "同步到 DKMS"
    cp -f "${SRC}" "${DKMS_BUILT}" && ok "built 缓存 <- ${SRC}"
    cp -f "${SRC}" "${INSTALLED}" && ok "装载位   <- ${SRC}"

    echo
    show_status
    echo
    warn "现在运行中的模块还是旧的。要生效需重载驱动或重启："
    echo "     sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia"
    echo "     sudo modprobe nvidia && sudo modprobe nvidia_uvm nvidia_modeset nvidia_drm"
}

# 把「当前装载位」那份固化到 DKMS 缓存 + /usr/src
# 适用：现在跑得好好的，但这套补丁状态随时可能被 DKMS 覆盖
do_adopt() {
    need_root adopt

    if [ ! -f "${INSTALLED}" ]; then
        err "找不到装载位 ${INSTALLED}"
        exit 1
    fi

    hdr "固化当前装载位的 .ko"
    echo "   源：${INSTALLED}"

    # 先报告源里有什么，让用户确认这就是"想要的状态"
    python3 - "${INSTALLED}" <<'PY'
import sys, hashlib
p = sys.argv[1]
d = open(p, "rb").read()
gen2 = bytes.fromhex("8d42fe415641554989fd41544531e4534883ed404889751089550c83f8010f87")
wl = d.count(bytes([0x87, 0x1b, 0x07, 0x00]))
print(f"   md5={hashlib.md5(d).hexdigest()}")
print(f"   gen2 补丁   : {'已打' if d.count(gen2) == 1 else '未打'}")
print(f"   白名单      : {'已打' if wl <= 1 else '未打'} (P104 x{wl})")
PY

    # 备份旧的 DKMS 缓存与 /usr/src 产物
    ts="$(date +%Y%m%d-%H%M%S)"
    [ -f "${DKMS_BUILT}" ] && cp -a "${DKMS_BUILT}" "${DKMS_BUILT}.bak-${ts}" \
        && ok "已备份 DKMS 缓存 -> $(basename "${DKMS_BUILT}").bak-${ts}"
    [ -f "${SRC}" ] && cp -a "${SRC}" "${SRC}.bak-${ts}" \
        && ok "已备份 /usr/src   -> $(basename "${SRC}").bak-${ts}"

    echo
    cp -f "${INSTALLED}" "${DKMS_BUILT}" && ok "DKMS 缓存 <- 装载位"
    cp -f "${INSTALLED}" "${SRC}"        && ok "/usr/src   <- 装载位"

    echo
    show_status
    echo
    ok "已固化。以后 dkms install / 装新内核 autoinstall 都不会再把补丁冲掉。"
    echo "   回滚：把上面 .bak-${ts} 拷回原路径即可。"
}

do_restore() {
    need_root restore

    if [ ! -f "${DKMS_BUILT}" ]; then
        err "找不到 DKMS 缓存 ${DKMS_BUILT}，无法反向恢复"
        exit 1
    fi

    hdr "从 DKMS 缓存恢复 /usr/src"
    cp -f "${DKMS_BUILT}" "${SRC}.from-dkms" && ok "已写出 ${SRC}.from-dkms"
    echo "   确认无误后手动替换："
    echo "     sudo cp ${SRC}.from-dkms ${SRC}"
}

# ============================================================
# 3. main
# ============================================================
main() {
    case "${ACTION}" in
        status)  do_status ;;
        adopt)   do_adopt ;;
        apply)   do_apply ;;
        restore) do_restore ;;
        -h|--help) usage ;;
        *)       err "未知动作：${ACTION}"; usage; exit 2 ;;
    esac
}

main "$@"
