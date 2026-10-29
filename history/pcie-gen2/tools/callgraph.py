#!/usr/bin/env python3
"""callgraph.py —— NVIDIA RM 全量调用图（ET_REL，纯静态）

背景（reports/192-193）：
  已定位 SPEED 锁定环在 `_nv017281rm`（链路异常恢复函数）。
  调用链推测为 `_nv005948rm` -> `_nv017282rm+0x52` -> `_nv017281rm`，
  但 `who_calls.py` 在 `.rela.text` 里查 `_nv005948rm` 得到 **0 处**引用，
  且数据段里两处裸指针经核查分别落在 `.symtab` 和 `.rela.orc_unwind_ip`
  —— 都不是函数指针表。所以"它是怎么被调到的"仍未闭环。

  `who_calls.py` 的局限：只扫 `.rela.text*`。本脚本扩到**全部 SHT_RELA 节**，
  并用 sh_info 精确定位 r_offset 所属目标节（而不是猜 .text）。

子命令
  syms    <pat>                     列出符号（名/节/地址/大小/类型/绑定）
  callers <sym>                     直接调用者（遍历所有 .rela.*）
  graph   <sym> [--depth N]         反向 BFS：向上追调用者到 N 层
  path    <sym> [--roots a,b,c]     反向最短路：追到已知入口（默认一组内核入口）
  imms    <sym>                     代码里是否出现该符号地址的立即数/lea（间接调用线索）

用法
  python3 callgraph.py callers _nv005948rm
  python3 callgraph.py path    _nv017281rm --roots nv_pci_probe,nvidia_unlocked_ioctl
  python3 callgraph.py graph   _nv005948rm --depth 4

偏移纪律：本脚本全部用「节内偏移」，与 objdump --start-address 一致。
"""
import bisect
import struct
import sys
from collections import defaultdict, deque

SHT_SYMTAB, SHT_RELA = 2, 4
STT_FUNC, STT_OBJECT, STT_NOTYPE = 2, 1, 0
BIND = {0: "LOCAL", 1: "GLOBAL", 2: "WEAK"}
RTYPE = {0: "NONE", 1: "64", 2: "PC32", 4: "PLT32", 9: "GOTPCREL", 10: "32",
         11: "32S", 41: "GOTPCRELX", 42: "REX_GOTPCRELX"}


