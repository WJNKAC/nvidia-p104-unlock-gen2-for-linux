#!/usr/bin/env python3
"""gen2-v2.py —— PCIe Gen2 解锁 V2：带安全网 + 实验矩阵

背景（必读 reports/175）：
  上一版 gen2-recipe.py 在第 1 步写 0x8C040(COMMIT) 之后，端点 BAR0 立刻变 0xffffffff，
  随后 remove + rescan 再也拉不回来，只能断电重启。
  参考项目自身也记录过同一事故（docs/03-走过的弯路.md:126）。

本版改动：
  1. mmio_alive() —— 每次高危写之后回读 LINK_CAP2(0x880A4) 这个稳定常量寄存器，
     读到 0xffffffff 立刻中止，绝不进第 2 步（旧版只查 lspci，会漏判）
  2. 0x8C040 拆成独立开关，五种模式可 A/B：
       --mode=nocommit   完全跳过 0x8C040（A 组）
       --mode=speedonly  第 1 步就写 SPEED[19:18]=1，不发 COMMIT（C 组）
       --mode=latespeed  ★默认★ 先解锁声明 + remove/rescan 重枚举，再写 SPEED（E 组）
       --mode=latecommit 同上但写的是 COMMIT（D 组，有毒，禁用）
       --mode=full       旧配方原样（★必掉卡）
  3. --no-unbind —— 第 2 步 remove 前不 unbind（B 组，对齐上次成功的路径）
  4. 每步前后 dump 状态到 state-<ts>.json，掉卡后还能事后复盘
  5. --no-bw 关闭改前/改后的 libcuda 带宽对照（bwtest）

实测结论（reports/176、177）：
  COMMIT 位是纯毒药；Gen2 只需要写 SPEED=1；但 SPEED 必须在"声明解锁 + 重枚举"之后写。

用法：
  sudo python3 gen2-v2.py                       # 默认 latespeed
  sudo python3 gen2-v2.py --mode=nocommit
  sudo python3 gen2-v2.py --mode=speedonly --hold=60
"""
import json, mmap, os, struct, subprocess, sys, time

DEV = "0000:01:00.0"
ROOT = "00:01.0"
PHYS = 0xF6000000
DEV_ID = "10de:1b87"
GRACE = 20          # 连续多少次 0xffffffff 才判定 MMIO 真的失效（参考源码用 20）

# ---- 关键寄存器 ----------------------------------------------------------
R_CAP2      = 0x880A4   # LINK_CAP2，稳定常量 0x0002/0x0006 —— 当"活着"的探针
R_LNKSTA    = 0x88088   # 低 16 位是 LNKSTA(端点)
R_CONFIG    = 0x8841C   # CONFIG，bit15/13/11 是 Gen2 使能位
R_VSEC      = 0x88610   # VSEC_HIERARCHY
R_LTSSM     = 0x8872C   # LTSSM / XVE_OVR，写 6 = 触发
R_LNKCFG0   = 0x8C040   # LINK_CONFIG_0，SPEED[19:18] + bit0 COMMIT  ★高危★
R_LNKCTL2   = 0x880A8   # 端点 LNKCTL2，低 4 位 TLS

ARGS = {"mode": "latespeed", "unbind": True, "hold": 60, "bw": True,
        "reload": False, "skip_reset": False, "no_retrain": False, "unbind_first": False}
BWBIN = "/data/labs/nvidia/pcie-gen2/bwtest"

def bw(tag):
    """真带宽：dlopen libcuda 的 H2D/D2H"""
    if not ARGS["bw"]:
        return
    if not os.path.exists(BWBIN):
        print(f"  [{tag}] 带宽测试未编译，跳过"); return
    r = sh(f"timeout 120 {BWBIN} 256 10 2>&1")
    lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    print(f"  [{tag}] 带宽: " + " | ".join(lines[1:]) if len(lines) > 1 else f"  [{tag}] {r.stdout.strip()}")

def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True)

def bar():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return fd, mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE, offset=PHYS)

def rd(m, o):    return struct.unpack_from("<I", m, o)[0]
def wr(m, o, v): struct.pack_into("<I", m, o, v)

def _rd_pci(sel, reg):
    s = sh(f"setpci -s {sel} {reg}").stdout.strip()
    if not s:
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None

def ep_lnksta():  return _rd_pci(DEV, "CAP_EXP+0x12.w")
def rp_lnksta():  return _rd_pci(ROOT, "CAP_EXP+0x12.w")
def rp_lnkctl2(): return _rd_pci(ROOT, "CAP_EXP+0x30.w")
def ep_cap2_pci(): return _rd_pci(DEV, "CAP_EXP+0x2c.l")

