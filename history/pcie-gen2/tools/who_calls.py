#!/usr/bin/env python3
"""who_calls.py —— 反向查：谁调用了某个符号（按 .rela.text 重定位）

用途（reports/192）：确认 `0x8C040` 锁定环所在函数 `_nv017281rm`
是否被 ioctl 路径调用 —— 即"这个 SPEED 锁定环是用户态发起的吗"。

用法：python3 who_calls.py <nvidia.ko> <符号名>
"""
import struct
import sys


def parse_elf(data):
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x3E)[0]
    base = e_shoff + e_shstrndx * e_shentsize
    so = struct.unpack_from("<Q", data, base + 0x18)[0]
    ss = struct.unpack_from("<Q", data, base + 0x20)[0]
    strtab = data[so:so + ss]
    secs = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        ni = struct.unpack_from("<I", data, o)[0]
        t = struct.unpack_from("<I", data, o + 4)[0]
        a = struct.unpack_from("<Q", data, o + 0x10)[0]
        of = struct.unpack_from("<Q", data, o + 0x18)[0]
        sz = struct.unpack_from("<Q", data, o + 0x20)[0]
        lk = struct.unpack_from("<I", data, o + 0x28)[0]
        end = strtab.find(b"\0", ni)
        secs.append({"idx": i, "name": strtab[ni:end].decode("utf-8", "replace"),
                     "type": t, "addr": a, "off": of, "size": sz, "link": lk})
    return secs


def symtab(data, secs, idx):
    st = secs[idx]
    strsec = secs[st["link"]]
    strtab = data[strsec["off"]:strsec["off"] + strsec["size"]]
    out = []
    for i in range(st["size"] // 24):
        o = st["off"] + i * 24
        ni = struct.unpack_from("<I", data, o)[0]
        shndx = struct.unpack_from("<H", data, o + 6)[0]
        val = struct.unpack_from("<Q", data, o + 8)[0]
        end = strtab.find(b"\0", ni)
        out.append((strtab[ni:end].decode("utf-8", "replace"), val, shndx))
    return out


def main():
    path, want = sys.argv[1], sys.argv[2]
    with open(path, "rb") as f:
        data = f.read()
    secs = parse_elf(data)
    text = next(s for s in secs if s["name"] == ".text")

    # 全部符号，按地址建映射（用于把 caller 偏移换算成"哪个函数+偏移"）
    allsyms = []
    for s in secs:
        if s["type"] == 2:      # SHT_SYMTAB
            allsyms = symtab(data, secs, s["idx"])
            break
    by_addr = sorted([(v, n) for n, v, sh in allsyms
                      if sh == text["idx"] and n])

    def owner(off):
        prev = [(v, n) for v, n in by_addr if v <= off]
        if not prev:
            return "?", 0
        v, n = prev[-1]
        return n, off - v

    # 遍历所有 .rela.text*，找 symbol == want 的
    found = []
    for rs in secs:
        if not rs["name"].startswith(".rela."):
            continue
        syms = symtab(data, secs, rs["link"])
        for i in range(rs["size"] // 24):
            o = rs["off"] + i * 24
            r_off = struct.unpack_from("<Q", data, o)[0]
            r_info = struct.unpack_from("<Q", data, o + 8)[0]
            si = r_info >> 32
            add = struct.unpack_from("<q", data, o + 16)[0]
            if si >= len(syms):
                continue
            nm = syms[si][0]
            if nm != want:
                continue
            found.append((r_off, add, rs["name"]))

    print(f"引用 {want} 的重定位：{len(found)} 处\n")
    if not found:
        print("没有直接 call 引用 —— 可能是间接调用（函数指针表）")
        return 0

    from collections import Counter
    c = Counter()
    for r_off, add, sec in found:
        fn, d = owner(r_off)
        c[f"{fn}+{d:#x}"] += 1
    print("按调用者聚合：")
    for k, v in c.most_common(20):
        print(f"  {k}   ×{v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
