#!/usr/bin/env python3
"""gen2-full.py —— 严格按 P104Gen2x4.cs 的 Linux 移植

依据：ref/P104-Gen2x4-v1.6/.../src/P104Gen2x4.cs（1564 行 C# 源码）

关键修正（相比此前所有尝试）：
  1. RMPcieLinkSpeed 必须 = 1（= 2 会把 TLS 钳死在 Gen1）★ 之前设成 2 是根因
  2. 还要 RmForceEnableGen2=1、RmEnableGen2=1
  3. PnP 复位必须让设备"真的离开总线"（remove + rescan），
     unbind/bind / enable 0/1 都做不到
  4. 判据是 LNKCAP2 & 0x6 == 0x6（声明锁存），不是 LNKSTA
  5. 复位后要重载驱动 + 重映射 BAR0，然后 6 轮"写使能→两端TLS→交替重训"

用法：
    sudo python3 gen2-full.py status    只看状态
    sudo python3 gen2-full.py once      执行一次完整流程
"""
import mmap, os, struct, subprocess, sys, time

PHYS_HINT = None          # 运行时从 config space 读 BAR0
DEV = "0000:01:00.0"
ROOT = "00:01.0"

# BAR0 偏移（与 C# 常量一致）
OFF_PRIV_MISC1 = 0x8841C    # 写使能：置 bit11/13/15，清 bit12/14
OFF_VSEC_HIER  = 0x88610    # 清 bit12，置 bit0
OFF_LTSSM      = 0x8872C    # 写 6
OFF_LINKCFG0   = 0x8C040    # SPEED[19:18]: 0=8GT 1=5GT 2=2.5GT；bit0=COMMIT
OFF_XP3G_OVR0  = 0x8E110
OFF_XP3G_OVR3  = 0x8E11C
OFF_XP3G_VAL0  = 0x8E120
OFF_XP3G_VAL3  = 0x8E12C
OFF_LNKSTA     = 0x88088    # 高16位=LNKSTA(含speed/width)，低16位=LNKCTL
OFF_LNKCAP2    = 0x880A4
OFF_CFG_SPACE  = 0x88000    # config space 镜像起点

SPEED_GEN2 = 1              # Pascal 编码：1 = 5.0GT/s

def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str),
                          capture_output=True, text=True, **kw)

def bar0_phys():
    """从 sysfs 读 BAR0 物理地址"""
    r = sh(f"setpci -s {DEV} 10.l")
    if r.returncode == 0 and r.stdout.strip():
        return int(r.stdout.strip(), 16) & 0xFFFFFFF0
    raise RuntimeError("读不到 BAR0 地址")

class Bar:
    def __init__(self):
        self.phys = bar0_phys()
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, 0x1000000, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE, offset=self.phys)
    def rd(self, o):  return struct.unpack_from("<I", self.m, o)[0]
    def wr(self, o, v): struct.pack_into("<I", self.m, o, v)
    def alive(self):  return self.rd(OFF_CFG_SPACE) not in (0xFFFFFFFF, 0)
    def close(self):
        try: self.m.close()
        finally: os.close(self.fd)

def state(b):
    s = b.rd(OFF_LNKSTA) >> 16
    return dict(cap2=b.rd(OFF_LNKCAP2), cfg=b.rd(OFF_PRIV_MISC1),
                link0=b.rd(OFF_LINKCFG0), sta=s,
                speed=s & 0xF, width=(s >> 4) & 0x3F)

def show(tag, b):
    s = state(b)
    sp = {1: "Gen1", 2: "Gen2", 3: "Gen3"}
    print(f"  [{tag}] LNKSTA={s['sta']:#06x} sp={s['speed']}({sp.get(s['speed'],'?')}) "
          f"x{s['width']}  CAP2={s['cap2']:#x}  cfg={s['cfg']:#010x}  link0={s['link0']:#010x}")
    return s

