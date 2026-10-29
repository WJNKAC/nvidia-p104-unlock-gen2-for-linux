#!/usr/bin/env python3
"""rel_tables.py —— 挖出 .text 内部的「相对地址表」（无重定位的间接分派）

为什么必须做这个（reports/194）
  用重定位建调用图时发现：**61,067 个 FUNC 符号里 67.5% 零入边**，
  连 53KB 的 `_nv020152rm`、12KB 的 `_nv005948rm`（HAL 安装器）都是零入边。
  这不可能是死代码，只能是"到达方式对重定位不可见"。

  机制找到了：`.long target - .` 这种**同节相对偏移表**，
  汇编器在汇编期就能把差值算成常量，**不产生任何重定位**。
  ⇒ 重定位图系统性地漏掉所有通过这类表分派的函数。
  ⇒ "零入边"**不能**作为死代码的证据（这是本工具存在的理由）。

做法
  对 .text 里每个 4 字节位置 p 读 int32 V，算 t = p + V，
  若 t 恰好是某个函数入口，则 (p, t) 是一个相对表项。
  连续的表项聚成一张表。

输出
  - 统计：多少位置命中、聚成多少张表、覆盖多少函数
  - `--target SYM`：只报告包含该符号的表（回答"它是通过哪张表被调到的"）
  - `--table ADDR`：dump 某张表的完整内容

用法
  python3 rel_tables.py <nvidia.ko> --stats
  python3 rel_tables.py <nvidia.ko> --target _nv005948rm
  python3 rel_tables.py <nvidia.ko> --target _nv017281rm --dump
"""
import bisect
import struct
import sys

sys.path.insert(0, "/data/labs/nvidia/pcie-gen2/tools")
from callgraph import Elf, Graph


def build(elf, g, step=1):
    t = elf.by_name[".text"]
    blob = elf.d[t["off"]:t["off"] + t["size"]]
    n = (len(blob) // 4) * 4
    entries = set(g.addrs)          # 函数入口集合
    hits = {}                       # p -> t
    # 用 iter_unpack 走 C 循环，比逐次 unpack_from 快一个量级
    vals = struct.iter_unpack("<i", blob[:n])
    for i, (v,) in enumerate(vals):
        p = i * 4
        tt = p + v
        if tt in entries:
            hits[p] = tt
    return blob, hits


def group(hits):
    """把相邻/近邻的命中聚成表。返回 [(start, end, [targets])]。"""
    ps = sorted(hits)
    tables = []
    cur = []
    for p in ps:
        if cur and p - cur[-1] <= 8:      # 允许 4~8 字节间隔视为同一张表
            cur.append(p)
        else:
            if cur:
                tables.append(cur)
            cur = [p]
    if cur:
        tables.append(cur)
    return [(c[0], c[-1] + 4, [hits[p] for p in c]) for c in tables]


def main():
    path = sys.argv[1]
    do_stats = "--stats" in sys.argv
    dump = "--dump" in sys.argv
    target = None
    if "--target" in sys.argv:
        target = sys.argv[sys.argv.index("--target") + 1]

    elf = Elf(path)
    g = Graph(elf)
    print(f"{path}\n.text size={elf.by_name['.text']['size']:#x}")
    blob, hits = build(elf, g)
    print(f"相对表项命中：{len(hits)} 个位置，覆盖 {len(set(hits.values()))} 个不同函数入口")

    tabs = group(hits)
    multi = [x for x in tabs if len(x[2]) >= 4]
    print(f"聚成 {len(tabs)} 张表，其中 ≥4 项的 {len(multi)} 张")
    print(f"这些表共覆盖 {len(set(e for _, _, ts in tabs for e in ts))} 个函数入口")

    if target:
        s = g.byname.get(target)
        if not s:
            print(f"\n找不到符号 {target}")
            return 1
        tgt = s["val"]
        holder = [(a, b, ts) for a, b, ts in tabs if tgt in ts]
        print(f"\n=== {target} (val={tgt:#x}) 出现在 {len(holder)} 张相对表中 ===")
        for a, b, ts in holder:
            idx = ts.index(tgt)
            fn, d = g.owner(a)
            print(f"  表 @ .text+{a:#x} ~ {b:#x}  共 {len(ts)} 项  第 {idx} 项")
            print(f"    表所在函数: {fn}+{d:#x}")
            if dump:
                for k, e in enumerate(ts):
                    nm, dd = g.owner(e)
                    mark = "  ★" if e == tgt else ""
                    print(f"      [{k:3d}] {e:#x} {nm}+{dd:#x}{mark}")
        if not holder:
            print("  未出现在任何相对表中 ⇒ 也不是相对表分派的目标")
            print("  （仍需注意：还可能是 基址+索引×步长，或运行时构造的绝对表）")

    if do_stats:
        print("\n最大的 15 张相对表：")
        for a, b, ts in sorted(multi, key=lambda x: -len(x[2]))[:15]:
            fn, d = g.owner(a)
            print(f"  .text+{a:#09x} ~ {b:#09x}  {len(ts):4d} 项   位于 {fn}+{d:#x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
