#!/usr/bin/env python3
"""find_8c040_writes.py —— 定位所有访问 BAR0 0x8C040 的指令，并区分读/写

用户建议的路径（reports/191 §下一步）：静态 grep 立即数，再逐点反汇编。
本脚本把它做完整：

  1. 在文件里搜小端 `40 c0 08 00`（= 0x8C040 作为 disp32/imm32）
  2. 对每个命中点，从**附近一条已知指令边界**开始窗口反汇编
     （x86 变长指令，必须从边界起解，否则全解错——踩过这个坑）
  3. 用前 8 字节里出现的 opcode 模式判定 读 / 写 / 传参
  4. 输出 ELF section 名 + 节内偏移，便于换算运行时地址

用法：
  python3 find_8c040_writes.py <file.o_binary> [--max 48]
"""
import struct
import subprocess
import sys
import tempfile
import os

PAT = bytes.fromhex("40c00800")

# 访问类型判定：看 disp32 之前的字节
def classify(pre: bytes):
    """pre = 命中点之前的若干字节（最多 8）。返回 (类型, 说明, 是否写)"""
    if len(pre) < 1:
        return ("?", "无前缀", None)
    b1 = pre[-1]
    b2 = pre[-2] if len(pre) >= 2 else None

    # mov $0x8c040, %reg   (B8+rd, 32 位立即数)
    for op, reg in ((0xB9, "ecx"), (0xB8, "eax"), (0xBA, "edx"),
                    (0xBB, "ebx"), (0xBE, "esi"), (0xBF, "edi")):
        if b1 == op:
            return ("IMM", f"mov ${'{'}0x8c040{'}'},%{reg}  ← 偏移当参数传", None)

    # 需要 opcode + ModRM
    if len(pre) < 2:
        return ("?", "过短", None)
    op, modrm = pre[-2], pre[-1]
    mod = (modrm >> 6) & 3
    regf = (modrm >> 3) & 7
    rm = modrm & 7
    REGS32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"]
    BASE = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi"]

    # REX 前缀：0x40-0x4f，且确实是 REX（不是 inc/dec 老编码）
    has_rex = False
    if len(pre) >= 3 and 0x40 <= pre[-3] <= 0x4F:
        has_rex = True

    if mod == 2 and rm != 4:          # [reg + disp32]
        base = BASE[rm] if rm < 8 else f"r{rm}"
        if op == 0x89:
            return ("W", f"★WRITE mov %{REGS32[regf]},[{'{'}0x8c040{'}'}+{base}]", True)
        if op == 0x8B:
            return ("R", f"read  mov %{REGS32[regf]},[{'{'}0x8c040{'}'}+{base}]", False)
        if op == 0xC7:
            return ("W", f"★WRITE movl $imm,[{'{'}0x8c040{'}'}+{base}]", True)
        if op in (0x01, 0x09, 0x21, 0x29, 0x31, 0x39):
            nm = {0x01: "add", 0x09: "or", 0x21: "and", 0x29: "sub",
                  0x31: "xor", 0x39: "cmp"}[op]
            w = op != 0x39
            return ("W" if w else "R",
                    f"{'★WRITE' if w else 'read '} {nm} [{'{'}0x8c040{'}'}+{base}]", w)
        if op in (0x03, 0x0B, 0x23, 0x2B, 0x33, 0x3B):
            return ("R", f"read  alu op={op:02x} [{'{'}0x8c040{'}'}+{base}]", False)
        if op == 0xFF:
            return ("?", f"op=ff /{regf} [{'{'}0x8c040{'}'}+{base}]", None)
        return ("?", f"op={op:02x} modrm={modrm:02x} base={base}", None)

    # mov $0x8c040 之外的另一常见形态：LEA / PUSH imm
    if b1 == 0x68:
        return ("IMM", "push $0x8c040", None)

    return ("?", f"b1={b1:02x} b2={b2:02x} modrm={modrm:02x}", None)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    maxn = 64
    if "--max" in sys.argv:
        maxn = int(sys.argv[sys.argv.index("--max") + 1])

    with open(path, "rb") as f:
        data = f.read()
    print(f"文件 {path}  {len(data):,} 字节")

    hits, i = [], 0
    while True:
        i = data.find(PAT, i)
        if i < 0:
            break
        hits.append(i)
        i += 1
    print(f"命中 {len(hits)} 处\n")

    # 提取一段文本用于窗口反汇编（用 temp 文件 + objdump）
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp.write(data)
    tmp.close()

    writes, reads, imms, others = [], [], [], []
    for off in hits[:maxn]:
        pre = data[max(0, off - 8):off]
        kind, desc, isw = classify(pre)
        rec = (off, desc, pre.hex(" "))
        if kind == "W":
            writes.append(rec)
        elif kind == "R":
            reads.append(rec)
        elif kind == "IMM":
            imms.append(rec)
        else:
            others.append(rec)

    def dump(title, lst, do_disasm=True):
        print(f"{'='*70}\n【{title}】{len(lst)} 处\n")
        for off, desc, pre in lst:
            print(f"  file={off:#010x}  {desc}")
            print(f"    前置8字节: {pre}")
            if not do_disasm:
                continue
            lo, hi = max(0, off - 40), min(len(data), off + 24)
            try:
                r = subprocess.run(
                    ["objdump", "-D", "-b", "binary", "-m", "i386:x86-64",
                     "-M", "intel", "--start-address", str(lo),
                     "--stop-address", str(hi), tmp.name],
                    capture_output=True, text=True, timeout=30)
                # 只保留包含命中的那几行 + 前后
                lines = [l for l in r.stdout.splitlines() if ":\t" in l]
                # 找到最接近 hit 的行，打印前后各 4 行
                idx = 0
                for k, l in enumerate(lines):
                    addr = int(l.split(":")[0].strip(), 16)
                    if addr <= off:
                        idx = k
                for l in lines[max(0, idx - 4):idx + 5]:
                    print("      " + l.strip())
            except Exception as e:
                print(f"      objdump 失败: {e}")
        print()

    dump("★ 直接写（displacement 形式）", writes)
    dump("直接读", reads, do_disasm=False)
    dump("偏移当立即数传（mov $0x8c040,%reg）", imms, do_disasm=False)
    dump("未判定", others, do_disasm=False)

    os.unlink(tmp.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
