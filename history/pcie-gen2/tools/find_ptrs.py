#!/usr/bin/env python3
"""find_ptrs.py —— 查某个 .text 地址是否被数据段的函数指针表引用

用途（reports/192）：判断 `_nv005948rm`（0x8C040 锁定环的顶层调用者）
是挂在 ioctl dispatch 表（用户态可触发）还是 devinit/probe 表（仅加载时）。

原理：函数指针表在 .data/.rodata 里以 8 字节绝对地址存储，
伴随 `R_X86_64_64` 类型重定位。

用法：python3 find_ptrs.py <nvidia.ko> <符号名>
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

    # 找 want 的地址
    target = None
    for s in secs:
        if s["type"] == 2:
            for nm, val, sh in symtab(data, secs, s["idx"]):
                if nm == want and sh == text["idx"]:
                    target = val
                    break
            break
    if target is None:
        print(f"找不到符号 {want}")
        return 1
    print(f"{want} @ .text+{target:#x}\n")

    # 遍历所有 .rela.*，找 symbol==want 且类型为 64 位绝对地址重定位
    hits = []
    for rs in secs:
        if not rs["name"].startswith(".rela."):
            continue
        syms = symtab(data, secs, rs["link"])
        for i in range(rs["size"] // 24):
            o = rs["off"] + i * 24
            r_off = struct.unpack_from("<Q", data, o)[0]
            r_info = struct.unpack_from("<Q", data, o + 8)[0]
            si = r_info >> 32
            rtype = r_info & 0xFFFFFFFF
            if si >= len(syms):
                continue
            if syms[si][0] != want:
                continue
            hits.append((r_off, rtype, rs["name"]))

    print(f"对 {want} 的全部重定位引用：{len(hits)} 处")
    for r_off, rtype, sec in hits:
        kind = {1: "R_X86_64_64 (数据段函数指针)"}.get(rtype, f"type={rtype}")
        # 判断落在哪个节
        where = "?"
        for s in secs:
            if s["type"] != 8 and s["off"] <= r_off < s["off"] + s["size"]:
                where = s["name"]
                break
        print(f"  {sec}  r_offset={r_off:#x}  {kind}  落在 {where}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
