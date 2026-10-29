#!/bin/bash
# ============================================================
# minimal.sh —— 580.178.04 的最小实现（自包含，不依赖仓库其他文件）
#
# 三个独立补丁，可单独使用：
#   gen2       2 字节   让驱动不再把 PCIe 速率降回 Gen1
#                       （链路还要靠 kmod/gen2kr.ko 推上去，本脚本不管那部分）
#   delay      1 字节   跳过 gr3d 每批次延迟命令（帧率 ~40fps 的上限）
#   whitelist  10 字节  从内核受限设备表里抹掉 P1XX 的 device ID（治本）
#
# 用法：
#   sudo bash minimal.sh <目标> <动作>
#
#   目标：gen2 | delay | whitelist | all
#   动作：check | apply | revert
#
#   例：
#     sudo bash minimal.sh gen2 check
#     sudo bash minimal.sh gen2 apply
#     sudo bash minimal.sh all check
#
# 备份策略：
#   apply 前自动创建 <文件>.orig（只建一次，revert 不删）
# ============================================================
set -u

# ============================================================
# 1. 变量
# ============================================================

# 同目录下的偏移定义（本脚本的唯一外部依赖）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OFFSETS_FILE="${SCRIPT_DIR}/offsets.sh"

VALID_TARGETS=(gen2 delay whitelist)
VALID_ACTIONS=(check apply revert)

# 退出码
EXIT_OK=0
EXIT_FAIL=1
EXIT_USAGE=2
EXIT_STATE=3            # 文件状态不符合预期（已打过 / 没打过 / 被别的改过）

# 颜色（非 tty 自动关闭）
if [ -t 1 ]; then
    C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
    C_R=; C_G=; C_Y=; C_B=; C_0=
fi

# ============================================================
# 2. 函数
# ============================================================

ok()   { echo "${C_G}✓${C_0} $*"; }
warn() { echo "${C_Y}⚠${C_0} $*"; }
err()  { echo "${C_R}✗${C_0} $*"; }
hdr()  { echo; echo "${C_B}── $* ──${C_0}"; }

# 打印用法
usage() {
    cat <<'EOF'
用法: sudo bash minimal.sh <目标> <动作>

目标:
  gen2        PCIe Gen2 解锁（nvidia.ko，2 字节）
  delay       跳过 gr3d 每批次延迟（libnvidia-glcore.so，1 字节）
  whitelist   内核受限设备表（nv-kernel.o_binary，10 字节）
  all         对三个都执行

动作:
  check       只读检查当前状态
  apply       打补丁（自动备份 .orig）
  revert      还原

例:
  sudo bash minimal.sh gen2 check
  sudo bash minimal.sh delay apply
  sudo bash minimal.sh all check
EOF
}

# 加载偏移定义
load_offsets() {
    if [ ! -f "${OFFSETS_FILE}" ]; then
        err "找不到 ${OFFSETS_FILE}"
        echo "    这个脚本必须和 offsets.sh 放在同一目录"
        exit "${EXIT_FAIL}"
    fi
    # shellcheck source=offsets.sh
    source "${OFFSETS_FILE}"
    ok "已加载偏移定义（驱动 ${DRIVER_VERSION}，内核 ${KERNEL_VERIFIED}）"
}

# 检查是否 root（只有 apply/revert 需要，check 也不要紧）
need_root() {
    [ "$(id -u)" = "0" ] || { err "需要 root：sudo bash $0 $*"; exit "${EXIT_FAIL}"; }
}

