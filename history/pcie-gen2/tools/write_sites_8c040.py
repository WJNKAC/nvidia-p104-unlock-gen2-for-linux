#!/usr/bin/env python3
"""write_sites_8c040.py —— 把 33 个「0x8C040 作为参数」的调用点，逐一钉到函数上

为什么需要它（reports/191 遗留问题）
  reports/191 已确认：`0x8C040` 在 .text 里**没有**直接写指令，
  33 处全是 `mov $0x8c040,%ecx` 后 call 某个 BAR0 访问封装。
  但"这 33 处分别在哪个函数里、调的是读封装还是写封装"从未列全。
  而这正是回答"谁把 SPEED 写回 2"的关键：
  只要某个调用点落在**有入边的活函数**里，它就是候选元凶。

做法（全部基于 ELF 节内偏移，避免三种偏移混淆）
  1. 在 .text 里找 `40 c0 08 00`
  2. 校验前 1~2 字节是 mov-imm 操作码（B8+r / 41 B8+r）⇒ 偏移是当参数传
  3. 从命中点往后 64 字节内找最近的 `e8`（call rel32），
     用 .rela.text 精确解析目标符号（不是靠反汇编猜）
  4. 用 owner() 把调用点映射到函数；用 Graph 的入边数标注"是否被静态引用"

用法
  python3 write_sites_8c040.py <nvidia.ko> [--bt]     # --bt 额外打印 8 字节调用上下文
"""
import struct
import sys

sys.path.insert(0, "/data/labs/nvidia/pcie-gen2/tools")
from callgraph import Elf, Graph

IMM = 0x0008C040  # 注意：0x8C040 的 4 字节小端是 40 c0 08 00
PAT = struct.pack("<I", IMM)


def mov_imm_reg(b1, b2):
    """返回 (是否 mov-imm, 寄存器名)。支持 B8-BF 与 41 B8-41 BF。"""
    R = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di"]
    R8 = ["r8d", "r9d", "r10d", "r11d", "r12d", "r13d", "r14d", "r15d"]
    if 0xB8 <= b1 <= 0xBF:
        return True, "e" + R[b1 - 0xB8]
    if b1 == 0x41 and 0xB8 <= b2 <= 0xBF:
        return True, R8[b2 - 0xB8]
    return False, ""


def main():
    path = sys.argv[1]
    show_bt = "--bt" in sys.argv
    e = Elf(path)
    g = Graph(e)
    t = e.by_name[".text"]
    blob = e.d[t["off"]:t["off"] + t["size"]]
    tidx = t["idx"]

    # 建 call 目标索引：r_offset -> (callee, rt)
    callmap = {}
    for rsec, tsec, r_off, rt, nm, add, shndx, stype in e.relocs():
        if tsec != ".text" or rt not in (2, 4):
            continue
        if nm == "" or nm == ".text" or stype == 3:
            callee, _ = g.owner(add) if shndx == tidx else ("?", 0)
        else:
            callee = nm
        callmap[r_off] = callee

    sites, i = [], 0
    while True:
        i = blob.find(PAT, i)
        if i < 0:
            break
        sites.append(i)
        i += 1

    print(f"{path}\n.text size={t['size']:#x}  命中 '40 c0 08 00'：{len(sites)} 处\n")

    rows = []
    for s in sites:
        # 命中点 = 立即数起点，操作码在其前
        is_mov, reg = False, ""
        if s >= 1:
            is_mov, reg = mov_imm_reg(blob[s - 1], blob[s - 2] if s >= 2 else 0)
        if not is_mov:
            continue                      # 不是 "偏移当参数"，跳过（应为 0 处）
        # 往后找最近 e8 call
        call_off = None
        for k in range(s + 4, min(len(blob) - 5, s + 68)):
            if blob[k] == 0xE8:
                call_off = k
                break
        callee = callmap.get(call_off + 1, f"<indirect@{call_off:#x}>" if call_off else "<none>")
        fn, d = g.owner(s)
        ind = len(g.callers.get(fn, ()))
        rows.append((s, reg, call_off, callee, fn, d, ind))

    print(f"判定为「mov $0x8c040,%reg 后 call」：{len(rows)} 处\n")
    hdr = f"{'节内偏移':>10s} {'reg':5s} {'call@':>9s} {'调用目标':26s} {'所在函数':30s} {'+off':>7s} {'入边':>4s}"
    print(hdr)
    print("-" * len(hdr))
    for s, reg, call_off, callee, fn, d, ind in rows:
        print(f"{s:#10x} {reg:5s} {(call_off or 0):#9x} {callee:26s} {fn:30s} {d:#7x} {ind:4d}")

    # 汇总：按调用目标
    from collections import Counter
    print("\n按调用目标汇总：")
    for k, v in Counter(r[3] for r in rows).most_common():
        print(f"  {k:30s} {v}")

    print("\n按所在函数汇总（入边=该函数被静态引用次数，0 不代表死代码，见 reports/194）：")
    for k, v in Counter(r[4] for r in rows).most_common():
        ind = next(r[6] for r in rows if r[4] == k)
        print(f"  {k:30s} {v} 处   入边 {ind}")

    if show_bt:
        print("\n上下文（命中点前 16 / 后 32 字节）：")
        for s, reg, call_off, callee, fn, d, ind in rows:
            lo, hi = max(0, s - 16), min(len(blob), s + 32)
            print(f"  {s:#x} {fn}+{d:#x} -> {callee}")
            print(f"    {blob[lo:hi].hex(' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
