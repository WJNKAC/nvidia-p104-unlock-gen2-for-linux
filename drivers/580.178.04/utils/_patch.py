#!/usr/bin/env python3
"""_patch.py —— JSON 规格驱动的通用二进制补丁器

被 00-whitelist.sh / 01-gen2-patch.sh 调用，不直接运行。

用法（内部）：
    python3 _patch.py <action> <spec_json>

spec 结构：
    {
      "name": "gen2",
      "desc": "让驱动不再把 PCIe 速率降回 Gen1",
      "path": "/lib/modules/<kver>/updates/dkms/nvidia.ko",
      "anchor": {"mode": "sig", "sig": "<hex>"}   或  {"mode": "offset", "off": 0},
      "edits": [
        {"off": 2, "old": "ff", "new": "fe", "what": "lea -1 -> lea -2"}
      ],
      "md5_before": "...",      # 可选，仅用于提示
      "md5_after": "...",       # 可选
      "after_note": "..."       # 可选，apply 后打印
    }

设计要点：
  · 三态判定：每处 edit 只有 "全 old" / "全 new" / "未知" 三种状态
    只有「全 old」才允许 apply，只有「全 new」才允许 revert —— 混合态直接拒绝
  · 备份 <path>.orig 只在 apply 且不存在时创建（revert 不删）
  · md5 只作提示不阻断（文件可能叠加了别的补丁）
  · 退出码：0 成功 / 1 失败 / 3 状态不符（拒绝操作）

签名锚点（mode=sig）：
  原始签名找不到时，会拿「补丁后的签名」再找一次 —— 这是为了正确识别
  "已打过补丁" 的状态，而不是误报"驱动版本不匹配"。
"""
import hashlib
import json
import os
import shutil
import sys

# ============================================================
# 颜色与输出
# ============================================================
C_R, C_G, C_Y, C_B, C_0 = (
    "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m")
if not sys.stdout.isatty():
    C_R = C_G = C_Y = C_B = C_0 = ""


def ok(msg):   print(f"{C_G}✓{C_0} {msg}")
def warn(msg): print(f"{C_Y}⚠{C_0} {msg}")
def err(msg):  print(f"{C_R}✗{C_0} {msg}")


# ============================================================
# 工具
# ============================================================
def parse_hex(s):
    """'ff' / '0xff' 都接受（一律按十六进制），返回 bytes"""
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


def find_all(data, pattern):
    """返回 pattern 在 data 里所有出现位置"""
    positions, i = [], data.find(pattern)
    while i != -1:
        positions.append(i)
        i = data.find(pattern, i + 1)
    return positions


def make_patched_sig(sig, edits):
    """把原始签名按 edits 改成「补丁后」的形态，用于识别已打状态"""
    buf = bytearray(sig)
    for e in edits:
        new = parse_hex(e["new"])
        buf[e["off"]:e["off"] + len(new)] = new
    return bytes(buf)


# ============================================================
# 定位锚点
# ============================================================
def locate(data, spec):
    """返回锚点基址。找不到 / 不唯一都直接退出。"""
    anchor = spec["anchor"]

    if anchor["mode"] == "offset":
        base = anchor["off"]
        ok(f"使用固定偏移（基点 0x{base:x}）")
        return base

    sig = bytes.fromhex(anchor["sig"])
    hits = find_all(data, sig)

    if len(hits) == 1:
        base = hits[0]
        ok(f"签名命中 1 处（文件偏移 0x{base:x}）")
        return base

    if len(hits) > 1:
        err(f"签名命中 {len(hits)} 处（应为 1 处）—— 无法安全定位")
        print("    拒绝继续。请人工确认。")
        sys.exit(3)

    # 原始签名找不到：可能是已打过补丁，用补丁后的签名再试
    patched = make_patched_sig(sig, spec["edits"])
    hits = find_all(data, patched)
    if len(hits) == 1:
        base = hits[0]
        ok(f"原始签名未命中，但补丁后签名命中 1 处（0x{base:x}）⇒ 应为已打补丁")
        return base

    err("签名一处都没命中 —— 驱动版本不匹配？")
    print("    请先确认驱动版本，再重新定位签名（不要硬套本版本的偏移）")
    sys.exit(3)


# ============================================================
# 逐处检查状态
# ============================================================
def scan_states(data, base, edits):
    """返回 [(state, off, cur, old, new), ...]，state ∈ orig/new/unknown"""
    states = []
    for e in edits:
        off = base + e["off"]
        old = parse_hex(e["old"])
        new = parse_hex(e["new"])
        cur = bytes(data[off:off + len(old)])
        if cur == old:
            st = "orig"
        elif cur == new:
            st = "new"
        else:
            st = "unknown"
        states.append((st, off, cur, old, new))
    return states


