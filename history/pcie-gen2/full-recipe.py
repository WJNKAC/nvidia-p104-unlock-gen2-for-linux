#!/usr/bin/env python3
"""full-recipe.py —— 严格按参考项目 §3.2「完整配方（五步缺一不可）」复现

来源：ref/p104-gen2-unlock/docs/01-技术报告.md §3.2

第1步 写寄存器（顺序不可乱：先配覆盖槽，最后才动 0x8C040）
    0x8E110=1  0x8E11C=4  0x8E120=0  0x8E12C=0x200000
    0x8841C：置 bit11/13，清 bit12/14
    0x88610：清 bit12，置 bit0
    0x8872C=6
    0x8C040：SPEED[19:18]=1 + COMMIT(bit0)
第2步 PnP 禁用→启用（unbind→bind）
第3步 等 ≥25 秒
第4步 两端设目标速率=2，交替触发重训
第5步 常驻按住（20ms）—— 由 --hold 模式启动

用法：
    sudo python3 full-recipe.py            # 只读当前状态
    sudo python3 full-recipe.py apply      # 跑 1~4 步
    sudo python3 full-recipe.py hold       # 进入常驻按住（前台，Ctrl-C 停）
    sudo python3 full-recipe.py revert     # 回滚第1步的写入
"""
import mmap, os, struct, subprocess, sys, time

PHYS, SIZE = 0xF6000000, 0x1000000
DEV = "0000:01:00.0"
ROOTPORT = "00:01.0"

R = dict(ov0=0x8E110, ov3=0x8E11C, val0=0x8E120, val3=0x8E12C,
         cfg=0x8841C, gate=0x88610, ltssm=0x8872C, link0=0x8C040,
         punits=0x2241C, pciid=0x88000, lnksta=0x88088, lnkcap2=0x880A4)

def mm():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, SIZE, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

class Mem:
    def __init__(self):
        self.fd, self.m = mm()
    def rd(self, o):   return struct.unpack_from("<I", self.m, o)[0]
    def wr(self, o, v): struct.pack_into("<I", self.m, o, v)
    def alive(self):   return self.rd(R["pciid"]) not in (0xFFFFFFFF, 0)
    def close(self):   self.m.close(); os.close(self.fd)

def dump(tag, m):
    sta = m.rd(R["lnksta"]) >> 16
    print(f"  [{tag}] LNKSTA={sta:#06x} sp={sta&0xF} width=x{(sta>>4)&0x3F}  "
          f"CAP2={m.rd(R['lnkcap2']):#x}  "
          f"cfg={m.rd(R['cfg']):#010x} link0={m.rd(R['link0']):#010x}")
    return sta & 0xF

def step1(m):
    """按顺序写 8 个寄存器；每写一个都检查设备是否还活着"""
    bk = {k: m.rd(o) for k, o in R.items() if k not in ("pciid", "lnksta", "lnkcap2")}
    print("\n=== 第1步：写寄存器（顺序：覆盖槽 → 配置门 → 触发 → 0x8C040）===")
    plan = [
        ("0x8E110 覆盖槽0", R["ov0"], 0x00000001),
        ("0x8E11C 覆盖槽3", R["ov3"], 0x00000004),
        ("0x8E120 值0",     R["val0"], 0x00000000),
        ("0x8E12C 值3",     R["val3"], 0x00200000),
    ]
    for name, off, val in plan:
        m.wr(off, val)
        rb = m.rd(off)
        ok = "✓" if rb == val else f"✗ 读回 {rb:#x}"
        print(f"  {name:<16} = {val:#010x}  {ok}")
        if not m.alive():
            print("  💥 设备掉出总线，中止"); return False, bk

    # 0x8841C：置 bit11/13，清 bit12/14
    c0 = m.rd(R["cfg"])
    c1 = (c0 | (1 << 11) | (1 << 13)) & ~(1 << 12) & ~(1 << 14)
    m.wr(R["cfg"], c1)
    print(f"  0x8841C cfg      = {c0:#010x} → {m.rd(R['cfg']):#010x}"
          f"  (置11/13 清12/14)")
    if not m.alive(): print("  💥 掉卡"); return False, bk

    # 0x88610：清 bit12，置 bit0
    g0 = m.rd(R["gate"])
    g1 = (g0 & ~(1 << 12)) | 1
    m.wr(R["gate"], g1)
    print(f"  0x88610 gate     = {g0:#010x} → {m.rd(R['gate']):#010x}  (清12 置0)")
    if not m.alive(): print("  💥 掉卡"); return False, bk

    # 0x8872C = 6
    m.wr(R["ltssm"], 6)
    print(f"  0x8872C ltssm    = {m.rd(R['ltssm']):#x}  (触发)")
    if not m.alive(): print("  💥 掉卡"); return False, bk

    # 0x8C040：SPEED[19:18]=1 + COMMIT —— 最后写
    l0 = m.rd(R["link0"])
    l1 = (l0 & ~0xC0000) | 0x40000 | 1
    print(f"  0x8C040 link0    = {l0:#010x} → 准备写 {l1:#010x}  (SPEED=1,COMMIT=1)")
    m.wr(R["link0"], l1)
    time.sleep(0.2)
    if not m.alive():
        print("  💥💥 写 0x8C040 后掉卡（与上次一致）"); return False, bk
    print(f"  0x8C040 读回     = {m.rd(R['link0']):#010x}")
    return True, bk