# 构造 gen2 的 JSON 规格
spec_gen2() {
    cat <<EOF
{
  "name": "gen2",
  "desc": "让驱动不再把 PCIe 速率降回 Gen1",
  "path": "${KO_PATH}",
  "anchor": {"mode": "sig", "sig": "${GEN2_SIG_HEX}"},
  "edits": [
    {"off": $(( GEN2_OFF_LEA )), "old": "${GEN2_ORIG_LEA}", "new": "${GEN2_NEW_LEA}",
     "what": "lea -0x1(%rdx) -> lea -0x2(%rdx)  (ff -> fe)"},
    {"off": $(( GEN2_OFF_CMP )), "old": "${GEN2_ORIG_CMP}", "new": "${GEN2_NEW_CMP}",
     "what": "cmp 0x2 -> cmp 0x1  (02 -> 01)"}
  ],
  "md5_before": "${GEN2_MD5_BEFORE}",
  "md5_after": "${GEN2_MD5_AFTER}",
  "after_note": "改完必须重载 nvidia 模块才生效；链路还需 gen2kr.ko 推送"
}
EOF
}

# 构造 delay 的 JSON 规格
spec_delay() {
    cat <<EOF
{
  "name": "delay",
  "desc": "跳过 gr3d 每批次延迟命令（~40fps 上限）",
  "path": "${GLOCORE_PATH}",
  "anchor": {"mode": "offset", "off": 0},
  "edits": [
    {"off": $(( DELAY_OFFSET )), "old": "0x74", "new": "0xeb",
     "what": "je -> jmp（无条件跳过延迟命令）"}
  ],
  "md5_before": "${DELAY_MD5_BEFORE}",
  "md5_after": "${DELAY_MD5_AFTER}",
  "after_note": "不需要重启，重新启动图形程序即可生效"
}
EOF
}

# 构造 whitelist 的 JSON 规格
spec_whitelist() {
    local edits="" i off dev
    for i in "${!WHITELIST_DEV_IDS[@]}"; do
        dev="${WHITELIST_DEV_IDS[$i]}"
        off=$(( WHITELIST_OFFSET + i * 4 ))
        # 低 16 位按小端拼成 4 个 hex 字符（只改这 2 字节，保留 0x0007 前缀）
        edits+=$(printf '{"off": %d, "old": "%02x%02x", "new": "ffff", "what": "device 0x%04x -> 0xffff（抹掉）"},' \
                 "${off}" $(( dev & 0xff )) $(( (dev >> 8) & 0xff )) "${dev}")
    done
    edits="${edits%,}"     # 去掉末尾逗号

    cat <<EOF
{
  "name": "whitelist",
  "desc": "从内核受限设备表里抹掉 P1XX 的 device ID",
  "path": "${NVKERNEL_PATH}",
  "anchor": {"mode": "offset", "off": 0},
  "edits": [${edits}],
  "md5_before": "${WHITELIST_MD5_BEFORE}",
  "md5_after": "${WHITELIST_MD5_AFTER}",
  "after_note": "改完必须重建内核模块并重载：cd /usr/src/nvidia-${DRIVER_VERSION} && sudo make"
}
EOF
}

# 取某个目标的 JSON 规格
spec_for() {
    case "$1" in
        gen2)      spec_gen2 ;;
        delay)     spec_delay ;;
        whitelist) spec_whitelist ;;
        *)         err "未知目标 $1"; exit "${EXIT_USAGE}" ;;
    esac
}

