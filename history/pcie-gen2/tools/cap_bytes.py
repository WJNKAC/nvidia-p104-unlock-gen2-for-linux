#!/usr/bin/env python3
"""cap_bytes.py —— 追查 RM 对象里"是否支持 Gen2/Gen3"能力位的来源

## 背景（reports/198）

`_nv017105rm` / `_nv017102rm` 是驱动自己的 `set_link_speed(mode)`：
    mode 1 → SPEED=2 (2.5GT/s, Gen1)
    mode 2 → SPEED=1 (5.0GT/s, Gen2)
    mode 3 → SPEED=0 (8.0GT/s, Gen3)
（`_nv017105rm` 还支持 mode 4）

调用前有**能力门禁**：mode≥2 时检查 `obj+0x954`(Gen2) / `+0x955`(Gen3) / `+0x956`(mode4)，
为 0 就直接 bail，什么都不写。

⇒ 如果这些字节是从"开机时的链路状态"学来的，就构成**自证预言**：
   开机 Gen1 → 能力位=0 → 驱动永不请求 Gen2 → 永远 Gen1。
   这也解释了为什么"在 nv_pci_probe 时机推 Gen2"有效（reports/184），
   而"驱动跑起来很久之后再改链路"无效（reports/195）。

本脚本在 .text 里找所有**写**这三个偏移的指令，定位设置能力位的函数。

## 做法

x86 里 `movb $imm8, 0x954(%reg)` 编码是：
    C6 8<reg> 54 09 00 00 <imm8>      （movb imm8, disp32(%reg)）
`mov %r8b, 0x954(%reg)`：
    88 8<reg> 54 09 00 00             （movb r8, disp32(%reg)）

所以搜 `54 09 00 00` / `55 09 00 00` / `56 09 00 00` 这 4 字节，
检查前面 2 字节是否是 88/C6 系列（mov 到内存），并区分读/写。

用法：
  python3 cap_bytes.py <nvidia.ko>
  python3 cap_bytes.py <nvidia.ko> --sym _nv017105rm   # 只看某个函数内
"""
import struct
import sys

sys.path.insert(0, "/data/labs/nvidia/pcie-gen2/tools")
from callgraph import Elf, Graph

TARGETS = {
    0x954: "cap_gen2（mode>=2 门禁）",
    0x955: "cap_gen3（mode>=3 门禁）",
    0x956: "cap_mode4（mode==4 门禁）",
}


def classify(blob, pos, disp_len=4):
    """pos 是 disp32 的起点。返回 (方向, 说明)。

    pos 前面依次是：ModRM, opcode, [REX]。ModRM 的 mod 决定这是
    [reg+disp32]（mod=10）还是别的形态。
    """
    if pos < 2:
        return "?", "太靠前"
    modrm = blob[pos - 1]
    op = blob[pos - 2]
    mod = (modrm >> 6) & 3
    regf = (modrm >> 3) & 7
    if mod != 2:
        return "?", f"mod={mod} 不是 [reg+disp32]"
    # mov 系列：88 /r = mov r8->rm8（写内存），8A /r = mov rm8->r8（读）
    if op == 0x88:
        return "W", f"★写 movb %r{regf}b, [disp]"
    if op == 0xC6:
        if pos + disp_len < len(blob):
            imm = blob[pos + disp_len]
            return "W", f"★写 movb ${imm:#x}, [disp]"
        return "W", "★写 movb $imm, [disp]"
    if op == 0x8A:
        return "R", f"读 movb [disp] -> %r{regf}b"
    # test/cmp 等
    if op in (0x80, 0xF6):
        return "R", f"比较 op={op:#x} /{regf}"
    if op in (0x84, 0x38, 0x3A):
        return "R", f"比较 op={op:#x}"
    if op == 0xFE:
        return "W", f"inc/dec op=fe /{regf}"
    return "?", f"op={op:#02x} modrm={modrm:#02x} /{regf}"


def main():
    path = sys.argv[1]
    only_sym = None
    if "--sym" in sys.argv:
        only_sym = sys.argv[sys.argv.index("--sym") + 1]

    e = Elf(path)
    g = Graph(e)
    t = e.by_name[".text"]
    blob = e.d[0:t["off"] + t["size"]]   # ★ 用文件偏移切片，file = text + 0x40
    toff = t["off"]

    rng = None
    if only_sym:
        s = g.byname.get(only_sym)
        if not s:
            print(f"找不到 {only_sym}"); return 2
        rng = (s["val"], s["val"] + s["size"])
        print(f"限定函数 {only_sym}  [{rng[0]:#x}, {rng[1]:#x})\n")

    for disp, desc in TARGETS.items():
        pat = struct.pack("<I", disp)
        hits = []
        start = 0
        while True:
            p = blob.find(pat, start)
            if p < 0:
                break
            start = p + 1
            text_off = p - toff            # 换算成 .text 内偏移
            if text_off < 0:
                continue
            if rng and not (rng[0] <= text_off < rng[1]):
                continue
            kind, why = classify(blob, p)
            fn, d = g.owner(text_off)
            hits.append((text_off, kind, why, fn, d))

        writes = [h for h in hits if h[1] == "W"]
        reads = [h for h in hits if h[1] == "R"]
        print(f"=== obj+{disp:#x}  {desc} ===")
        print(f"    命中 {len(hits)} 处：写 {len(writes)}，读 {len(reads)}")
        for off, kind, why, fn, d in hits:
            tag = "★WRITE" if kind == "W" else "      "
            print(f"  {tag} .text+{off:#09x}  {fn}+{d:#x}   {why}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
