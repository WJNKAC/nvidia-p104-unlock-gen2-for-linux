#!/usr/bin/env python3
"""find_lnkcfg0_v2.py —— 定位 0x8C040 的访问点，并按 section 反汇编窗口

改进点（v1 的教训）：
  · .ko 是 ET_REL（可重定位目标文件），**没有 PT_LOAD**，必须用 section header 映射
  · 主要模式不是 `89/8b disp32(%reg)`，而是 `b9 40c00800` = `mov $0x8c040,%ecx`
    → 说明驱动是**把寄存器偏移当函数参数传**（如 os_read_reg(offset)）
  · 因此真正要找的是"谁调用这个函数、传了 0x8c040"

输出：
  1. 每处命中的 section 名 + section 内偏移
  2. 该处前后 48 字节的十六进制
  3. 用 objdump 对提取出的 section 做窗口反汇编

用法：python3 find_lnkcfg0_v2.py <nvidia.ko> [--disasm N]
"""
import struct
import subprocess
import sys
import os
import tempfile

PATTERN = struct.pack("<I", 0x8C040)
BASEREG = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
           "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
REGS32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi",
          "r8d", "r9d", "r10d", "r11d", "r12d", "r13d", "r14d", "r15d"]

def parse_sections(data):
    """返回 [(name, sh_offset, sh_size, sh_addr)]"""
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return []
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x3E)[0]
    if e_shoff == 0 or e_shnum == 0:
        return []
    # 先拿 shstrtab
    base = e_shoff + e_shstrndx * e_shentsize
    str_off = struct.unpack_from("<Q", data, base + 0x18)[0]
    str_size = struct.unpack_from("<Q", data, base + 0x20)[0]
    strtab = data[str_off:str_off + str_size]
    out = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sh_name = struct.unpack_from("<I", data, off)[0]
        sh_type = struct.unpack_from("<I", data, off + 4)[0]
        sh_addr = struct.unpack_from("<Q", data, off + 0x10)[0]
        sh_offset = struct.unpack_from("<Q", data, off + 0x18)[0]
        sh_size = struct.unpack_from("<Q", data, off + 0x20)[0]
        if sh_type == 8:      # SHT_NOBITS
            continue
        name = strtab[sh_name:strtab.find(b"\0", sh_name)].decode("utf-8", "replace")
        out.append((name, sh_offset, sh_size, sh_addr))
    return out

def find_section(secs, foff):
    for name, off, size, addr in secs:
        if off <= foff < off + size:
            return name, foff - off, addr
    return None, None, None

def describe(prefix):
    """根据 pattern 前几个字节判断访问类型"""
    if len(prefix) < 2:
        return "?", None
    # mov $0x8c040, %ecx   = b9
    # mov $0x8c040, %esi   = be
    for op, reg in ((0xB9, "ecx"), (0xBE, "esi"), (0xBA, "edx"), (0xBF, "edi"),
                    (0xB8, "eax"), (0xBB, "ebx")):
        if prefix[-1] == op:
            return f"imm  mov ${'{'}0x8c040{'}'}, %{reg}   ← 偏移当参数传", "imm"
    op, modrm = prefix[-2], prefix[-1]
    mod = (modrm >> 6) & 3
    reg = (modrm >> 3) & 7
    rm = modrm & 7
    if mod == 2:
        base = BASEREG[rm]
        if op == 0x89:
            return f"★WRITE mov %{REGS32[reg]}, [0x8c040+{base}]", "w"
        if op == 0x8B:
            return f"read  mov %{REGS32[reg]}, [0x8c040+{base}]", "r"
        if op == 0xC7:
            return f"★WRITE movl $imm, [0x8c040+{base}]", "w"
        if op in (0x01, 0x09, 0x21, 0x29, 0x31):
            return f"★WRITE alu op={op:02x} [0x8c040+{base}]", "w"
        if op in (0x03, 0x0B, 0x23, 0x2B, 0x33):
            return f"read  alu op={op:02x} [0x8c040+{base}]", "r"
    return f"?     op={op:02x} modrm={modrm:02x}", None

def main():
    path = sys.argv[1]
    do_disasm = "--disasm" in sys.argv
    with open(path, "rb") as f:
        data = f.read()
    secs = parse_sections(data)
    print(f"文件 {len(data):,} 字节，section {len(secs)} 个")
    txt = [s for s in secs if s[0] == ".text"]
    if txt:
        print(f".text: offset={txt[0][1]:#x} size={txt[0][2]:,}")

    hits = []
    start = 0
    while True:
        i = data.find(PATTERN, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1

    results = []
    for foff in hits:
        name, soff, addr = find_section(secs, foff)
        prefix = data[max(0, foff - 4):foff]
        desc, kind = describe(prefix)
        results.append((foff, name, soff, desc, kind, prefix.hex()))

    # 只报 .text 里的
    intext = [r for r in results if r[1] == ".text"]
    print(f"\n0x8C040 模式命中 {len(hits)} 处，其中 .text 里 {len(intext)} 处\n")

    imm = [r for r in intext if r[4] == "imm"]
    wr = [r for r in intext if r[4] == "w"]
    rd = [r for r in intext if r[4] == "r"]
    other = [r for r in intext if r[4] is None]

    for title, lst in (("★ 直接写（displacement）", wr),
                       ("直接读", rd),
                       ("把偏移当立即数传（mov $0x8c040,%reg）", imm),
                       ("其他/未判定", other)):
        print(f"=== {title}：{len(lst)} 处 ===")
        for foff, name, soff, desc, kind, pfx in lst[:40]:
            print(f"  .text+{soff:#08x}  {desc}   (前缀 {pfx})")
        if len(lst) > 40:
            print(f"  …… 还有 {len(lst)-40} 处")
        print()

    if not do_disasm:
        return 0

    # ---- 反汇编窗口 ----
    if not txt:
        print("没有 .text，跳过反汇编"); return 0
    tname, toff, tsize, taddr = txt[0]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp.write(data[toff:toff + tsize]); tmp.close()

    # 选前 6 个 imm 命中做窗口反汇编（它们是"传参"点，最能定位函数）
    targets = imm[:6] if imm else intext[:6]
    for foff, name, soff, desc, kind, pfx in targets:
        lo = max(0, soff - 96)
        hi = min(tsize, soff + 96)
        print(f"\n{'='*70}")
        print(f"[.text+{soff:#x}] {desc}")
        # 该命中之前的字节（判断是否为一次调用）
        print(f"  前置 8 字节: {data[foff-8:foff].hex(' ')}  命中: {data[foff:foff+4].hex(' ')}")
        try:
            r = subprocess.run(
                ["objdump", "-D", "-b", "binary", "-m", "i386:x86-64",
                 "-M", "intel", "--start-address", str(lo), "--stop-address", str(hi),
                 tmp.name],
                capture_output=True, text=True, timeout=60)
            lines = [l for l in r.stdout.splitlines() if ":\t" in l]
            for l in lines:
                print("   " + l.strip())
        except Exception as e:
            print(f"   objdump 失败: {e}")
    os.unlink(tmp.name)
    return 0

if __name__ == "__main__":
    sys.exit(main())