def write_enables(b):
    """C# WriteEnables()"""
    b.wr(OFF_XP3G_OVR0, 1)
    b.wr(OFF_XP3G_OVR3, 4)
    b.wr(OFF_XP3G_VAL0, 0)
    b.wr(OFF_XP3G_VAL3, 0x200000)
    c = b.rd(OFF_PRIV_MISC1)
    want = (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14))
    b.wr(OFF_PRIV_MISC1, want)
    got = b.rd(OFF_PRIV_MISC1)
    ok15 = (got >> 15) & 1
    if not ok15:
        print("    !! bit15 写入未落地")
    g = b.rd(OFF_VSEC_HIER)
    b.wr(OFF_VSEC_HIER, (g & ~(1 << 12)) | 1)
    b.wr(OFF_LTSSM, 6)
    return ok15

def write_speed_gen2(b):
    """C# WriteSpeedGen2()"""
    cur = b.rd(OFF_LINKCFG0)
    b.wr(OFF_LINKCFG0, (cur & ~0x000C0000) | (SPEED_GEN2 << 18) | 1)

def pci_cap():
    r = sh(f"setpci -s {DEV} 34.b")
    p = int(r.stdout.strip(), 16) if r.returncode == 0 else 0
    for _ in range(32):
        if p == 0: break
        r = sh(f"setpci -s {DEV} {p:x}.b")
        cap = int(r.stdout.strip(), 16)
        if cap == 0x10:
            return p
        r = sh(f"setpci -s {DEV} {p+1:x}.b")
        p = int(r.stdout.strip(), 16)
    return 0

def write_tls_both(cap):
    """C# WriteTlsBoth()：GPU + 根端口 都设 TLS=2"""
    if cap:
        r = sh(f"setpci -s {DEV} {cap+0x30:x}.w")
        cur = int(r.stdout.strip(), 16)
        if (cur & 0xF) != 2:
            sh(f"setpci -s {DEV} {cap+0x30:x}.w={((cur & ~0xF) | 2):04x}")
    r = sh(f"setpci -s {ROOT} CAP_EXP+0x30.w")
    if r.returncode == 0 and r.stdout.strip():
        cur = int(r.stdout.strip(), 16)
        if (cur & 0xF) != 2:
            sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((cur & ~0xF) | 2):04x}")

def retrain_both(cap):
    """C# RetrainBoth()：先根端口，再 GPU"""
    r = sh(f"setpci -s {ROOT} CAP_EXP+0x10.w")
    if r.returncode == 0 and r.stdout.strip():
        lc = int(r.stdout.strip(), 16)
        if lc != 0xFFFF:
            sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={(lc | 0x20):04x}")
    if cap:
        r = sh(f"setpci -s {DEV} {cap+0x10:x}.w")
        if r.returncode == 0 and r.stdout.strip():
            lc = int(r.stdout.strip(), 16)
            if lc != 0xFFFF:
                sh(f"setpci -s {DEV} {cap+0x10:x}.w={(lc | 0x20):04x}")

def pnp_reset():
    """让设备真的离开总线再回来（remove + rescan）"""
    print("\n=== PnP 复位：remove → rescan ===")
    pr = f"/sys/bus/pci/devices/{DEV}/remove"
    try:
        with open(pr, "w") as f: f.write("1")
    except Exception as e:
        print(f"  remove 失败: {e}"); return False
    gone = False
    for _ in range(30):
        time.sleep(0.2)
        # 用 config 空间能否读到 vendor:device 判断，不是 lspci 返回码
        r = sh(f"lspci -Dn | grep -c '10de:1b87'")
        if r.returncode != 0 or r.stdout.strip() in ("", "0"):
            gone = True; break
    print(f"  设备离开总线: {gone}")
    try:
        with open("/sys/bus/pci/rescan", "w") as f: f.write("1")
    except Exception as e:
        print(f"  rescan 失败: {e}"); return False
    back = False
    for i in range(60):
        time.sleep(0.5)
        r = sh(f"lspci -Dn | grep -i 10de")
        if "1b87" in r.stdout:
            print(f"  设备回到总线（{(i+1)*0.5:.1f}s）")
            back = True; break
    return back

