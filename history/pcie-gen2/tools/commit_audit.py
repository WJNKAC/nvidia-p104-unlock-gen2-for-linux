#!/usr/bin/env python3
"""commit_audit.py —— 审计所有「写 0x8C040」的调用点，看谁会置 bit0(COMMIT)

## 为什么必须查（reports/198）

reports/186 定案过一条：
    「写 0x8C040.bit0(COMMIT) → 任何状态都必死」

但读 `_nv017105rm`（驱动自己的 `set_link_speed`）反汇编时发现：

```asm
127f40: and  $0xfffffff0,%r15d    ; 清 bits[3:0]
127f47: mov  $0x8c040,%ecx
127f4e: or   $0x1,%r15d           ; ★ 置 bit0 = COMMIT
127f5a: call <写 0x8C040>
```

**驱动自己就写 COMMIT。** 说明 COMMIT 不是"毒药"，而是驱动正常流程里的
「提交/生效」步骤 —— 我们把外部乱写导致的掉卡**误归因**给了 COMMIT 本身。

真正致命的是"缺前置步骤就写"（比如 SPEED 未写、训练未完成），
COMMIT 只是把已有改动 latch 下去。

## 本脚本做什么

遍历 `0x8c040` 作为参数（`mov $0x8c040,%reg` 后 call 写封装）的**全部**调用点，
对每个点往前看 48 字节，判定：

  · 值寄存器里是否出现 `or $0x1` / `or $0x10`（bits[3:0]）⇒ 可能置 COMMIT
  · 是否有 `and $0xfffffff0` ⇒ 先清低 4 位再置位（驱动那种写法）

只做静态判定，不碰设备。

用法：
  python3 commit_audit.py <nvidia.ko>
"""
import struct
import sys

sys.path.insert(0, "/data/labs/nvidia/pcie-gen2/tools")
from callgraph import Elf, Graph

PAT = struct.pack("<I", 0x8C040)


def mov_imm_reg(b1, b2):
    R = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di"]
    R8 = ["r8d", "r9d", "r10d", "r11d", "r12d", "r13d", "r14d", "r15d"]
    if 0xB8 <= b1 <= 0xBF:
        return True, "e" + R[b1 - 0xB8]
    if b1 == 0x41 and 0xB8 <= b2 <= 0xBF:
        return True, R8[b2 - 0xB8]
    return False, ""


def find_or1(blob, lo, hi):
    """在 [lo,hi) 里找 or $0x1,%reg 或 or $0x10 形式的指令。

    编码：
      83 /1 ib          = or  r/m32, imm8      （ModRM 的 reg 字段 = 1）
      41 83 /1 ib       = REX.B + or r/m32, imm8
    """
    out = []
    for k in range(lo, hi - 2):
        if blob[k] != 0x83:
            continue
        modrm = blob[k + 1]
        if ((modrm >> 3) & 7) != 1:      # /1 = OR
            continue
        if (modrm >> 6) != 3:            # mod=11 = 寄存器
            continue
        imm = blob[k + 2]
        if imm & 0x0F or imm in (0x11,):  # 只关心 touches bits[3:0]
            reg = modrm & 7
            out.append((k, reg, imm, imm & 0xF))
    return out


def main():
    path = sys.argv[1]
    e = Elf(path)
    g = Graph(e)
    t = e.by_name[".text"]
    blob = e.d[t["off"]:t["off"] + t["size"]]

    sites = []
    i = 0
    while True:
        i = blob.find(PAT, i)
        if i < 0:
            break
        b1, b2 = blob[i - 1], blob[i - 2] if i >= 2 else 0
        ok, reg = mov_imm_reg(b1, b2)
        if ok:
            sites.append((i, reg))
        i += 1

    print(f"{path}\n.text size={t['size']:#x}   'mov $0x8c040,%reg' 命中 {len(sites)} 处\n")
    print(f"{'节内偏移':>10s} {'reg':6s} {'所在函数':26s} {'低4位写入':>10s}  判定")
    print("-" * 92)

    # 建一个 call 目标索引，用于确定窗口右边界
    callmap = {}
    for rsec, tsec, r_off, rt, nm, add, shndx, stype in e.relocs():
        if tsec == ".text" and rt in (2, 4):
            callmap[r_off] = nm or "?"

    suspicious = 0
    for off, reg in sites:
        fn, d = g.owner(off)
        # ★ 窗口必须是 [mov-imm 之前, call 之前]：
        #   驱动那种写法是 "mov $0x8c040,%ecx ... or $0x1,%r15d ... call 写"
        #   即 COMMIT 位是在**加载偏移之后**才置上的。
        #   （第一版只往前扫 48 字节，把 127f4e 那个真 COMMIT 漏掉了 —— 已修正）
        lo = max(0, off - 48)
        hi = off + 4
        for k in range(off + 4, min(len(blob) - 5, off + 64)):
            if blob[k] == 0xE8:
                hi = k
                break
        ors = find_or1(blob, lo, hi)
        hits = [x for x in ors if x[3] != 0]

        if hits:
            suspicious += 1
            desc = ", ".join(f"or ${x[2]:02x},r{x[1]}" for x in hits[:3])
            verdict = "★ 可能置 COMMIT 等低位"
        else:
            desc = "-"
            verdict = ""

        print(f"{off:#10x} {reg:6s} {fn:26s} {desc:>10s}  {verdict}")

    print()
    print("=" * 92)
    print(f"结论：{len(sites)} 个写点中，{suspicious} 个在调用前对值寄存器写过 bits[3:0]。")
    print()
    print("★ 判读纪律（reports/198）")
    print("  1. `or $0x1` 不一定是 COMMIT —— 也可能是别的寄存器/字段。")
    print("     必须结合反汇编上下文看它操作的是不是 0x8C040 的待写值。")
    print("  2. 已知事实：`_nv017105rm+0x1ff`（127f4e）确实 `or $0x1,%r15d`，")
    print("     紧接着 `call 写 0x8c040`，中间没有别的写 ⇒ 这是真 COMMIT。")
    print("  3. ⇒ reports/186 的『写 COMMIT 必死』是**误归因**：")
    print("     驱动正常流程就写 COMMIT。致命的是『缺前置步骤就写』。")
    print("  4. ⚠ 这不代表我们可以随便写 COMMIT —— 仍然不要写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
