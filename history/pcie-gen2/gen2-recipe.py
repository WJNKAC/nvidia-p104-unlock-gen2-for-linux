#!/usr/bin/env python3
"""gen2-recipe.py —— 严格按 release 包《研究过程记录.md》§8 最终配方四步

与 gen2-full.py 的关键差异（之前全错）：
  1. 声明解锁靠【第2步的禁用→启用】，不是 0x8872C 触发
  2. LNKCTL2 TLS=2 必须在【复位之后】设（复位会清）
  3. 根端口 TLS 起始值是 3(Gen3)，必须先设成 2
  4. 必须按住，否则驱动回压导致掉回 Gen1
  5. 判据：端点 LNKSTA=0x1042 且根端口 LNKSTA 低4位=2

用法：sudo python3 gen2-recipe.py
"""
import mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000

def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True)

def bar():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

def rd(m, o):      return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v):   struct.pack_into("<I", m, o, v)

def _rd_pci(sel, reg):
    r = sh(f"setpci -s {sel} {reg}")
    s = r.stdout.strip()
    if not s:
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None

def ep_lnksta():
    return _rd_pci(DEV, "CAP_EXP+0x12.w")

def rp_lnksta():
    return _rd_pci(ROOT, "CAP_EXP+0x12.w")

def rp_lnkctl2():
    return _rd_pci(ROOT, "CAP_EXP+0x30.w")

def alive():
    r = sh("lspci -Dn | grep -c '10de:1b87'")
    return r.stdout.strip() not in ("", "0")

def drv(tag):
    """驱动健康检查：绑定情况 + smi + 最新 NVRM"""
    bound = os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver")
    smi = sh("timeout 12 nvidia-smi --query-gpu=name,pcie.link.gen.current,"
             "pcie.link.width.current --format=csv,noheader 2>&1 | head -1")
    s = smi.stdout.strip().replace("\n", " ")
    nv = sh("dmesg | sed -n '/RECIPE-START-MARK/,$p' | grep -i NVRM | tail -2").stdout.strip()
    if len(nv) > 400:
        nv = nv[-400:]
    print(f"  [{tag}] bound={bound} | smi: {s or '(空)'}")
    if nv:
        for ln in nv.splitlines():
            print(f"  [{tag}] NVRM: {ln.strip()}")
    return bound and "NVIDIA" in s

def step1(m):
    """写使能位 + 速率请求"""
    print("\n=== 第1步：写使能位 + 速率请求 ===")
    for off, val, name in [
        (0x8E110, 1,          "XP3G_OVR0"),
        (0x8E11C, 4,          "XP3G_OVR3"),
        (0x8E120, 0,          "XP3G_VAL0"),
        (0x8E12C, 0x200000,   "XP3G_VAL3"),
    ]:
        wr(m, off, val)
        ok = "✓" if rd(m, off) == val else "✗"
        print(f"  {name:12s} = {val:#010x}  {ok}")

    c = rd(m, 0x8841C)
    want = (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14))
    wr(m, 0x8841C, want)
    print(f"  0x8841C      = {c:#010x} → {rd(m, 0x8841C):#010x}  (置11/13/15,清12/14)")

    g = rd(m, 0x88610)
    wr(m, 0x88610, (g & ~(1 << 12)) | 1)
    print(f"  0x88610      = {g:#010x} → {rd(m, 0x88610):#010x}  (清12,置0)")

    wr(m, 0x8872C, 6)
    print(f"  0x8872C      = {rd(m, 0x8872C):#x}")

    l = rd(m, 0x8C040)
    wr(m, 0x8C040, (l & ~0x000C0000) | (1 << 18) | 1)
    print(f"  0x8C040      = {l:#010x} → {rd(m, 0x8C040):#010x}  (SPEED=1 +COMMIT)")
    return alive()

def step2():
    """禁用 → 离开总线 → 启用 → 回来"""
    print("\n=== 第2步：禁用 → 启用（关键）===")
    # ★ 必须先 unbind 驱动再 remove，否则设备会卡在 D3cold（实测）
    drv = "/sys/bus/pci/drivers/nvidia"
    if os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver"):
        try:
            with open(f"{drv}/unbind", "w") as f: f.write(DEV)
            print("  已 unbind 驱动（避免 D3cold）")
            time.sleep(1)
        except Exception as e:
            print(f"  unbind 失败: {e}")

    rm = f"/sys/bus/pci/devices/{DEV}/remove"
    try:
        with open(rm, "w") as f: f.write("1")
    except Exception as e:
        print(f"  remove 失败: {e}"); return False
    gone = False
    for _ in range(40):
        time.sleep(0.2)
        if not alive():
            gone = True; break
    print(f"  设备离开总线: {gone}")
    if not gone:
        print("  !! 没能让设备离开总线"); return False

    try:
        open("/sys/bus/pci/rescan", "w").write("1")
    except Exception as e:
        print(f"  rescan 失败: {e}"); return False

    back = False
    for i in range(60):
        time.sleep(0.5)
        if alive():
            print(f"  设备回来（{(i+1)*0.5:.1f}s）")
            back = True; break
    if back:
        # 显式拉回 D0
        for cmd in (f"echo 1 > /sys/bus/pci/devices/{DEV}/enable",
                    f"echo on > /sys/bus/pci/devices/{DEV}/power/control"):
            sh(cmd)
        time.sleep(1.5)
        st = sh(f"cat /sys/bus/pci/devices/{DEV}/power_state").stdout.strip()
        print(f"  power_state = {st}")
    return back