def on_bus():
    return sh(f"lspci -Dn | grep -c '{DEV_ID}'").stdout.strip() not in ("", "0")

def mmio_alive(m):
    """★ 安全网：回读稳定常量寄存器。
    参考源码教训（P104Gen2x4.cs:1293）：
      **连续 20 次** 0xFFFFFFFF 才算 MMIO 失效，单次多半是重训瞬态。
    所以这里允许 grace 次重试。
    """
    for i in range(GRACE):
        v = rd(m, R_CAP2)
        if v != 0xFFFFFFFF:
            return True, v
        time.sleep(0.02)
    return False, 0xFFFFFFFF

def panic(m, where):
    """MMIO 死了：立刻关闭映射并退出，绝不进入后续步骤"""
    v = rd(m, R_CAP2)
    print(f"\n!!! 安全网触发（{where}）：LINK_CAP2 回读 = {v:#010x} —— 端点 MMIO 已死")
    print("!!! 立即中止。此时 lspci 可能仍能看到设备，那是假象。")
    try:
        m.close()
    except Exception:
        pass
    sys.exit(2)

def state(tag, m=None):
    d = {"tag": tag, "t": time.strftime("%F %T")}
    d["on_bus"] = on_bus()
    d["power"] = sh(f"cat /sys/bus/pci/devices/{DEV}/power_state 2>/dev/null").stdout.strip()
    d["bound"] = os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver")
    if m is not None:
        for name, off in (("cap2", R_CAP2), ("config", R_CONFIG), ("vsec", R_VSEC),
                          ("lnkcfg0", R_LNKCFG0), ("lnkctl2", R_LNKCTL2)):
            d[name] = f"{rd(m, off):#010x}"
        d["lnksta"] = f"{rd(m, R_LNKSTA) >> 16:#06x}"
    for name, fn in (("ep_lnksta", ep_lnksta), ("rp_lnksta", rp_lnksta),
                     ("rp_lnkctl2", rp_lnkctl2)):
        v = fn()
        d[name] = None if v is None else f"{v:#06x}"
    try:
        with open(STATEF, "a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return d

def show(d):
    print(f"  [{d['tag']}] on_bus={d['on_bus']} power={d['power']} bound={d['bound']}")
    print(f"    CAP2={d.get('cap2','-')} LNKSTA={d.get('lnksta','-')} "
          f"cfg={d.get('config','-')} lnkcfg0={d.get('lnkcfg0','-')}")
    print(f"    端点 LNKSTA={d.get('ep_lnksta')} | 根端口 LNKSTA={d.get('rp_lnksta')} "
          f"TLS2={d.get('rp_lnkctl2')}")

def drv(tag):
    bound = os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver")
    # ★ 参考源码警告（技术细节.md §5）：nvidia-smi 的 pcie.link.gen.current
    #   读的是驱动侧缓存/省电态，实测寄存器已 Gen2 时它仍可能报 1。
    #   所以这里只当参考，唯一判据是 LNKSTA 寄存器的速率字段。
    s = sh("timeout 12 nvidia-smi --query-gpu=name,pcie.link.gen.current,"
           "pcie.link.width.current --format=csv,noheader 2>&1 | head -1").stdout.strip()
    nv = sh("dmesg | sed -n '/RECIPE-START-MARK/,$p' | grep -i NVRM | tail -2").stdout.strip()
    print(f"  [{tag}] bound={bound} | smi: {s or '(空)'}  ← smi 的 gen 不可当判据")
    if nv:
        for ln in nv.splitlines():
            print(f"  [{tag}] NVRM: {ln.strip()}")
    return bound and "NVIDIA" in s

def bind(do_bind=True):
    d = f"/sys/bus/pci/devices/{DEV}"
    f = "/sys/bus/pci/drivers/nvidia/" + ("bind" if do_bind else "unbind")
    try:
        with open(f, "w") as fh:
            fh.write(DEV)
        return True
    except Exception as e:
        print(f"  {'bind' if do_bind else 'unbind'} 失败: {e}")
        return False

def bound():
    return os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver")

# ---- 第 1 步 -------------------------------------------------------------
def step1(m):
    print(f"\n=== 第1步：写使能位 + 速率请求（mode={ARGS['mode']}）===")
    for off, val, name in [
        (0x8E110, 1,        "XP3G_OVR0"),
        (0x8E11C, 4,        "XP3G_OVR3"),
        (0x8E120, 0,        "XP3G_VAL0"),
        (0x8E12C, 0x200000, "XP3G_VAL3"),
    ]:
        wr(m, off, val)
        ok = "✓" if rd(m, off) == val else "✗"
        print(f"  {name:12s} = {val:#010x}  {ok}")

    c = rd(m, R_CONFIG)
    wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
    print(f"  0x8841C      = {c:#010x} → {rd(m, R_CONFIG):#010x}  (置11/13/15,清12/14)")

    g = rd(m, R_VSEC)
    wr(m, R_VSEC, (g & ~(1 << 12)) | 1)
    print(f"  0x88610      = {g:#010x} → {rd(m, R_VSEC):#010x}  (清12,置0)")

    wr(m, R_LTSSM, 6)
    print(f"  0x8872C      = {rd(m, R_LTSSM):#x}")

    ok, v = mmio_alive(m)
    if not ok:
        panic(m, "0x8872C 之后")
    print(f"  安全网：LINK_CAP2 = {v:#06x} ✓ MMIO 正常")

    # ---- 0x8C040 分支 ----
    if ARGS["mode"] == "nocommit" or ARGS["mode"] == "latespeed":
        print(f"  0x8C040      —— 跳过（{ARGS['mode']}：推迟到重枚举之后再写 SPEED）")
    elif ARGS["mode"] == "latecommit":
        print("  0x8C040      —— 推迟到声明锁存之后再写（D 组）")
    elif ARGS["mode"] == "speedonly":
        if not speed_now(m, "第1步"):
            panic(m, "写 SPEED 之后")
    else:
        l = rd(m, R_LNKCFG0)
        new = (l & ~0x000C0000) | (1 << 18) | 1      # 旧配方
        wr(m, R_LNKCFG0, new)
        nb, v2 = mmio_alive(m)
        rl = rd(m, R_LNKCFG0)
        print(f"  0x8C040      = {l:#010x} → 写入 {new:#010x}  回读 {rl:#010x}  "
              f"(SPEED=1 +COMMIT（★高危）)")
        if not nb or rl == 0xFFFFFFFF:
            panic(m, "0x8C040 写入之后")
    return True

def speed_now(m, tag=""):
    """只写 SPEED[19:18]=1，不发 COMMIT。
    重训期间 BAR0 可能短暂返回全 1，允许 1 秒内重试；一直全 1 才算真死。
    """
    l = rd(m, R_LNKCFG0)
    if ((l >> 18) & 0x3) == 1:
        print(f"  [{tag}] SPEED 已经是 1（{l:#010x}），跳过")
        return True
    new = (l & ~0x000C0000) | (1 << 18)
    wr(m, R_LNKCFG0, new)
    for i in range(20):
        time.sleep(0.05)
        if rd(m, R_CAP2) != 0xFFFFFFFF:
            print(f"  [{tag}] 0x8C040 = {l:#010x} → {new:#010x}  "
                  f"回读 {rd(m, R_LNKCFG0):#010x}  CAP2={rd(m, R_CAP2):#06x}  "
                  f"（第 {i+1} 次读到有效值）")
            return True
    print(f"  [{tag}] !! 写 SPEED 后 1 秒内 BAR0 一直 0xffffffff —— 端点已死")
    return False

def commit_now(m, tag=""):
    """单独发一次 COMMIT（latecommit 用）"""
    l = rd(m, R_LNKCFG0)
    new = (l & ~0x000C0000) | (1 << 18) | 1
    wr(m, R_LNKCFG0, new)
    rl = rd(m, R_LNKCFG0)
    ok, _ = mmio_alive(m)
    print(f"  [{tag}] 0x8C040 = {l:#010x} → {new:#010x}  回读 {rl:#010x}")
    if not ok or rl == 0xFFFFFFFF:
        panic(m, "late COMMIT")

# ---- 第 2 步 -------------------------------------------------------------
def step2():
    if ARGS["skip_reset"]:
        print("\n=== 第2步：★跳过（--skip-reset，验证不需要复位）===")
        return True
    print(f"\n=== 第2步：remove → rescan（unbind={ARGS['unbind']}）===")
    drv_path = "/sys/bus/pci/drivers/nvidia"
    if ARGS["unbind"] and os.path.exists(f"/sys/bus/pci/devices/{DEV}/driver"):
        try:
            with open(f"{drv_path}/unbind", "w") as f:
                f.write(DEV)
            print("  已 unbind 驱动")
            time.sleep(1)
        except Exception as e:
            print(f"  unbind 失败: {e}")

    try:
        with open(f"/sys/bus/pci/devices/{DEV}/remove", "w") as f:
            f.write("1")
    except Exception as e:
        print(f"  remove 失败: {e}"); return False

    gone = False
    for _ in range(40):
        time.sleep(0.2)
        if not on_bus():
            gone = True; break
    print(f"  设备离开总线: {gone}")
    if not gone:
        print("  !! 没能让设备离开总线"); return False

    sh("echo 1 > /sys/bus/pci/rescan")
    back = False
    for i in range(60):
        time.sleep(0.5)
        if on_bus():
            print(f"  设备回来（{(i+1)*0.5:.1f}s）")
            back = True; break
    if not back:
        print("  !! 设备没有回来")
        return False

    sh(f"echo 1 > /sys/bus/pci/devices/{DEV}/enable")
    sh(f"echo on > /sys/bus/pci/devices/{DEV}/power/control")
    time.sleep(1.5)
    print(f"  power_state = {sh(f'cat /sys/bus/pci/devices/{DEV}/power_state').stdout.strip()}")
    return True

def wait_latch(secs=25):
    print(f"\n=== 等声明锁存（最多 {secs}s）===")
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(4)
        v = ep_cap2_pci()
        if v is None:
            print("  端点读不到（可能掉了）"); return False
        print(f"  [{time.time()-t0:.0f}s] LCAP2={v:#010x}"
              + ("  ✓ 已锁存 Gen2" if (v & 0x6) == 0x6 else ""))
        if (v & 0x6) == 0x6:
            return True
    return False

# ---- 第 3 步 -------------------------------------------------------------
def step3(m):
    print("\n=== 第3步：两端 TLS=2 + 交替重训 ===")
    cur = rd(m, R_LNKCTL2)
    if (cur & 0xF) != 2:
        wr(m, R_LNKCTL2, (cur & ~0xF) | 2)
        print(f"  端点   LNKCTL2 = {cur:#06x} → {rd(m, R_LNKCTL2):#06x}")
    else:
        print(f"  端点   LNKCTL2 = {cur:#06x}（TLS 已是 2，跳过）")

    rc = rp_lnkctl2()
    if rc is None:
        print("  根端口读不到，跳过")
    elif (rc & 0xF) != 2:
        sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rc & ~0xF) | 2):04x}")
        print(f"  根端口 LNKCTL2 = {rc:#06x} → {rp_lnkctl2():#06x}")
    else:
        print(f"  根端口 LNKCTL2 = {rc:#06x}（TLS 已是 2，跳过）")

    # ★★ 关键：先看一眼，已经到目标速率就绝不重训。
    #   实测（reports/179 T0）：不动复位、只写 SPEED 之后两端已经是
    #   Gen2（端点 0x1042 / 根端口 0xd042），再发 Retrain Link 反而把端点打死。
    ep0, rp0 = ep_lnksta(), rp_lnksta()
    if ep0 is not None and (ep0 & 0xF) >= 2:
        print(f"  端点 LNKSTA={ep0:#06x} sp={ep0 & 0xF} 已达目标 → **跳过重训**"
              f"（根端口 {(f'{rp0:#06x}' if rp0 is not None else 'N/A')}）")
        return True
    if ARGS["no_retrain"]:
        print(f"  --no-retrain：跳过重训（当前端点 "
              f"{(f'{ep0:#06x}' if ep0 is not None else 'N/A')}）")
        return ep0 is not None and (ep0 & 0xF) >= 2

    for i in range(6):                     # 参考源码用 6 轮 / 每轮 sleep 700ms
        rl = sh(f"setpci -s {ROOT} CAP_EXP+0x10.w").stdout.strip()
        if rl:
            sh(f"setpci -s {ROOT} CAP_EXP+0x10.w={((int(rl,16)) | 0x20):04x}")
        time.sleep(0.1)
        v = rd(m, R_LNKSTA)
        wr(m, R_LNKSTA, v | 0x20)
        time.sleep(0.7)
        ep, rp = ep_lnksta(), rp_lnksta()
        if ep is None:
            print(f"  第{i+1}轮: 端点读不到"); return False
        rps = f"{rp:#06x}" if rp is not None else "N/A"
        print(f"  第{i+1}轮: 端点 LNKSTA={ep:#06x} sp={ep & 0xF} | 根端口={rps}")
        if (ep & 0xF) >= 2:
            return True
    return False