def wait_latch(cap, want_secs=25):
    """等驱动绑定并读策略，把声明锁存成 Gen2"""
    print(f"\n=== 等声明锁存（最多 {want_secs}s，每 4s 检查）===")
    t0 = time.time()
    while time.time() - t0 < want_secs:
        time.sleep(4)
        r = sh(f"setpci -s {DEV} {cap+0x2c:x}.l")
        if r.returncode == 0 and r.stdout.strip():
            v = int(r.stdout.strip(), 16)
            el = time.time() - t0
            print(f"  [{el:.0f}s] LCAP2={v:#010x}  {'✓ 已锁存 Gen2' if (v & 0x6) == 0x6 else ''}")
            if (v & 0x6) == 0x6:
                return True
    return False

def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "status"
    if os.geteuid() != 0:
        print("需要 root"); return 1

    cap = pci_cap()
    if not cap:
        print("找不到 PCIe capability"); return 1

    b = Bar()
    print(f"BAR0 @ {b.phys:#x}   PCIe cap @ {cap:#x}")
    print("=== 起点 ===")
    show("before", b)

    if modo == "status":
        b.close(); return 0

    # 判据：LNKCAP2 & 0x6 == 0x6
    r = sh(f"setpci -s {DEV} {cap+0x2c:x}.l")
    l2 = int(r.stdout.strip(), 16)
    print(f"\nLNKCAP2 = {l2:#010x}  (需 & 0x6 == 0x6)")

    b.close()

    if (l2 & 0x6) != 0x6:
        print("\n=== 声明是 Gen1，执行复位 ===")
        b = Bar(); write_enables(b); write_speed_gen2(b); b.close()
        if not pnp_reset():
            print("!! 复位失败"); return 1
        time.sleep(1.5)
        # 重映射（BAR0 可能变了）
        try:
            b = Bar()
        except Exception as e:
            print(f"重映射失败: {e}"); return 1
        show("复位后", b)
        b.close()
        if not wait_latch(cap):
            print("\n!! 声明未锁存成 Gen2")

    # 逐轮：写使能 → 两端 TLS → 交替重训
    print("\n=== 逐轮尝试（最多 6 轮）===")
    for rnd in range(1, 7):
        b = Bar()
        write_enables(b)
        write_speed_gen2(b)
        b.close()
        write_tls_both(cap)
        retrain_both(cap)
        time.sleep(0.7)
        r = sh(f"setpci -s {DEV} {cap+0x12:x}.w")
        ls = int(r.stdout.strip(), 16)
        r = sh(f"setpci -s {DEV} {cap+0x2c:x}.l")
        l2 = int(r.stdout.strip(), 16)
        print(f"  第{rnd}轮: LNKSTA={ls:#06x} sp={ls & 0xF} x{(ls>>4)&0x3F}  CAP2={l2:#x}")
        if (ls & 0xF) >= 2:
            print("\n★★★★★ 成功：链路已上 Gen2 ★★★★★")
            # 切成功后把 0x8C040 的 SPEED 字段写回 Gen1 编码（2=2.5GT/s），
            # 但不清 COMMIT —— 让 RM 初始化时看到它期望的编码
            b = Bar()
            cur = b.rd(OFF_LINKCFG0)
            restored = (cur & ~0x000C0000) | (2 << 18)
            b.wr(OFF_LINKCFG0, restored)
            time.sleep(0.2)
            print(f"  恢复 0x8C040: {cur:#010x} → {b.rd(OFF_LINKCFG0):#010x} "
                  f"(SPEED[19:18]={(b.rd(OFF_LINKCFG0) >> 18) & 3}, 期望 2)")
            b.close()
            # 复查链路是否还在 Gen2
            time.sleep(1)
            r = sh(f"setpci -s {DEV} {cap+0x12:x}.w")
            ls2 = int(r.stdout.strip(), 16)
            print(f"  复查 LNKSTA={ls2:#06x} sp={ls2 & 0xF}"
                  + ("  （Gen2 保持）" if (ls2 & 0xF) >= 2 else "  !! 掉回 Gen1"))
            return 0
    print("\n6 轮结束，链路仍在 Gen1")
    return 1

if __name__ == "__main__":
    sys.exit(main())