def wait_latch(secs=25):
    """等声明锁存成 0x06"""
    print(f"\n=== 等声明锁存（最多 {secs}s）===")
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(4)
        r = sh(f"setpci -s {DEV} CAP_EXP+0x2c.l")
        v = int(r.stdout.strip(), 16) if r.stdout.strip() else 0
        print(f"  [{time.time()-t0:.0f}s] LCAP2={v:#010x}"
              + ("  ✓ 已锁存 Gen2" if (v & 0x6) == 0x6 else ""))
        if (v & 0x6) == 0x6:
            return True
    return False

def step3(m):
    """两端 TLS=2 + 交替重训（必须在复位之后）"""
    print("\n=== 第3步：两端 TLS=2 + 交替重训 ===")
    # 端点 LNKCTL2
    cur = rd(m, 0x880A8)
    wr(m, 0x880A8, (cur & ~0xF) | 2)
    print(f"  端点   LNKCTL2 = {cur:#06x} → {rd(m, 0x880A8):#06x}")
    # 根端口 LNKCTL2（起始 0x0043 → TLS=3，必须改成 2）
    rc = rp_lnkctl2()
    if rc is None:
        print("  根端口读不到，跳过")
    else:
        sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rc & ~0xF) | 2):04x}")
        nv = rp_lnkctl2()
        print(f"  根端口 LNKCTL2 = {rc:#06x} → {('N/A' if nv is None else format(nv,'#06x'))}")

    # 交替重训：先根端口，再端点
    for i in range(3):
        rl = sh(f"setpci -s {ROOT} CAP_EXP+0x10.w").stdout.strip()
        if rl:
            sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={((int(rl,16)) | 0x20):04x}")
        time.sleep(0.3)
        v = rd(m, 0x88088)
        wr(m, 0x88088, v | 0x20)
        time.sleep(0.5)
        ep, rp = ep_lnksta(), rp_lnksta()
        if ep is None:
            print(f"  第{i+1}轮: 端点读不到（设备可能掉了）"); return False
        rps = f"{rp:#06x}" if rp is not None else "N/A"
        print(f"  第{i+1}轮: 端点 LNKSTA={ep:#06x} sp={ep & 0xF} | 根端口 LNKSTA={rps}")
        if (ep & 0xF) == 2:
            return True
    return False

def step4_hold(secs=10):
    """常驻按住"""
    print(f"\n=== 第4步：按住 {secs} 秒（每 20ms）===")
    t0 = time.time(); n = 0
    while time.time() - t0 < secs:
        try:
            fd, m = bar()
            # 重写使能位 + 速率请求 + 两端 TLS
            c = rd(m, 0x8841C)
            wr(m, 0x8841C, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
            l = rd(m, 0x8C040)
            wr(m, 0x8C040, (l & ~0x000C0000) | (1 << 18) | 1)
            cur = rd(m, 0x880A8)
            if (cur & 0xF) != 2:
                wr(m, 0x880A8, (cur & ~0xF) | 2)
            m.close(); os.close(fd)
            rc = rp_lnkctl2()
            if rc is not None and (rc & 0xF) != 2:
                sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rc & ~0xF) | 2):04x}")
            n += 1
        except OSError:
            print("  !! MMIO 失效（可能掉卡）")
            return False
        if n % 50 == 0:
            ep, rp = ep_lnksta(), rp_lnksta()
            es = f"{ep & 0xF}" if ep is not None else "?"
            rs = f"{rp & 0xF}" if rp is not None else "?"
            print(f"  [{time.time()-t0:.1f}s n={n}] 端点 sp={es} | 根端口 sp={rs}")
        time.sleep(0.02)
    ep, rp = ep_lnksta(), rp_lnksta()
    if ep is None:
        print("  结束: 端点已不可读"); return False
    rps = f"{rp:#06x}" if rp is not None else "N/A"
    print(f"  结束: 端点 LNKSTA={ep:#06x} sp={ep & 0xF} | 根端口 LNKSTA={rps}")
    return (ep & 0xF) == 2

def main():
    if os.geteuid() != 0:
        print("需要 root"); return 1
    sh("echo 'RECIPE-START-MARK' > /dev/kmsg")
    print(f"设备在线: {alive()}")
    fd, m = bar()
    print(f"[起点] cfg={rd(m,0x8841C):#010x} link0={rd(m,0x8C040):#010x} "
          f"CAP2={rd(m,0x880A4):#x} LNKSTA={rd(m,0x88088)>>16:#06x}")
    print(f"  根端口 LNKCTL2={rp_lnkctl2():#06x} LNKSTA={rp_lnksta():#06x}")

    if not step1(m):
        print("\n第1步后设备消失"); m.close(); os.close(fd); return 1
    m.close(); os.close(fd)

    if not step2():
        print("\n第2步失败"); return 1

    print("\n=== 复位后驱动状态 ===")
    drv("复位后")

    fd, m = bar()
    print(f"[复位后] cfg={rd(m,0x8841C):#010x} CAP2={rd(m,0x880A4):#x} LNKSTA={rd(m,0x88088)>>16:#06x}")
    m.close(); os.close(fd)

    if not wait_latch():
        print("\n!! 声明未锁存")

    fd, m = bar()
    ok = step3(m)
    m.close(); os.close(fd)
    if not ok:
        print("\n第3步未升速，仍尝试按住")
    step4_hold()

    print("\n=== 绑定驱动验证 ===")
    sh("for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do lsmod | grep -q \"^$m \" && rmmod $m; done")
    time.sleep(2)
    sh("modprobe nvidia")
    time.sleep(5)
    drv("最终")
    return 0

if __name__ == "__main__":
    sys.exit(main())