# ---------------------------------------------------------------- ELF
class Elf:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.d = f.read()
        d = self.d
        self.e_shoff = struct.unpack_from("<Q", d, 0x28)[0]
        self.e_shentsize = struct.unpack_from("<H", d, 0x3A)[0]
        self.e_shnum = struct.unpack_from("<H", d, 0x3C)[0]
        self.e_shstrndx = struct.unpack_from("<H", d, 0x3E)[0]
        b = self.e_shoff + self.e_shstrndx * self.e_shentsize
        so = struct.unpack_from("<Q", d, b + 0x18)[0]
        ss = struct.unpack_from("<Q", d, b + 0x20)[0]
        strtab = d[so:so + ss]
        self.secs = []
        for i in range(self.e_shnum):
            o = self.e_shoff + i * self.e_shentsize
            ni, typ = struct.unpack_from("<II", d, o)
            flags = struct.unpack_from("<Q", d, o + 8)[0]
            addr, off, sz = struct.unpack_from("<QQQ", d, o + 0x10)
            link, info = struct.unpack_from("<II", d, o + 0x28)
            end = strtab.find(b"\0", ni)
            self.secs.append({
                "idx": i, "name": strtab[ni:end].decode("utf-8", "replace"),
                "type": typ, "flags": flags, "addr": addr, "off": off,
                "size": sz, "link": link, "info": info})
        self.by_name = {s["name"]: s for s in self.secs}
        self.syms = self._symtab()

    def _symtab(self):
        st = next((s for s in self.secs if s["type"] == SHT_SYMTAB), None)
        if st is None:
            return []
        strsec = self.secs[st["link"]]
        strtab = self.d[strsec["off"]:strsec["off"] + strsec["size"]]
        out = []
        for i in range(st["size"] // 24):
            o = st["off"] + i * 24
            ni = struct.unpack_from("<I", self.d, o)[0]
            info = self.d[o + 4]
            shndx = struct.unpack_from("<H", self.d, o + 6)[0]
            val, size = struct.unpack_from("<QQ", self.d, o + 8)
            end = strtab.find(b"\0", ni)
            out.append({"i": i, "name": strtab[ni:end].decode("utf-8", "replace"),
                        "bind": info >> 4, "type": info & 0xF,
                        "shndx": shndx, "val": val, "size": size})
        return out

    def secidx_name(self, i):
        if 0 < i < len(self.secs):
            return self.secs[i]["name"]
        return {0: "UNDEF", 0xFFF1: "ABS", 0xFFF2: "COMMON"}.get(i, f"<{i}>")

    def owner_map(self, secname=".text"):
        """返回 (addrs, names, sizes) 供二分查"某偏移属于哪个函数"。"""
        sec = self.by_name.get(secname)
        if sec is None:
            return [], [], []
        lst = [(s["val"], s["name"], s["size"]) for s in self.syms
               if s["shndx"] == sec["idx"] and s["type"] == STT_FUNC and s["name"]]
        lst.sort()
        return [x[0] for x in lst], [x[1] for x in lst], [x[2] for x in lst]

    def relocs(self):
        """产出 (rela节名, 目标节名, r_offset, 类型, 符号名, addend, 符号节索引, 符号类型)。

        ★ 关键（2026-10-29 踩坑）：NVIDIA RM 里大量 call 的重定位符号是**节符号**
          （STT_SECTION，名字为空或 ".text"）+ addend 表示节内偏移。
          只按符号名建图会把它们全塌缩成一个假节点，导致 74% 的函数"零引用"。
          必须用 addend 反查 owner。
        """
        for rs in self.secs:
            if rs["type"] != SHT_RELA:
                continue
            syms = []
            if 0 < rs["link"] < len(self.secs) and self.secs[rs["link"]]["type"] == SHT_SYMTAB:
                st = self.secs[rs["link"]]
                strsec = self.secs[st["link"]]
                strtab = self.d[strsec["off"]:strsec["off"] + strsec["size"]]
                for i in range(st["size"] // 24):
                    o = st["off"] + i * 24
                    ni = struct.unpack_from("<I", self.d, o)[0]
                    shndx = struct.unpack_from("<H", self.d, o + 6)[0]
                    stype = self.d[o + 4] & 0xF
                    end = strtab.find(b"\0", ni)
                    syms.append((strtab[ni:end].decode("utf-8", "replace"), shndx, stype))
            tgt = self.secidx_name(rs["info"])
            for i in range(rs["size"] // 24):
                o = rs["off"] + i * 24
                r_off = struct.unpack_from("<Q", self.d, o)[0]
                r_info = struct.unpack_from("<Q", self.d, o + 8)[0]
                add = struct.unpack_from("<q", self.d, o + 16)[0]
                si, rt = r_info >> 32, r_info & 0xFFFFFFFF
                nm, shndx, stype = syms[si] if si < len(syms) else (f"<sym{si}>", 0, 0)
                yield rs["name"], tgt, r_off, rt, nm, add, shndx, stype


def is_section_sym(nm, stype):
    """是否是"节符号"重定位（用 addend 表示节内偏移）。"""
    return stype == 3 or nm == "" or nm == ".text"



# ---------------------------------------------------------------- helpers
class Graph:
    def __init__(self, elf, textsec=".text"):
        self.e = elf
        self.textsec = textsec
        self.addrs, self.names, self.sizes = elf.owner_map(textsec)
        # 符号名 -> (val,size)
        self.byname = {}
        for s in elf.syms:
            if s["name"] and s["shndx"] == elf.by_name[textsec]["idx"]:
                self.byname.setdefault(s["name"], s)
        self.callers = defaultdict(set)   # callee -> {(caller, off_in_caller, rela_sec)}
        self.calls = defaultdict(set)     # caller -> {callee}
        self._build()

    def owner(self, off, fuzzy=False):
        """off 属于哪个函数。fuzzy=True 时允许落在符号 size 之外（尾巴/合并）。"""
        if not self.addrs:
            return "?", 0
        k = bisect.bisect_right(self.addrs, off) - 1
        if k < 0:
            return "?", off
        v, n, sz = self.addrs[k], self.names[k], self.sizes[k]
        if not fuzzy and sz and off >= v + sz:
            return f"?after:{n}", off - v
        return n, off - v

    def _build(self):
        tgt_text = self.e.by_name[self.textsec]["idx"]
        self.secsym_calls = 0
        for rsec, tsec, r_off, rt, nm, add, shndx, stype in self.e.relocs():
            if not nm and not is_section_sym(nm, stype):
                continue
            # 只在"目标节是 .text"时把 r_off 当代码位置
            if tsec != self.textsec:
                continue
            if rt not in (2, 4, 42, 41):      # PC32 / PLT32 / GOTPCRELX 类 call/jmp
                continue
            caller, delta = self.owner(r_off)
            if is_section_sym(nm, stype):
                # 节符号：目标是 addend 处的函数
                if shndx != tgt_text:
                    continue
                callee, cdelta = self.owner(add)
                if callee.startswith("?"):
                    continue
                self.secsym_calls += 1
            else:
                callee = nm
            self.callers[callee].add((caller, delta, rsec))
            self.calls[caller].add(callee)

    # ---- 反向 BFS
    def bfs_up(self, start, max_depth=6, limit=400):
        seen = {start: (0, None, 0)}
        q = deque([start])
        order = [start]
        while q and len(order) < limit:
            cur = q.popleft()
            d, _, _ = seen[cur]
            if d >= max_depth:
                continue
            for caller, delta, rsec in sorted(self.callers.get(cur, ())):
                if caller in seen:
                    continue
                seen[caller] = (d + 1, cur, delta)
                order.append(caller)
                q.append(caller)
        return seen, order

    def path_to_roots(self, start, roots, max_depth=40):
        """反向 BFS 到任一根，返回最短路径。"""
        prev = {start: None}
        q = deque([(start, 0)])
        rootset = set(roots)
        while q:
            cur, d = q.popleft()
            if cur in rootset and cur != start:
                chain, c = [], cur
                while c is not None:
                    chain.append(c)
                    c = prev[c]
                return list(reversed(chain))
            if d >= max_depth:
                continue
            for caller, delta, rsec in sorted(self.callers.get(cur, ())):
                if caller in prev:
                    continue
                prev[caller] = cur
                q.append((caller, d + 1))
        return None


# ---------------------------------------------------------------- cmds
def cmd_syms(elf, pat, limit=40):
    hits = [s for s in elf.syms if pat in s["name"]]
    print(f"符号匹配 '{pat}'：{len(hits)} 个\n")
    print(f"{'name':32s} {'bind':7s} {'type':7s} {'section':22s} {'addr':>10s} {'size':>8s}")
    for s in hits[:limit]:
        t = {0: "NOTYPE", 1: "OBJECT", 2: "FUNC", 3: "SECTION", 4: "FILE"}.get(s["type"], str(s["type"]))
        print(f"{s['name']:32s} {BIND.get(s['bind'], s['bind']):7s} {t:7s} "
              f"{elf.secidx_name(s['shndx']):22s} {s['val']:#10x} {s['size']:#8x}")
    return 0


def cmd_callers(elf, g, name):
    rows = g.callers.get(name, set())
    print(f"直接引用 {name} 的重定位：{len(rows)} 处（全量 SHT_RELA 扫描）")
    if not rows:
        print("\n→ 无直接引用。可能是：间接调用（函数指针表/计算地址）、"
              "被裁剪、或该符号本就不是被 call 的入口。")
        print("  下一步：python3 callgraph.py imms " + name)
        return 0
    agg = defaultdict(int)
    for caller, delta, rsec in rows:
        agg[(caller, rsec)] += 1
    print(f"\n{'caller':44s} {'rela节':26s} n")
    for (caller, rsec), n in sorted(agg.items(), key=lambda x: -x[1])[:40]:
        print(f"{caller:44s} {rsec:26s} {n}")
    return 0


def cmd_refs(elf, g, name):
    """列出**任何位置**对 name 的重定位 —— 包括 .data / .rodata 里的函数指针表。

    ★ 这是 callers 的盲区：callers 只看"目标节是 .text"（= 直接 call/jmp），
      而函数指针表的 r_offset 落在 .data/.rodata，必须单独查。
    """
    rows = [(rsec, tsec, r_off, rt, add, nm)
            for rsec, tsec, r_off, rt, nm, add, shndx, stype in elf.relocs() if nm == name]
    print(f"对 {name} 的具名重定位（不限目标节）：{len(rows)} 处\n")
    if not rows:
        print("→ 全 ELF 无任何重定位引用它。")
        print("  含义：地址从未被静态取用 ⇒ 只能靠①计算偏移②外部模块（但 LOCAL 不可见）")
        print("        ⇒ 大概率是**死代码**（编译器/链接期保留但运行时不可达）。")
        return 0
    print(f"{'rela节':26s} {'目标节':22s} {'r_offset':>10s} {'类型':8s} addend")
    for rsec, tsec, r_off, rt, add, nm in rows:
        where = ""
        if tsec == ".text":
            fn, d = g.owner(r_off)
            where = f"  ← 位于 {fn}+{d:#x}"
        print(f"{rsec:26s} {tsec:22s} {r_off:#10x} {RTYPE.get(rt, rt):8} {add:#x}{where}")
    return 0


def cmd_graph(elf, g, name, depth=4, limit=200):
    seen, order = g.bfs_up(name, max_depth=depth, limit=limit)
    print(f"反向调用图（起点 {name}，深度 ≤{depth}，节点 {len(order)}）\n")
    # 树形打印
    kids = defaultdict(list)
    for n in order:
        d, parent, delta = seen[n]
        if parent:
            kids[parent].append((n, delta))

    def walk(n, ind=0, depth_left=depth):
        if depth_left < 0:
            return
        for c, delta in sorted(kids[n]):
            mark = "  ◀ 根?" if not g.callers.get(c) else ""
            print("  " * ind + f"← {c} (+{delta:#x} 调用点){mark}")
            walk(c, ind + 1, depth_left - 1)

    print(name)
    walk(name)
    print("\n无调用者的节点（可能是入口/间接调用目标）：")
    for n in order:
        if not g.callers.get(n):
            d, _, _ = seen[n]
            print(f"  depth{d}  {n}")
    return 0


def cmd_path(elf, g, name, roots):
    p = g.path_to_roots(name, roots)
    print(f"从 {name} 反向追到入口 {roots}\n")
    if not p:
        print("→ 未找到路径（在给定深度内）。说明它不在这棵调用树里，"
              "或入口不在 roots 集合中。")
        return 1
    for i, fn in enumerate(p):
        arrow = "" if i == 0 else "  ↑ 被调用 by"
        print(f"{fn}{arrow}")
    print(f"\n共 {len(p)-1} 跳")
    return 0


def cmd_imms(elf, g, name, span=8):
    """在 .text 里找"该符号地址"作为 32/64 位立即数出现的位置（lea/mov/表项）。"""
    s = g.byname.get(name)
    if s is None:
        print(f"找不到符号 {name}")
        return 1
    val = s["val"]
    text = elf.by_name[".text"]
    blob = elf.d[text["off"]:text["off"] + text["size"]]
    print(f"符号 {name} 节内地址 {val:#x}，在 .text 内搜立即数引用\n")
    hits = []
    for width, pack in ((8, "<Q"), (4, "<I")):
        pat = struct.pack(pack, val)
        start = 0
        while True:
            p = blob.find(pat, start)
            if p < 0:
                break
            hits.append((p, width))
            start = p + 1
    if not hits:
        print("→ .text 内 0 处立即数引用（地址未被 lea/mov 取用）")
        return 0
    print(f"命中 {len(hits)} 处：")
    for p, w in sorted(hits):
        fn, d = g.owner(p)
        print(f"  .text+{p:#x} (宽{w})  位于 {fn}+{d:#x}")
        # 打印上下文
        lo = max(0, p - 12)
        print(f"      上下文: {blob[lo:p+12].hex(' ')}")
    return 0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, cmd = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    opts, pos = {}, []
    i = 0
    while i < len(args):
        if args[i].startswith("--"):
            k = args[i][2:]
            v = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else "1"
            opts[k] = v
            i += 2
        else:
            pos.append(args[i])
            i += 1

    elf = Elf(path)
    g = Graph(elf)

    if cmd == "syms":
        return cmd_syms(elf, pos[0] if pos else "_nv0172")
    if cmd == "callers":
        return cmd_callers(elf, g, pos[0])
    if cmd == "refs":
        return cmd_refs(elf, g, pos[0])
    if cmd == "graph":
        return cmd_graph(elf, g, pos[0], int(opts.get("depth", 4)), int(opts.get("limit", 200)))
    if cmd == "path":
        roots = opts.get("roots", "nv_pci_probe,nvidia_unlocked_ioctl,nv_open,rm_ioctl").split(",")
        return cmd_path(elf, g, pos[0], roots)
    if cmd == "imms":
        return cmd_imms(elf, g, pos[0])
    print(f"未知子命令 {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
