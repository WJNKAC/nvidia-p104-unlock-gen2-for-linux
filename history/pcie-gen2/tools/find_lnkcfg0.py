#!/usr/bin/env python3
"""find_lnkcfg0.py —— 在 nvidia.ko 里定位读写 0x8C040 的指令

思路：
  0x8C040 作为 disp32 的小端字节序列是  40 c0 08 00。
  用这个模式在文件里搜，然后看它前面 1–2 个字节（opcode + ModRM）判断是
  读还是写、基址寄存器是哪个。最后用 ELF program header 把文件偏移换算成
  虚拟地址（模块内偏移，配合 /sys/module/nvidia/sections/.text 可得到运行时地址）。

x86-64 常见形式：
  89 /r  mov r32, r/m32   → 写到内存
  8b /r  mov r/m32, r32   → 从内存读
  c7 /0  mov r/m32, imm32 → 写立即数
  ModRM mod=10 表示 disp32

用法：python3 find_lnkcfg0.py /lib/modules/.../nvidia.ko
"""
import struct
import sys

PATTERN = struct.pack("<I", 0x8C040)

BASEREG = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
           "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]


def elf_segments(data):
    """返回 [(file_off, vaddr, filesz)]"""
    if data[:4] != b"\x7fELF":
        return []
    is64 = data[4] == 2
    if not is64:
        return []
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    segs = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type = struct.unpack_from("<I", data, off)[0]
        if p_type != 1:          # PT_LOAD
            continue
        p_offset = struct.unpack_from("<Q", data, off + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        segs.append((p_offset, p_vaddr, p_filesz))
    return segs


def to_vaddr(segs, foff):
    for p_off, vaddr, fsz in segs:
        if p_off <= foff < p_off + fsz:
            return vaddr + (foff - p_off)
    return None


def classify(prefix):
    """prefix = pattern 之前的字节（最多 4 个）。返回 (描述, 是否写)"""
    if not prefix:
        return None, None
    # 立即数写：c7 8x <disp32> <imm32>  → 但 disp32 在 imm 之前，这里先看 c7 中 ModRM
    # 从紧邻的前 1 字节开始判断 ModRM/opcode 组合
    # 形态：<opcode> <modrm> 40 c0 08 00
    if len(prefix) >= 2:
        op = prefix[-2]
        modrm = prefix[-1]
        mod = (modrm >> 6) & 3
        reg = (modrm >> 3) & 7
        rm = modrm & 7
        if mod == 2 and rm == 4:
            # SIB 形式，后面应还有 SIB 字节——不在本模式里，跳过
            return f"疑似 SIB 形式 op={op:02x} modrm={modrm:02x}", None
        if mod == 2:
            base = BASEREG[rm + (8 if False else 0)]
            if op == 0x89:
                return f"★ WRITE mov {BASEREG[reg]}, [0x8c040+{base}]", True
            if op == 0x8B:
                return f"read  mov {BASEREG[reg]}, [0x8c040+{base}]", False
            if op == 0xC7:
                return f"★ WRITE movl $imm, [0x8c040+{base}]", True
            if op == 0x01:
                return f"★ WRITE add {BASEREG[reg]}, [0x8c040+{base}]", True
            if op == 0x09:
                return f"★ WRITE or  {BASEREG[reg]}, [0x8c040+{base}]", True
            if op in (0x21, 0x29, 0x31):
                return f"★ WRITE logic op={op:02x} [0x8c040+{base}]", True
            if op in (0x03, 0x0B, 0x23, 0x2B, 0x33):
                return f"read  alu op={op:02x} [0x8c040+{base}]", False
            if op == 0xFF:
                return f"?     op=ff /{reg} [0x8c040+{base}]", None
            return f"?     op={op:02x} modrm={modrm:02x} base={base}", None
    return f"数据/其他（前缀 {prefix.hex()}）", None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/lib/modules/$(uname -r)/updates/dkms/nvidia.ko"
    with open(path, "rb") as f:
        data = f.read()
    segs = elf_segments(data)
    print(f"文件大小 {len(data):,} 字节，PT_LOAD 段 {len(segs)} 个")

    hits = []
    start = 0
    while True:
        i = data.find(PATTERN, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1

    print(f"找到 0x8C040 的 disp32 模式 {len(hits)} 处\n")

    writes, reads, unknown = [], [], []
    for foff in hits:
        prefix = data[max(0, foff - 4):foff]
        desc, is_w = classify(prefix)
        va = to_vaddr(segs, foff)
        rec = (foff, va, desc, prefix.hex())
        if is_w is True:
            writes.append(rec)
        elif is_w is False:
            reads.append(rec)
        else:
            unknown.append(rec)

    def show(name, lst):
        print(f"=== {name}（{len(lst)} 处）===")
        for foff, va, desc, pfx in lst:
            vas = f"{va:#x}" if va is not None else "?"
            print(f"  file={foff:#010x} va={vas:>12}  {desc}   (前缀 {pfx})")
        print()

    show("写操作 ★", writes)
    show("读操作", reads)
    show("未判定", unknown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