# ---- 第 4 步 -------------------------------------------------------------
def step4(secs=10):
    """按住循环。
    ★ 严格按 mode 决定 0x8C040 写什么 —— 上一版这里写成 `mode != "nocommit"`，
      导致 speedonly 也每 20ms 发一次 COMMIT，几毫秒就把卡打死（reports/176）。
    """
    print(f"\n=== 第4步：按住 {secs}s（每 20ms，mode={ARGS['mode']}）===")
    t0 = time.time(); n = 0; pushback = 0
    while time.time() - t0 < secs:
        try:
            fd, m = bar()
            c = rd(m, R_CONFIG)
            wr(m, R_CONFIG, (c | (1 << 11) | (1 << 13) | (1 << 15)) & ~((1 << 12) | (1 << 14)))
            l = rd(m, R_LNKCFG0)
            if ARGS["mode"] in ("full", "latecommit"):
                wr(m, R_LNKCFG0, (l & ~0x000C0000) | (1 << 18) | 1)   # 带 COMMIT（高危）
            elif ARGS["mode"] in ("speedonly", "latespeed"):
                if ((l >> 18) & 0x3) != 1:
                    pushback += 1
                    wr(m, R_LNKCFG0, (l & ~0x000C0000) | (1 << 18))   # 只改 SPEED
            cur = rd(m, R_LNKCTL2)
            if (cur & 0xF) != 2:
                wr(m, R_LNKCTL2, (cur & ~0xF) | 2)
            ok, v = mmio_alive(m)
            rl = rd(m, R_LNKCFG0)
            m.close(); os.close(fd)
            if not ok or rl == 0xFFFFFFFF:
                print(f"  !! MMIO 失效（n={n}, CAP2={v:#010x}, lnkcfg0={rl:#010x}）")
                return False
            rc = rp_lnkctl2()
            if rc is not None and (rc & 0xF) != 2:
                sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rc & ~0xF) | 2):04x}")
            n += 1
        except OSError:
            print("  !! MMIO 映射失败"); return False
        if n % 100 == 0:
            ep, rp = ep_lnksta(), rp_lnksta()
            es = f"{ep & 0xF}" if ep is not None else "?"
            rs = f"{rp & 0xF}" if rp is not None else "?"
            print(f"  [{time.time()-t0:.1f}s n={n}] 端点 sp={es} | 根端口 sp={rs} | 驱动回压={pushback}")
        time.sleep(0.02)
    ep = ep_lnksta()
    if ep is None:
        print("  结束: 端点不可读"); return False
    print(f"  结束: 端点 LNKSTA={ep:#06x} sp={ep & 0xF} | 驱动回压次数={pushback}")
    if ARGS["mode"] in ("speedonly", "latespeed"):
        print("  （驱动回压=0 说明 580.178.04 根本不写 0x8C040，按住循环可以省掉）")
    return (ep & 0xF) == 2

