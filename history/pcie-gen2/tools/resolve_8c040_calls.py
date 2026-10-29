#!/usr/bin/env python3
"""resolve_8c040_calls.py —— 把 33 处 `mov $0x8c040,%ecx` 的 call 目标解析出来

思路：
  每处 `mov $0x8c040,%ecx` 之后，通常紧跟 `xor r8d,r8d / xor edx,edx / mov rdi,rXX /
  call rel32`。把 call 的 rel32 解析出来（文件是 ET_REL，call 目标要靠
  .rela.text 重定位；若没有重定位则说明是节内直接调用，可直接算）。

输出：按"被调用目标"聚合，统计每个目标被多少处使用 ⇒ 找出主入口。

用法：python3 resolve_8c040_calls.py <nv-kernel.o_binary>
"""
import struct
import sys

PAT = bytes.fromhex("40c00800")


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


def sec_of(secs, foff):
    for s in secs:
        if s["type"] != 8 and s["off"] <= foff < s["off"] + s["size"]:
            return s
    return None


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
    path = sys.argv[1]
    with open(path, "rb") as f:
        data = f.read()
    secs = parse_elf(data)
    text = next((s for s in secs if s["name"] == ".text"), None)
    print(f"文件 {path}  {len(data):,}")
    if text:
        print(f".text off={text['off']:#x} size={text['size']:,}\n")

    # 建立重定位索引: r_offset -> (symname, secidx)
    relocs = {}
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
            nm, val, shndx = syms[si] if si < len(syms) else ("?", 0, 0)
            relocs[r_off] = (nm, val, shndx, add, rs["name"])
    print(f"重定位条目 {len(relocs):,}\n")

    hits, i = [], 0
    while True:
        i = data.find(PAT, i)
        if i < 0:
            break
        hits.append(i)
        i += 1

    # 每处命中：往后 40 字节内找 E8 (call rel32)
    results = []
    for off in hits:
        sec = sec_of(secs, off)
        if not sec or sec["name"] != ".text":
            continue
        base = sec["off"]
        found = None
        for d in range(5, 41):
            p = off + d
            if p + 5 > len(data):
                break
            if data[p] == 0xE8:
                rel = struct.unpack_from("<i", data, p + 1)[0]
                next_ip = (p - base) + 5
                tgt_off = next_ip + rel
                r = relocs.get(p - base + 1) or relocs.get(p - base)
                found = {
                    "call_file": p,
                    "call_text": p - base,
                    "target_text": tgt_off,
                    "reloc": r,
                }
                break
        results.append((off, off - base, found))

    # 按目标聚合
    agg = {}
    for off, toff, f in results:
        if not f:
            agg.setdefault("(未找到 call)", []).append(toff)
            continue
        if f["reloc"]:
            key = f"reloc:{f['reloc'][0]}"
        else:
            key = f"text+{f['target_text']:#x}"
        agg.setdefault(key, []).append(toff)

    print("=" * 70)
    print("按 call 目标聚合（33 处 mov ecx,0x8c040 的调用去向）")
    print("=" * 70)
    for k, v in sorted(agg.items(), key=lambda kv: -len(kv[1])):
        print(f"\n{k}   ×{len(v)}")
        print(f"   来自 .text+ : {', '.join(hex(x) for x in v[:10])}"
              + (" ..." if len(v) > 10 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