def step2(m, wait=25):
    """PnP 禁用→启用，然后等锁存"""
    print(f"\n=== 第2步：PnP 禁用 → 启用 ===")
    drv = "/sys/bus/pci/drivers/nvidia"
    try:
        with open(f"{drv}/unbind", "w") as f:
            f.write(DEV)
        print("  已 unbind")
        time.sleep(3)
        with open(f"{drv}/bind", "w") as f:
            f.write(DEV)
        print("  已 bind")
    except Exception as e:
        print(f"  失败: {e}"); return False
    print(f"\n=== 第3步：等 {wait} 秒（锁存需要 ≥25s）===")
    for s in range(wait):
        time.sleep(1)
        if (s + 1) % 5 == 0:
            print(f"    {s+1}s …")
    return True

def step4():
    """两端设目标速率 + 交替触发重训"""
    print("\n=== 第4步：两端设目标速率=Gen2 + 交替重训 ===")
    def sp(*args):
        r = subprocess.run(["setpci", "-s", ROOTPORT] + list(args),
                           capture_output=True, text=True)
        return (r.stdout or r.stderr).strip()
    # 根端口 LNKCTL2 (cap+0x30) [3:0] = 2
    sp("CAP_EXP+0x30.w=0002")
    print(f"  根端口 LNKCTL2 → {sp('CAP_EXP+0x30.w')}")
    # 交替重训：先根端口 retrain，再 GPU 侧 0x8872C
    for i in range(3):
        ctl = sp("CAP_EXP+0x10.w")
        try:
            cur = int(ctl, 16)
            sp(f"CAP_EXP+0x10.w={(cur | 0x20):04x}")
        except ValueError:
            pass
        time.sleep(0.3)
        try:
            fd, mm_ = mm()
            alive = struct.unpack_from("<I", mm_, R["pciid"])[0] != 0xFFFFFFFF
            sta = struct.unpack_from("<I", mm_, R["lnksta"])[0] >> 16
            mm_.close(); os.close(fd)
            print(f"    第{i+1}轮  设备{'在线' if alive else '★掉卡'}  LNKSTA={sta:#06x} sp={sta & 0xF}")
        except Exception as e:
            print(f"    第{i+1}轮后访问失败: {e}")

def do_hold():
    """第5步：常驻按住，每 20ms 重写关键寄存器"""
    print("=== 第5步：常驻按住（每 20ms）—— Ctrl-C 退出 ===")
    fd, m = mm()
    n = 0
    try:
        while True:
            l = struct.unpack_from("<I", m, R["link0"])[0]
            struct.pack_into("<I", m, R["link0"], (l & ~0xC0000) | 0x40000 | 1)
            struct.pack_into("<I", m, R["ltssm"], 6)
            n += 1
            if n % 250 == 0:
                sta = struct.unpack_from("<I", m, R["lnksta"])[0] >> 16
                print(f"  [{n}] LNKSTA={sta:#06x} sp={sta&0xF}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\n停止按住")
    finally:
        m.close(); os.close(fd)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if os.geteuid() != 0:
        print("需要 root"); return 1
    if mode == "hold":
        do_hold(); return 0

    m = Mem()
    if mode == "check":
        print("=== 当前状态 ==="); dump("now", m); m.close(); return 0

    if mode == "revert":
        print("回滚（写回 Gen1 声明）")
        m.wr(R["cfg"], 0xC0B46500)
        m.wr(R["punits"], 0x0)
        print(f"  cfg={m.rd(R['cfg']):#010x}"); m.close(); return 0

    # apply
    print("=== 起点 ==="); dump("before", m)
    ok, bk = step1(m)
    if not ok:
        print("\n第1步失败，回滚")
        for k, v in bk.items():
            try: m.wr(R[k], v)
            except Exception: pass
        m.close(); return 1
    m.close()

    if not step2(None): return 1
    step4()

    m = Mem()
    sp = dump("after", m)
    print()
    if sp == 2:
        print("★★★ 成功：链路已到 Gen2 ★★★")
        print("下一步：sudo python3 full-recipe.py hold   （常驻按住，否则会掉回 Gen1）")
    elif m.rd(R["lnkcap2"]) == 0x06:
        print("能力声明已解锁(0x06)，但链路仍 Gen1")
    else:
        print("未生效")
    m.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