# ---- main ----------------------------------------------------------------
STATEF = ""

def parse_args():
    for a in sys.argv[1:]:
        if a.startswith("--mode="):
            ARGS["mode"] = a.split("=", 1)[1]
        elif a == "--no-unbind":
            ARGS["unbind"] = False
        elif a == "--no-bw":
            ARGS["bw"] = False
        elif a == "--reload":
            ARGS["reload"] = True
        elif a == "--skip-reset":
            ARGS["skip_reset"] = True
        elif a == "--no-retrain":
            ARGS["no_retrain"] = True
        elif a == "--unbind-first":
            ARGS["unbind_first"] = True
        elif a.startswith("--hold="):
            ARGS["hold"] = int(a.split("=", 1)[1])
    if ARGS["mode"] not in ("nocommit", "speedonly", "latecommit", "latespeed", "full"):
        print(f"未知 mode: {ARGS['mode']}"); sys.exit(1)

def main():
    global STATEF
    if os.geteuid() != 0:
        print("需要 root"); return 1
    parse_args()
    STATEF = f"/data/labs/nvidia/pcie-gen2/state-{time.strftime('%Y%m%d-%H%M%S')}.json"
    sh("echo 'RECIPE-START-MARK' > /dev/kmsg")

    print(f"模式: mode={ARGS['mode']} unbind={ARGS['unbind']}  状态文件: {os.path.basename(STATEF)}")
    print(f"设备在线: {on_bus()}")
    if not on_bus():
        print("设备不在总线上，先 rescan"); sh("echo 1 > /sys/bus/pci/rescan"); time.sleep(2)
        if not on_bus():
            print("rescan 后仍不在，需要重启"); return 1

    fd, m = bar()
    show(state("起点", m))
    m.close(); os.close(fd)
    bw("Gen1 基线")

    # ★ H4：写 SPEED 触发重训，若驱动正在访问 GPU，重训可能被流量打断。
    #   所以先把驱动解绑（只解绑，不 remove），让链路空闲，再动寄存器。
    if ARGS["unbind_first"]:
        print("\n=== 第0步：先 unbind 驱动（只解绑，不 remove）===")
        if bound():
            print(f"  unbind {'成功' if bind(False) else '失败'}")
            time.sleep(1.5)
        print(f"  现在 bound={bound()}")
        bw("unbind 后基线")

    fd, m = bar()
    if not step1(m):
        m.close(); os.close(fd); return 1
    show(state("第1步后", m))
    m.close(); os.close(fd)

    if not step2():
        print("\n第2步失败"); return 1

    fd, m = bar()
    show(state("复位后", m))
    ok_mmio, v = mmio_alive(m)
    if not ok_mmio:
        panic(m, "第2步复位后")
    latch_ok = wait_latch()
    show(state("锁存后", m))

    if ARGS["mode"] == "latespeed":
        print("\n=== 第2.5步：声明已锁存 + 已重枚举 → 现在写 SPEED ===")
        if latch_ok:
            # ★ V3 关键改动：先把根端口 TLS 也设成 2。
            #   原顺序下根端口 TLS=3(Gen3)，端点请求 5GT/s → 两端目标不一致
            #   → 训练要协商降速 → 协商失败就卡死掉卡（E 成 / F 败，reports/177）。
            rc = rp_lnkctl2()
            if rc is not None and (rc & 0xF) != 2:
                sh(f"setpci -s {ROOT} CAP_EXP+0x30.w={((rc & ~0xF) | 2):04x}")
                print(f"  根端口 TLS2 = {rc:#06x} → {rp_lnkctl2():#06x}  "
                      f"（先让两端同目标，再请求速率）")
            ep = _rd_pci(DEV, "CAP_EXP+0x30.w")
            if ep is not None and (ep & 0xF) != 2:
                sh(f"setpci -s {DEV} CAP_EXP+0x30.w={((ep & ~0xF) | 2):04x}")
                print(f"  端点   TLS2 = {ep:#06x} → {_rd_pci(DEV,'CAP_EXP+0x30.w'):#06x}")
            if not speed_now(m, "late"):
                panic(m, "late SPEED 之后")
        else:
            print("  声明没锁存，跳过 late SPEED")
        show(state("late SPEED 后", m))
    elif ARGS["mode"] == "latecommit":
        if latch_ok:
            commit_now(m, "late")
        else:
            print("\n声明没锁存，跳过 late COMMIT")
        show(state("late COMMIT 后", m))

    up = step3(m)
    m.close(); os.close(fd)
    if not up:
        print("\n第3步未升速，仍尝试按住")
    held = step4(ARGS["hold"])

    print("\n=== 停止按住后的空闲观察（参考项目称会立刻掉回 Gen1）===")
    for i in range(3):
        time.sleep(5)
        ep, rp = ep_lnksta(), rp_lnksta()
        es = f"{ep:#06x}" if ep is not None else "N/A"
        rs = f"{rp:#06x}" if rp is not None else "N/A"
        print(f"  空闲 {(i+1)*5}s: 端点 LNKSTA={es} sp={'-' if ep is None else ep & 0xF} | 根端口={rs}")

    print("\n=== 驱动验证：先测活着的驱动（★不重载）===")
    if ARGS["unbind_first"] and not bound():
        print("  （第0步解绑过，现在重新 bind）")
        if bind(True):
            time.sleep(4)
    drv("按住结束时")
    bw("Gen2 结果")

    if ARGS["reload"]:
        print("\n=== 可选：重载驱动（这一步在 Gen2 上很可能失败，见 reports/178）===")
        sh("for mm in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do "
           "lsmod | grep -q \"^$mm \" && rmmod $mm; done")
        time.sleep(2)
        sh("modprobe nvidia")
        time.sleep(5)
        drv("重载后")

    print(f"\n=== 小结 === mode={ARGS['mode']} 声明锁存={latch_ok} "
          f"第3步升级={up} 按住保持={held}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