# 对单个目标执行一个动作（核心逻辑在 python 里）
run_one() {
    local target="$1" action="$2" spec
    spec="$(spec_for "${target}")"

    python3 - "${action}" "${spec}" "${0}" <<'PY'
import hashlib
import json
import os
import shutil
import sys

ACTION = sys.argv[1]
SPEC = json.loads(sys.argv[2])
SCRIPT_PATH = sys.argv[3]

C_R, C_G, C_Y, C_B, C_0 = "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"
if not sys.stdout.isatty():
    C_R = C_G = C_Y = C_B = C_0 = ""


def ok(msg):   print(f"{C_G}✓{C_0} {msg}")
def warn(msg): print(f"{C_Y}⚠{C_0} {msg}")
def err(msg):  print(f"{C_R}✗{C_0} {msg}")


def parse_hex(s):
    """'ff' / '0xff' 都接受（一律按十六进制），返回 bytes"""
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


name = SPEC["name"]
desc = SPEC["desc"]
path = SPEC["path"]
self_name = os.path.basename(SCRIPT_PATH)

print()
print(f"{C_B}── {name}：{desc} ──{C_0}")
print(f"   文件: {path}")

# ---------- 文件存在性 ----------
if not os.path.exists(path):
    err(f"文件不存在：{path}")
    print(f"    这个版本的补丁是为驱动 {name} 准备的，请确认驱动版本和安装路径")
    sys.exit(3)

data = bytearray(open(path, "rb").read())
md5_now = hashlib.md5(data).hexdigest()

# ---------- 定位锚点 ----------
anchor = SPEC["anchor"]
if anchor["mode"] == "sig":
    sig = bytes.fromhex(anchor["sig"])
    positions, i = [], data.find(sig)
    while i != -1:
        positions.append(i)
        i = data.find(sig, i + 1)

    if len(positions) == 1:
        base = positions[0]
        ok(f"签名命中 1 处（文件偏移 0x{base:x}）")
    elif len(positions) == 0:
        # 可能是已打过补丁（签名被改了一个字节），用补丁后的签名再找
        patched = bytearray(sig)
        for e in SPEC["edits"]:
            patched[e["off"]] = parse_hex(e["new"])[0]
        positions, i = [], data.find(bytes(patched))
        while i != -1:
            positions.append(i)
            i = data.find(bytes(patched), i + 1)
        if len(positions) == 1:
            base = positions[0]
            ok(f"原始签名未命中，但补丁后签名命中 1 处（0x{base:x}）⇒ 应为已打补丁")
        else:
            err("签名一处都没命中 —— 驱动版本不匹配？")
            print("    请先确认驱动版本，再重新定位签名（不要硬套本版本的偏移）")
            sys.exit(3)
    else:
        err(f"签名命中 {len(positions)} 处（应为 1 处）—— 无法安全定位")
        print("    拒绝继续。请人工确认。")
        sys.exit(3)
else:
    base = anchor["off"]
    ok(f"使用固定偏移（基点 0x{base:x}）")

# ---------- 检查每处 edit 的当前状态 ----------
states = []       # 每处: "orig" / "new" / "unknown"
for e in SPEC["edits"]:
    off = base + e["off"]
    old = parse_hex(e["old"])
    new = parse_hex(e["new"])
    cur = bytes(data[off:off + len(old)])
    if cur == old:
        states.append(("orig", off, cur, old, new))
    elif cur == new:
        states.append(("new", off, cur, old, new))
    else:
        states.append(("unknown", off, cur, old, new))

n_orig = sum(1 for s in states if s[0] == "orig")
n_new = sum(1 for s in states if s[0] == "new")
n_unk = sum(1 for s in states if s[0] == "unknown")

# ---------- check：只报告 ----------
print()
if ACTION == "check":
    print(f"   md5 = {md5_now}")
    for s in states:
        st, off, cur, old, new = s
        mark = {"orig": "未打", "new": "已打", "unknown": "未知"}[st]
        print(f"   0x{off:08x}  当前={cur.hex():<6s} {mark}")

    print()
    if n_orig == len(states):
        warn("状态：未打补丁")
        print(f"    打补丁: sudo bash {self_name} {name} apply")
    elif n_new == len(states):
        ok("状态：已打补丁")
        print(f"    还原:   sudo bash {self_name} {name} revert")
    else:
        err(f"状态异常：{n_orig} 处原始 / {n_new} 处已改 / {n_unk} 处未知")
        print("    文件可能被别的工具改过，或驱动版本不匹配。不要盲目 apply。")
        sys.exit(3)
    sys.exit(0)

# ---------- apply / revert：写前必须状态一致 ----------
want_from = "orig" if ACTION == "apply" else "new"
if not all(s[0] == want_from for s in states):
    err(f"状态不符合预期，拒绝 {ACTION}")
    for s in states:
        st, off, cur, old, new = s
        print(f"   0x{off:08x}  当前={cur.hex()}  期望={'原始 ' + old.hex() if ACTION == 'apply' else '已打 ' + new.hex()}")
    sys.exit(3)

# 备份（只在 apply 且 .orig 不存在时创建）
bak = path + ".orig"
if ACTION == "apply" and not os.path.exists(bak):
    shutil.copy2(path, bak)
    ok(f"已备份 → {os.path.basename(bak)}")

# 写回
for s in states:
    st, off, cur, old, new = s
    target = new if ACTION == "apply" else old
    data[off:off + len(target)] = target

open(path, "wb").write(data)

md5_after_write = hashlib.md5(data).hexdigest()
print()
for e, s in zip(SPEC["edits"], states):
    st, off, cur, old, new = s
    target = new if ACTION == "apply" else old
    print(f"   0x{off:08x}  {cur.hex()} → {target.hex()}   {e['what']}")

print()
ok(f"{ACTION} 完成")
print(f"   md5: {md5_now} → {md5_after_write}")

# md5 校验（仅提示，不阻断 —— 因为可能已叠加了别的补丁）
# apply 时：写前应等于 md5_before，写后应等于 md5_after
# revert 时：反过来
if ACTION == "apply":
    checks = [("写前", md5_now, SPEC.get("md5_before")),
              ("写后", md5_after_write, SPEC.get("md5_after"))]
else:
    checks = [("写前", md5_now, SPEC.get("md5_after")),
              ("写后", md5_after_write, SPEC.get("md5_before"))]

for label, got, expect in checks:
    if not expect:
        continue
    if got == expect:
        ok(f"md5 {label}与记录一致（{expect[:8]}…）")
    else:
        warn(f"md5 {label}与记录不一致（记录 {expect[:8]}…，实际 {got[:8]}…）")
        print("      不影响功能，但说明这份文件可能叠加了其他改动")

if ACTION == "apply" and SPEC.get("after_note"):
    print()
    print(f"   ★ {SPEC['after_note']}")

sys.exit(0)
PY
}

