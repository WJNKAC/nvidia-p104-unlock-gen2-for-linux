#!/usr/bin/env python3
"""bar0-read.py —— 只读 dump P104 BAR0 的 PCIe 相关寄存器（/dev/mem 版）

本机实测：sysfs `resource0` 的 mmap 返回 EINVAL，改用 /dev/mem + 物理地址。

⚠ 前提：BAR0 已被 nvidia 驱动 request_mem_region（/proc/iomem 下有 nvidia 子项），
   `CONFIG_IO_STRICT_DEVMEM=y` 时 /dev/mem 访问会返回 EPERM。
   解决：内核参数 `iomem=relaxed`（重启生效），或先 unbind nvidia 驱动。

参考：github.com/skbskhud/p104-gen2-unlock  docs/04-硬件事实表.md
只读，不写任何寄存器。

用法：sudo python3 bar0-read.py [物理基址，默认 0xf6000000]
"""
import mmap, os, struct, sys

PHYS = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0xF6000000
SIZE = 0x1000000          # 16 MB（BAR0 = 0xf6000000-0xf6ffffff）

# 参考项目实测的 Gen1 态值（他的卡是 x4，我们是 x16，宽度字段会不同）
EXPECT = [
    (0x88000, "PCIID",            0x1B8710DE, "VID/DID"),
    (0x88004, "CMD_STA",          0x00100006, "Command/Status"),
    (0x88078, "EXP_HEAD",         0x00010010, "v1 模式（Gen1 态）"),
    (0x88084, "EXP_LNK_CAP",      0x00463C41, "SPEED[3:0]=1 只声明 Gen1"),
    (0x88088, "EXP_LNK_CMD_STA",  0x10410140, "LNKSTA=0x1041 (Gen1,x4)"),
    (0x880A4, "LNKCAP2",          0x00000002, "只支持 Gen1"),
    (0x880A8, "LNKCTL2",          0x00000000, "目标速率未设"),
    (0x8841C, "CONFIG",           0xC0B46500, "能力字段生成器输入（冷启动）"),
    (0x8872C, "LTSSM",            0x00000000, "写 6 = 触发"),
    (0x8C040, "LINK_CONFIG_0",    0x20585800, "SPEED[19:18]=2 → 2.5GT/s；bit0=COMMIT"),
    (0x8C080, "PPCI_2.WIDTH",     0x00000404, "宽度状态反射"),
    (0x8C1C0, "PL_LINK_RATE",     0x00110036, "bit2=PCIE_V2_SUPPORTED"),
    (0x2241C, "PUNITS.PCI",       0x00000000, "bit7=1 → Gen2（live）"),
    (0x2157C, "GEN23_DIS(熔丝)",  0x00000001, "只读熔丝"),
    (0x21580, "GEN3_DIS(熔丝)",   0x00000001, "只读熔丝"),
    (0x21394, "LANE_DIS(熔丝)",   0x00000000, "0 = 宽度未被熔丝关"),
]

def is_poison(v):
    return (v & 0xFFFF0000) == 0xBADF0000 or v in (0xFFFFFFFF, 0xDEADBEEF, 0xDEADDEAD)

def main():
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    except PermissionError:
        print("需要 root：sudo python3 bar0-read.py"); return 1

    try:
        m = mmap.mmap(fd, SIZE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)
    except OSError as e:
        print(f"mmap /dev/mem @{PHYS:#x} 失败: {e}")
        if e.errno == 1:      # EPERM
            print("  → BAR0 被 nvidia 独占（/proc/iomem 下有 nvidia 子项），")
            print("    IO_STRICT_DEVMEM 拒绝访问。需要 iomem=relaxed（重启），")
            print("    或先 unbind nvidia 驱动。")
        return 1

    print(f"/dev/mem @{PHYS:#x}   映射 {SIZE / 1048576:.0f} MB")

    def rd(o):
        return struct.unpack_from("<I", m, o)[0]

    print()
    print(f"{'偏移':<10} {'寄存器':<18} {'实测值':<12} {'参考(Gen1)':<12} 判定")
    print("─" * 78)
    same = diff = poison = 0
    for off, name, exp, note in EXPECT:
        try:
            v = rd(off)
        except Exception as e:
            print(f"{off:#08x}  {name:<18} 读取异常: {e}"); continue
        if is_poison(v):
            poison += 1
            mark = "毒值(不可访问)"
        elif v == exp:
            same += 1; mark = "✓ 一致"
        else:
            diff += 1; mark = f"≠ 差 {v ^ exp:#x}"
        print(f"{off:#08x}  {name:<18} {v:#010x}  {exp:#010x}    {mark}")
        if v != exp and not is_poison(v):
            print(f"{'':<10} └ {note}")

    print("─" * 78)
    print(f"一致 {same} / 不同 {diff} / 毒值 {poison}")

    # 关键位域解码
    print()
    print("=== 关键位域解码 ===")
    cap = rd(0x88084); sta = rd(0x88088)
    cap2 = rd(0x880A4); ctl2 = rd(0x880A8); cfg = rd(0x8C040); punits = rd(0x2241C)
    sp = {1: "2.5GT/s(Gen1)", 2: "5.0GT/s(Gen2)", 3: "8.0GT/s(Gen3)"}
    print(f"  LNK_CAP.SPEED[3:0]      = {cap & 0xF}  ({sp.get(cap & 0xF,'?')})")
    print(f"  LNK_CAP.WIDTH[9:4]      = x{(cap >> 4) & 0x3F}")
    print(f"  LNKSTA.SPEED[3:0]       = {(sta >> 16) & 0xF}  ({sp.get((sta>>16)&0xF,'?')})   ← 唯一采信的判据")
    print(f"  LNKSTA.WIDTH[9:4]       = x{((sta >> 16) >> 4) & 0x3F}")
    print(f"  LNKCAP2.SLSV[7:1]       = {bin((cap2 >> 1) & 0x7)}  (0b001=仅Gen1, 0b011=+Gen2, 0b111=+Gen3)")
    print(f"  LNKCTL2.SPEED[1:0]      = {ctl2 & 0x3}")
    print(f"  0x8C040.SPEED[19:18]    = {(cfg >> 18) & 0x3}  (2=2.5GT/s, 1=5.0GT/s, 0=8.0GT/s)")
    print(f"  0x8C040.COMMIT[0]       = {cfg & 1}")
    print(f"  0x2241C.PCIE_VERSION[0] = {punits & 1}   PCIE_SPEED[7] = {(punits >> 7) & 1}")
    print(f"  0x8872C                 = {rd(0x8872C):#x} (写6=触发)")
    m.close(); os.close(fd)
    return 0

if __name__ == "__main__":
    sys.exit(main())
