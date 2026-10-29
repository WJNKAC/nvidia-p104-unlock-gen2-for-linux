#!/usr/bin/env python3
"""analyze_logs.py —— 跨实验日志挖掘：什么条件下写 SPEED 会掉卡

不依赖实验机。从 pcie-gen2/log-*.log 里抽取每次「写 0x8C040」时的
前置条件与结果，做条件-结果对照表。

抽取的字段：
  · 驱动是否绑定（bound=True/False）
  · 写前 0x8841C / LINK_CAP2 / 0x8C040
  · 是否带 COMMIT
  · 写后是否读到 0xffffffff（掉卡）
  · 之后的链路状态
"""
import glob
import os
import re
import sys

LOGDIR = "/data/labs/nvidia/pcie-gen2"

RE_BOUND = re.compile(r"bound=(True|False)")
RE_CAP2 = re.compile(r"CAP2=(0x[0-9a-f]+)")
RE_LNKCFG = re.compile(r"lnkcfg0=(0x[0-9a-f]+)")
RE_WRITE = re.compile(r"0x8C040\s*=\s*(0x[0-9a-f]+)\s*→\s*(?:写入\s*)?(0x[0-9a-f]+)")
RE_WRITE2 = re.compile(r"写 0x8C040:\s*(0x[0-9a-f]+)\s*→\s*(0x[0-9a-f]+)")
RE_DEAD = re.compile(r"0xffffffff|端点已死|端点死了|端点读不到|端点 MMIO 已死")
RE_LNKSTA = re.compile(r"LNKSTA=(0x[0-9a-f]+)")
RE_EP = re.compile(r"端点\s*LNKSTA=(0x[0-9a-f]+)")


def parse(path):
    """返回该日志里所有"写 SPEED"事件的列表"""
    events = []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return events

    bound = None
    cap2 = None
    lnkcfg = None
    for i, ln in enumerate(lines):
        m = RE_BOUND.search(ln)
        if m:
            bound = (m.group(1) == "True")
        m = RE_CAP2.search(ln)
        if m:
            cap2 = int(m.group(1), 16)
        m = RE_LNKCFG.search(ln)
        if m:
            lnkcfg = int(m.group(1), 16)

        w = RE_WRITE.search(ln) or RE_WRITE2.search(ln)
        if not w:
            continue
        old, new = int(w.group(1), 16), int(w.group(2), 16)
        # 只看真正改了 SPEED 字段的
        if (old >> 18) & 3 == (new >> 18) & 3:
            continue
        commit = bool(new & 1)
        speed = (new >> 18) & 3

        # 往后 6 行内找死亡信号
        tail = "\n".join(lines[i + 1:i + 7])
        dead = bool(RE_DEAD.search(tail))
        # 找随后读到的端点 LNKSTA
        lnksta = None
        for t in lines[i + 1:i + 10]:
            m2 = RE_EP.search(t) or RE_LNKSTA.search(t)
            if m2:
                lnksta = int(m2.group(1), 16)
                break

        events.append({
            "file": os.path.basename(path),
            "line": i + 1,
            "bound": bound,
            "cap2": cap2,
            "lnkcfg": lnkcfg,
            "speed": speed,
            "commit": commit,
            "dead": dead,
            "lnksta": lnksta,
        })
    return events


def main():
    files = sorted(glob.glob(os.path.join(LOGDIR, "log-*.log")))
    print(f"扫描 {len(files)} 份日志\n")
    allev = []
    for f in files:
        evs = parse(f)
        if evs:
            allev.extend(evs)

    print(f"共提取 {len(allev)} 次「修改 SPEED 字段」事件\n")
    if not allev:
        print("没有找到事件"); return 0

    # ---- 按 (bound, commit) 分组统计 ----
    print("=" * 78)
    print("按「驱动绑定 × 是否带 COMMIT」分组：")
    print("=" * 78)
    groups = {}
    for e in allev:
        key = (e["bound"], e["commit"])
        g = groups.setdefault(key, {"n": 0, "dead": 0, "lnk2": 0, "lnk1": 0})
        g["n"] += 1
        if e["dead"]:
            g["dead"] += 1
        if e["lnksta"] is not None:
            if e["lnksta"] & 0xF == 2:
                g["lnk2"] += 1
            else:
                g["lnk1"] += 1

    print(f"{'驱动绑定':<10}{'COMMIT':<9}{'次数':<6}{'掉卡':<6}{'掉卡率':<9}{'升Gen2':<8}{'仍Gen1':<8}")
    for (bound, commit), g in sorted(groups.items(), key=lambda kv: (kv[0][0] is None, kv[0][0])):
        bs = "未记录" if bound is None else ("是" if bound else "否")
        cs = "是★" if commit else "否"
        rate = f"{g['dead']*100//g['n']}%" if g["n"] else "-"
        print(f"{bs:<10}{cs:<9}{g['n']:<6}{g['dead']:<6}{rate:<9}{g['lnk2']:<8}{g['lnk1']:<8}")

    # ---- 明细 ----
    print()
    print("=" * 78)
    print("明细（掉卡事件优先）")
    print("=" * 78)
    for e in sorted(allev, key=lambda x: (not x["dead"], x["file"])):
        bs = "?" if e["bound"] is None else ("B" if e["bound"] else "U")
        cap2 = f"{e['cap2']:#06x}" if e["cap2"] is not None else "  ?  "
        sta = f"{e['lnksta']:#06x}" if e["lnksta"] is not None else "  ?  "
        print(f"  {'☠' if e['dead'] else '✓'} [{bs}] {'COMMIT' if e['commit'] else 'no-COM'} "
              f"SPEED={e['speed']} CAP2={cap2} →LNKSTA={sta}  "
              f"{e['file']}:{e['line']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