# 对目标执行动作；all 则逐个执行
run_target() {
    local target="$1" action="$2" t rc=0

    if [ "${target}" = "all" ]; then
        for t in "${VALID_TARGETS[@]}"; do
            run_one "${t}" "${action}" || rc=1
        done
        return "${rc}"
    fi

    run_one "${target}" "${action}"
}

# 参数校验
validate_args() {
    local target="${1:-}" action="${2:-}" t a

    if [ -z "${target}" ] || [ -z "${action}" ]; then
        usage
        exit "${EXIT_USAGE}"
    fi

    local target_ok=0
    for t in "${VALID_TARGETS[@]}" all; do
        [ "${target}" = "${t}" ] && target_ok=1
    done
    [ "${target_ok}" = "1" ] || { err "未知目标：${target}"; usage; exit "${EXIT_USAGE}"; }

    local action_ok=0
    for a in "${VALID_ACTIONS[@]}"; do
        [ "${action}" = "${a}" ] && action_ok=1
    done
    [ "${action_ok}" = "1" ] || { err "未知动作：${action}"; usage; exit "${EXIT_USAGE}"; }
}

# ============================================================
# 3. main
# ============================================================
main() {
    local target="${1:-}" action="${2:-}"

    validate_args "${target}" "${action}"

    # 先加载偏移（后面要用 DRIVER_VERSION 等变量）
    load_offsets

    echo "======================================================"
    echo " NVIDIA ${DRIVER_VERSION} 最小补丁实现"
    echo " 目标=${target}  动作=${action}"
    echo "======================================================"

    # check 不需要 root，apply/revert 需要
    if [ "${action}" != "check" ]; then
        need_root "${target}" "${action}"
        echo "   ★ apply/revert 会改二进制文件，已有 .orig 备份可随时 revert"
    fi

    run_target "${target}" "${action}"
    exit $?
}

main "$@"