# ============================================================
# 动作：check
# ============================================================
def do_check(spec, data, md5_now, states, self_name):
    n_orig = sum(1 for s in states if s[0] == "orig")
    n_new = sum(1 for s in states if s[0] == "new")
    n_unk = sum(1 for s in states if s[0] == "unknown")

    print(f"   md5 = {md5_now}")
    for st, off, cur, old, new in states:
        mark = {"orig": "未打", "new": "已打", "unknown": "未知"}[st]
        print(f"   0x{off:08x}  当前={cur.hex():<6s} {mark}")

    print()
    if n_orig == len(states):
        warn("状态：未打补丁")
        print(f"    打补丁: sudo bash {self_name} apply")
    elif n_new == len(states):
        ok("状态：已打补丁")
        print(f"    还原:   sudo bash {self_name} revert")
    else:
        err(f"状态异常：{n_orig} 处原始 / {n_new} 处已改 / {n_unk} 处未知")
        print("    文件可能被别的工具改过，或驱动版本不匹配。不要盲目 apply。")
        sys.exit(3)
    sys.exit(0)


# ============================================================
# 动作：apply / revert
# ============================================================
def do_write(action, spec, data, md5_now, states, path):
    want_from = "orig" if action == "apply" else "new"

    # 状态必须「全 orig」或「全 new」，混合态拒绝
    if not all(s[0] == want_from for s in states):
        err(f"状态不符合预期，拒绝 {action}")
        for st, off, cur, old, new in states:
            want = ("原始 " + old.hex()) if action == "apply" else ("已打 " + new.hex())
            print(f"   0x{off:08x}  当前={cur.hex()}  期望={want}")
        sys.exit(3)

    # 备份：只在 apply 且 .orig 不存在时创建
    bak = path + ".orig"
    if action == "apply" and not os.path.exists(bak):
        shutil.copy2(path, bak)
        ok(f"已备份 → {os.path.basename(bak)}")

    # 写回
    for st, off, cur, old, new in states:
        target = new if action == "apply" else old
        data[off:off + len(target)] = target

    with open(path, "wb") as f:
        f.write(data)

    md5_after = hashlib.md5(data).hexdigest()

    print()
    for e, (st, off, cur, old, new) in zip(spec["edits"], states):
        target = new if action == "apply" else old
        print(f"   0x{off:08x}  {cur.hex()} → {target.hex()}   {e['what']}")

    print()
    ok(f"{action} 完成")
    print(f"   md5: {md5_now} → {md5_after}")

    # md5 校验（仅提示，不阻断）
    # apply 时：写前=before、写后=after；revert 时反过来
    if action == "apply":
        checks = [("写前", md5_now, spec.get("md5_before")),
                  ("写后", md5_after, spec.get("md5_after"))]
    else:
        checks = [("写前", md5_now, spec.get("md5_after")),
                  ("写后", md5_after, spec.get("md5_before"))]

    for label, got, expect in checks:
        if not expect:
            continue
        if got == expect:
            ok(f"md5 {label}与记录一致（{expect[:8]}…）")
        else:
            warn(f"md5 {label}与记录不一致（记录 {expect[:8]}…，实际 {got[:8]}…）")
            # 说明为什么会不一致 —— 最常见的原因就是"先打了 00 白名单"
            print("      ⓘ 这不是错误，不影响执行（md5 仅用于提示，不阻断）。")
            print("        最常见原因：这份文件上还叠了别的补丁 / 重新编译过。")
            print("        例如先跑过 00-whitelist（改 nv-kernel.o_binary → 重编 nvidia.ko），")
            print("        nvidia.ko 的 md5 就必然和「原厂」记录对不上 —— 这是预期行为。")
            print("        真正的安全网是：32 字节签名唯一定位 + 三态校验（混合态拒绝）。")

    if action == "apply" and spec.get("after_note"):
        print()
        print(f"   ★ {spec['after_note']}")


# ============================================================
# main
# ============================================================
def main():
    if len(sys.argv) < 4:
        err("内部用法: _patch.py <action> <spec_json> <parent_script>")
        return 1

    action = sys.argv[1]
    spec = json.loads(sys.argv[2])
    self_name = os.path.basename(sys.argv[3])

    path = spec["path"]
    print(f"   文件: {path}")

    if not os.path.exists(path):
        err(f"文件不存在：{path}")
        print("    请确认驱动版本与安装路径")
        return 3

    data = bytearray(open(path, "rb").read())
    md5_now = hashlib.md5(data).hexdigest()

    base = locate(data, spec)
    states = scan_states(data, base, spec["edits"])

    if action == "check":
        do_check(spec, data, md5_now, states, self_name)
    elif action in ("apply", "revert"):
        do_write(action, spec, data, md5_now, states, path)
    else:
        err(f"未知动作 {action}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
