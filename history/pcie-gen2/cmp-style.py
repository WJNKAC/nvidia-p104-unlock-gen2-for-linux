#!/usr/bin/env python3
"""cmp-style.py —— 按 cmpunlocker 的完整序列解锁，适配 Pascal

来源（ref/cmpunlocker，789 stars，针对 CMP 170HX / GA100）：
  driver/patches/pcie-gen2.patch      —— 驱动内部完整序列
  driver/passthrough/cmp_no_bus_reset.c
  tools/hammer.sh                     —— 600 轮 × 0.05s 循环重训

★ 我们之前漏掉的三个东西（关键）：
  1. 0x8C2C0 bit2 = DIS_G2（Disable Gen2）—— 必须清零，否则 Gen2 被硬件禁用
  2. 0x8C1C0 = PL_LINK_RATE，写 0x00240036
  3. 0x880A8 (BAR0 侧 LNKCTL2 镜像)：除了 TLS=2，还要设 bits[19:16] = 0xF

★ Pascal vs Ampere 的 0x8C040.SPEED 编码相反：
     Ampere (CMP 170HX/GA100)：2 = Gen2
     Pascal (P104/GP104)      ：1 = Gen2   ← 我们用这个
   参考 ref/P104-Gen2x4-v1.6/docs/技术细节.md §1

★ 顺序（照 patch 原文）：
    LNKCTL2(0x880A8) → 清 DIS_G2(0x8C2C0) → SPEED(0x8C040)
    → PL_LINK_RATE(0x8C1C0) → XVE_OVR(0x8872C=6)
    → msleep(50) → 两端 TLS=2 → 根端口 Retrain → 轮询 LNKSTA

用法：
  sudo python3 cmp-style.py --dry-run     # 只读，打印所有相关寄存器
  sudo python3 cmp-style.py               # 执行
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000

# --- 寄存器 ---
R_LNKCAP   = 0x88084
R_LNKCAP2  = 0x880A4
R_LNKCTL2  = 0x880A8    # BAR0 侧 LNKCTL2 镜像
R_LNKSTA   = 0x88088
R_CONFIG   = 0x8841C    # PRIV_MISC_1
R_VSECDEV  = 0x8860C
R_VSEC     = 0x88610
R_LTSSM    = 0x8872C
R_SPEED    = 0x8C040    # LINK_CONFIG_0
R_PLRATE   = 0x8C1C0    # PL_LINK_RATE
R_CYA      = 0x8C2C0    # CYA_0, bit2 = DIS_G2
R_WIDTH    = 0x8C080

GEN2_SPEED = 1              # ★ Pascal 编码：1 = 5.0 GT/s
PLRATE_VAL = 0x00240036
GRACE = 20

def sh(c, t=120):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(c, 124, "", "TIMEOUT")

def rd(m, o): return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v): struct.pack_into("<I", m, o, v)

def bar():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

def alive(m):
    for _ in range(GRACE):
        if rd(m, R_LNKCAP2) != 0xFFFFFFFF:
            return True
        time.sleep(0.02)
    return False

def pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    try:
        return int(s, 16) if s else None
    except ValueError:
        return None

def dump(m, tag):
    print(f"\n=== [{tag}] 全部相关寄存器 ===")
    rows = [
        ("0x88084 LINK_CAP     ", R_LNKCAP),
        ("0x880A4 LINK_CAP2    ", R_LNKCAP2),
        ("0x880A8 LNKCTL2(镜像)", R_LNKCTL2),
        ("0x88088 LNKSTA(镜像) ", R_LNKSTA),
        ("0x8841C PRIV_MISC_1  ", R_CONFIG),
        ("0x8860C VSEC_DEVICE  ", R_VSECDEV),
        ("0x88610 VSEC_HIERARCHY", R_VSEC),
        ("0x8872C LTSSM/XVE_OVR", R_LTSSM),
        ("0x8C040 LINK_CONFIG_0", R_SPEED),
        ("0x8C080 PPCI_WIDTH   ", R_WIDTH),
        ("0x8C1C0 PL_LINK_RATE ", R_PLRATE),
        ("0x8C2C0 CYA_0        ", R_CYA),
    ]
    for name, off in rows:
        v = rd(m, off)
        extra = ""
        if off == R_SPEED:
            extra = f"  SPEED[19:18]={(v >> 18) & 3}"
        elif off == R_CYA:
            extra = f"  **DIS_G2(bit2)={(v >> 2) & 1}**"
        elif off == R_LNKCTL2:
            extra = f"  TLS[3:0]={v & 0xF}  bits[19:16]={(v >> 16) & 0xF}"
        elif off == R_LNKCAP2:
            extra = "  (支持速率向量)"
        print(f"  {name} = {v:#010x}{extra}")
    # 配置空间侧
    print(f"  --- 配置空间 ---")
    print(f"  端点   LNKCTL2  CAP_EXP+0x30.w = "
          f"{('N/A' if (v := pci(DEV,'CAP_EXP+0x30.w')) is None else format(v,'#06x'))}")
    print(f"  端点   LNKSTA   CAP_EXP+0x12.w = "
          f"{('N/A' if (v := pci(DEV,'CAP_EXP+0x12.w')) is None else format(v,'#06x'))}")
    print(f"  根端口 LNKCTL2  CAP_EXP+0x30.w = "
          f"{('N/A' if (v := pci(ROOT,'CAP_EXP+0x30.w')) is None else format(v,'#06x'))}")
    print(f"  根端口 LNKSTA   CAP_EXP+0x12.w = "
          f"{('N/A' if (v := pci(ROOT,'CAP_EXP+0x12.w')) is None else format(v,'#06x'))}")

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    dry = "--dry-run" in sys.argv
    loops = 600
    for a in sys.argv[1:]:
        if a.startswith("--loops="):
            loops = int(a.split("=", 1)[1])

    fd, m = bar()
    if not alive(m):
        print("MMIO 不应答 —— 先重启"); m.close(); os.close(fd); return 1

    dump(m, "起点")
    if dry:
        print("\n--dry-run：只看不改。")
        m.close(); os.close(fd); return 0

    if not os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver"):
        print("\n注意：驱动未绑定（早期启动场景）")

    # ---------- 第1步：解锁声明（安全，已多次验证）----------
    print("\n=== 第1步：解锁声明 + XP3G 使能 ===")
    for off, val, nm in [(0x8E110, 1, "XP3G_OVR0"), (0x8E11C, 4, "XP3G_OVR3"),
                         (0x8E120, 0, "XP3G_VAL0"), (0x8E12C, 0x200000, "XP3G_VAL3")]:
        wr(m, off, val)
        print(f"  {nm:10s}= {val:#010x} {'✓' if rd(m, off) == val else '✗'}")
    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    print(f"  0x8841C   = {c:#010x} → {rd(m, R_CONFIG):#010x}")
    g = rd(m, R_VSEC)
    wr(m, R_VSEC, (g & ~(1 << 12)) | 1)
    print(f"  0x88610   = {g:#010x} → {rd(m, R_VSEC):#010x}")
    # VSEC_DEVICE bit0（patch 里单独做了一次）
    v = rd(m, R_VSECDEV)
    wr(m, R_VSECDEV, v | 1)
    print(f"  0x8860C   = {v:#010x} → {rd(m, R_VSECDEV):#010x}  (置 bit0)")

    # ---------- 第2步：BAR0 侧 LNKCTL2 ----------
    print("\n=== 第2步：0x880A8 LNKCTL2 = TLS 2 + bits[19:16]=0xF ===")
    lc2 = rd(m, R_LNKCTL2)
    new = (lc2 & ~0x0000000F) | 2
    new = (new & ~0x000F0000) | 0x000F0000
    wr(m, R_LNKCTL2, new)
    print(f"  0x880A8   = {lc2:#010x} → {rd(m, R_LNKCTL2):#010x}"
          f"  TLS={rd(m, R_LNKCTL2) & 0xF} bits[19:16]={(rd(m,R_LNKCTL2)>>16)&0xF}")

    # ---------- 第3步：清 DIS_G2（★我们之前漏掉的）----------
    print("\n=== 第3步：清 0x8C2C0 bit2 (DIS_G2) ★关键 ===")
    cya = rd(m, R_CYA)
    before = (cya >> 2) & 1
    wr(m, R_CYA, cya & ~(1 << 2))
    after = (rd(m, R_CYA) >> 2) & 1
    print(f"  0x8C2C0   = {cya:#010x} → {rd(m, R_CYA):#010x}  DIS_G2 {before} → {after}")
    if after != 0:
        print("  !! DIS_G2 清不掉 —— 这解释了为什么 Gen2 无法真正工作")

    # ---------- 第4步：SPEED ----------
    print("\n=== 第4步：0x8C040 SPEED=1（Pascal 编码，无 COMMIT）===")
    sp = rd(m, R_SPEED)
    wr(m, R_SPEED, (sp & ~0x000C0000) | (GEN2_SPEED << 18))
    print(f"  0x8C040   = {sp:#010x} → {rd(m, R_SPEED):#010x}"
          f"  SPEED={(rd(m,R_SPEED)>>18)&3}")
    if not alive(m):
        print("  !! 写 SPEED 后端点已死"); m.close(); os.close(fd); return 2

    # ---------- 第5步：PL_LINK_RATE（★漏掉的）----------
    print("\n=== 第5步：0x8C1C0 PL_LINK_RATE = 0x00240036 ★关键 ===")
    pr = rd(m, R_PLRATE)
    wr(m, R_PLRATE, PLRATE_VAL)
    print(f"  0x8C1C0   = {pr:#010x} → {rd(m, R_PLRATE):#010x}")

    # ---------- 第6步：XVE_OVR ----------
    print("\n=== 第6步：0x8872C = 6 ===")
    wr(m, R_LTSSM, 6)
    print(f"  0x8872C   = {rd(m, R_LTSSM):#010x}")

    if not alive(m):
        print("  !! 寄存器序列后端点已死"); m.close(); os.close(fd); return 2
    print(f"  存活 ✓  LINK_CAP2 = {rd(m, R_LNKCAP2):#06x}")
    m.close(); os.close(fd)

    # ---------- 第7步：两端 TLS=2 + 循环重训（hammer.sh 的做法）----------
    print(f"\n=== 第7步：两端 TLS=2，然后循环重训（{loops} 轮 × 0.05s）===")
    for sel, nm in ((ROOT, "根端口"), (DEV, "端点")):
        cur = pci(sel, "CAP_EXP+0x30.w")
        if cur is not None:
            sh(f"setpci -s {sel} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")
            print(f"  {nm} TLS: {cur:#06x} → {pci(sel,'CAP_EXP+0x30.w'):#06x}")

    t0 = time.time()
    for i in range(1, loops + 1):
        # 按 hammer.sh：每轮都重写两端 TLS，然后根端口 Retrain
        sh(f"setpci -s {ROOT} CAP_EXP+0x30.w=0002:000f")
        sh(f"setpci -s {DEV}  CAP_EXP+0x30.w=0002:000f")
        sh(f"setpci -s {ROOT} CAP_EXP+0x10.w=0020:0020")
        st = pci(DEV, "CAP_EXP+0x12.w")
        if st is None:
            print(f"  [{i}] 端点读不到 —— 掉了"); return 3
        gen = st & 0xF
        if gen >= 2:
            print(f"  ★ 第 {i} 轮成功（{time.time()-t0:.1f}s）: "
                  f"端点 LNKSTA={st:#06x} sp={gen} | 根端口={pci(ROOT,'CAP_EXP+0x12.w')}")
            break
        if i % 100 == 0:
            print(f"  [{i}] 仍是 sp={gen}（{time.time()-t0:.1f}s）")
        time.sleep(0.05)
    else:
        st = pci(DEV, "CAP_EXP+0x12.w")
        print(f"  {loops} 轮后仍未成功，端点 sp={('N/A' if st is None else st & 0xF)}")
        return 1

    # ---------- 结果 ----------
    print("\n=== 结果 ===")
    print("  " + sh("lspci -vv -s 01:00.0 2>/dev/null | grep -E 'LnkSta:'").stdout.strip())
    fd, m = bar()
    dump(m, "达成后")
    m.close(); os.close(fd)
    print("\n  带宽（pinned/pageable 对照）：")
    print("  " + sh("timeout 150 /data/labs/nvidia/pcie-gen2/bwtest2 256 7 2>&1")
          .stdout.strip().replace("\n", "\n  "))
    st = pci(DEV, "CAP_EXP+0x12.w")
    print(f"\n  测完端点 sp={('N/A' if st is None else st & 0xF)}"
          f"  {'☠ 掉线（Xid 79 类问题）' if st is None else '✓ 存活'}")
    print("  NVRM: " + sh("dmesg | grep -i NVRM | tail -2").stdout.strip())
    return 0

if __name__ == "__main__":
    sys.exit(main())
