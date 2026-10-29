#!/usr/bin/env python3
"""find_reloc.py —— 解析 .ko 的重定位表，找出某个 .text 偏移处的 call 指向谁

背景（reports/187）：
  反汇编看到 `.text+0x126c5d` 处是 `call e8 00 00 00 00`（相对偏移 0），
  说明这是**未解析的 PLT32 重定位** —— 实际目标在 .rela.text 节里。
  只有解析重定位表才能知道它调用的是哪个函数。

用途：找到「BAR0 寄存器读/写」的封装函数符号，以便 kprobe 它
     （拦截 offset==0x8C040 且 SPEED 字段被改成 2 的写）。

用法：
  python3 find_reloc.py <nvidia.ko> <offset> [<offset2> ...]
     offset 用 .text 内偏移（例如 0x126c5d）或文件偏移都行，脚本会自己判断
"""
import struct
import sys


def parse_elf(data):
    """返回 sections 列表 与 符号表"""
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x3E)[0]

    base = e_shoff + e_shstrndx * e_shentsize
    str_off = struct.unpack_from("<Q", data, base + 0x18)[0]
    strtab = data[str_off:str_off + struct.unpack_from("<Q", data, base + 0x20)[0]]

    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name_i = struct.unpack_from("<I", data, off)[0]
        sh_type = struct.unpack_from("<I", data, off + 4)[0]
        sh_flags = struct.unpack_from("<Q", data, off + 8)[0]
        sh_addr = struct.unpack_from("<Q", data, off + 0x10)[0]
        sh_offset = struct.unpack_from("<Q", data, off + 0x18)[0]
        sh_size = struct.unpack_from("<Q", data, off + 0x20)[0]
        sh_link = struct.unpack_from("<I", data, off + 0x28)[0]
        sh_info = struct.unpack_from("<I", data, off + 0x2C)[0]
        sh_entsize = struct.unpack_from("<Q", data, off + 0x38)[0]
        end = strtab.find(b"\0", name_i)
        name = strtab[name_i:end].decode("utf-8", "replace")
        secs.append({
            "idx": i, "name": name, "type": sh_type, "flags": sh_flags,
            "addr": sh_addr, "off": sh_offset, "size": sh_size,
            "link": sh_link, "info": sh_info, "entsize": sh_entsize,
        })
    return secs


def get_symtab(data, secs, symtab_idx):
    """解析符号表，返回 [(name, value, shndx)]"""
    st = secs[symtab_idx]
    strsec = secs[st["link"]]
    strtab = data[strsec["off"]:strsec["off"] + strsec["size"]]
    syms = []
    n = st["size"] // 24
    for i in range(n):
        o = st["off"] + i * 24
        st_name = struct.unpack_from("<I", data, o)[0]
        st_info = data[o + 4]
        st_other = data[o + 5]
        st_shndx = struct.unpack_from("<H", data, o + 6)[0]
        st_value = struct.unpack_from("<Q", data, o + 8)[0]
        st_size = struct.unpack_from("<Q", data, o + 16)[0]
        end = strtab.find(b"\0", st_name)
        nm = strtab[st_name:end].decode("utf-8", "replace")
        syms.append({
            "name": nm, "info": st_info, "shndx": st_shndx,
            "value": st_value, "size": st_size, "other": st_other,
        })
    return syms


RELOC_TYPE = {
    1: "R_X86_64_64", 2: "R_X86_64_PC32", 4: "R_X86_64_PLT32",
    9: "R_X86_64_GOTPCREL", 10: "R_X86_64_32", 11: "R_X86_64_32S",
}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    path = sys.argv[1]
    targets = []
    for a in sys.argv[2:]:
        targets.append(int(a, 0))

    with open(path, "rb") as f:
        data = f.read()

    secs = parse_elf(data)
    text = next((s for s in secs if s["name"] == ".text"), None)
    if not text:
        print("找不到 .text")
        return 1
    print(f".text: off={text['off']:#x} size={text['size']:,} addr={text['addr']:#x}")

    # 把所有 target 统一换算成 .text 内偏移
    want = []
    for t in targets:
        if text["off"] <= t < text["off"] + text["size"]:
            want.append(("file", t, t - text["off"]))
        else:
            want.append((".text", text["off"] + t, t))
    for kind, foff, soff in want:
        print(f"  目标 {kind} 偏移 {foff:#x}  → .text+{soff:#x}")

    # 找到 .rela.text（以及可能的 .rela.text.unlikely 等）
    rela_secs = [s for s in secs if s["name"].startswith(".rela.text")]
    print(f"\n找到 {len(rela_secs)} 个 .rela.text* 节")

    hits = []
    for rs in rela_secs:
        symtab_idx = rs["link"]
        syms = get_symtab(data, secs, symtab_idx)
        n = rs["size"] // 24
        for i in range(n):
            o = rs["off"] + i * 24
            r_offset = struct.unpack_from("<Q", data, o)[0]
            r_info = struct.unpack_from("<Q", data, o + 8)[0]
            r_addend = struct.unpack_from("<q", data, o + 16)[0]
            sym_idx = r_info >> 32
            rtype = r_info & 0xFFFFFFFF

            for kind, foff, soff in want:
                # call rel32 的重定位通常落在 call 指令的 +1 处
                if r_offset in (soff, soff + 1, soff + 2):
                    sym = syms[sym_idx] if sym_idx < len(syms) else None
                    hits.append({
                        "sec": rs["name"], "r_offset": r_offset,
                        "soff": soff, "type": RELOC_TYPE.get(rtype, str(rtype)),
                        "sym": sym["name"] if sym else "?",
                        "sym_idx": sym_idx, "addend": r_addend,
                        "sym_shndx": sym["shndx"] if sym else None,
                        "sym_value": sym["value"] if sym else None,
                    })

    if not hits:
        print("\n未找到匹配重定位。")
        print("提示：反汇编里的 `call e8 00 00 00 00` 偏移是相对于【下一条指令】，")
        print("     重定位通常落在 call 指令地址本身或 +1；也试用文件偏移再跑一次。")
        return 1

    print(f"\n命中 {len(hits)} 条：")
    for h in hits:
        sec = next((s for s in secs if s["idx"] == h["sym_shndx"]), None)
        sname = sec["name"] if sec else f"shndx={h['sym_shndx']}"
        print(f"  {h['sec']}  r_offset={h['r_offset']:#x} (={'.text+'+hex(h['soff'])})")
        print(f"    type={h['type']}  addend={h['addend']}")
        print(f"    symbol[{h['sym_idx']}] = {h['sym']}   in {sname}"
              f"  value={h['sym_value']:#x}" if h["sym_value"] is not None else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
