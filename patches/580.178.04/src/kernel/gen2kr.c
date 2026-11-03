// SPDX-License-Identifier: GPL-2.0
/*
 * gen2kr.c —— kretprobe 拦截 nvidia.ko 的 probe 末尾，把 PCIe 链路推到 Gen2
 *
 * 依据（reports/183 N194 实测）：
 *   · 驱动 probe 时**只改 0x8C040.SPEED（1→2）**，不碰 TLS
 *   · 驱动不在时 TLS=2 连续 5 轮重训都不回滚（0/5）⇒ 是驱动主动写
 *   · 参考实现 ref/cmpunlocker/driver/patches/pcie-gen2-probe-retrain.patch
 *     把这段挂在 nv_pci_probe 末尾，且**全部用普通 writel，不用 SEC2**
 *
 * ★ 关键约束（实测踩过）：
 *   kretprobe handler 运行在【原子上下文】，里面不能 msleep/ioremap 睡眠。
 *   第一次实现直接在 handler 里做，触发 "BUG: scheduling while atomic"。
 *   现在 handler 只 schedule_delayed_work，真正操作放 workqueue。
 *
 * 序列（照参考 patch，适配 Pascal）：
 *   ① 解锁声明：XP3G_OVR0/3 + VAL0/3 + 0x8841C + 0x88610
 *   ② 清 0x8C2C0 bit2 (DIS_G2)
 *   ③ 0x8C040 SPEED=1（★Pascal 编码 1=5GT/s；Ampere 是 2）—— 绝不发 COMMIT
 *   ④ 0x8872C = 6
 *   ⑤ msleep(50) → 两端 LNKCTL2 TLS=5GT → 根端口 Retrain
 *   ⑥ 轮询 LNKSTA 最多 20×100ms
 *   ⑦ 之后守 30×100ms，把 SPEED 按回 1（防驱动异步改回）
 */
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/pci.h>
#include <linux/pci_regs.h>
#include <linux/delay.h>
#include <linux/io.h>
#include <linux/workqueue.h>
#include <linux/fs.h>
#include <linux/file.h>

#define NV_VENDOR   0x10de
#define NV_DEV_P104 0x1b87

#define OFF_XP3G_OVR0  0x8E110
#define OFF_XP3G_OVR3  0x8E11C
#define OFF_XP3G_VAL0  0x8E120
#define OFF_XP3G_VAL3  0x8E12C
#define OFF_CONFIG     0x8841C   /* PRIV_MISC_1 */
#define OFF_VSEC       0x88610   /* VSEC_HIERARCHY */
#define OFF_CAP2       0x880A4   /* LINK_CAP2 镜像 */
#define OFF_LTSSM      0x8872C   /* XVE_OVR */
#define OFF_LINKCFG0   0x8C040   /* LINK_CONFIG_0 */
#define OFF_CYA        0x8C2C0   /* CYA_0, bit2 = DIS_G2 */
#define BAR0_LEN       0x90000

static int guard_rounds = 0;     /* 0 = 常驻；>0 = 只守这么多轮（每轮 100ms） */
module_param(guard_rounds, int, 0644);
MODULE_PARM_DESC(guard_rounds, "守卫轮数，0=常驻（默认）");

static struct kretprobe krp;
static bool triggered;
static void __iomem *early_bar;      /* init 时预映射，handler 里直接用（不睡眠） */

static struct delayed_work gen2_work;
static int guard_left;
static int pushback_cnt;
static int retrain_cnt;
static bool bad_read_logged;    /* 抑制"读到 ffffffff"的重复告警 */
static bool pushing;

/* ---------- 安全约束参数（reports/185：无约束的守卫曾打挂整机） ---------- */
static int retrain_min_ms = 2000;   /* 两次完整重训尝试之间的最小间隔 */
module_param(retrain_min_ms, int, 0644);
MODULE_PARM_DESC(retrain_min_ms, "两次重训尝试最小间隔 ms，默认 2000；0=不限（危险）");

/*
 * ★ stage0(写 0x8872C=6) → stage1(根端口 Retrain) 的间隔（reports/196）
 *
 * 这个参数此前被 retrain_min_ms 粗暴复用，导致 stage 间被强制隔 2 秒。
 * 实测证据：`push_fn` 用 msleep(50) 就能一次成功（104ms 内 LNKSTA=1042），
 * 而 guard 等 2 秒再 Retrain 却连续 4 轮失败。
 * 把该间隔改到 150ms 后，恢复时间 9.59s → 5.31s。
 *
 * 不能设 0：写 0x8872C=6 后硬件需要一点时间启动训练，
 * 立刻 Retrain 会打断它（push 也是 50ms 后才 Retrain）。
 */
static int stage_gap_ms = 150;
module_param(stage_gap_ms, int, 0644);
MODULE_PARM_DESC(stage_gap_ms, "stage0→stage1 间隔 ms，默认 150（模仿 push_fn 的 50ms+余量）");

static int debounce_cnt = 3;        /* 连续多少次读到 Gen1 才重训 */
module_param(debounce_cnt, int, 0644);
MODULE_PARM_DESC(debounce_cnt, "重训防抖次数，默认 3（3×200ms=600ms；原 10）");

static int guard_period_ms = 200;   /* 守卫周期，默认 200ms（原 100ms） */

/*
 * 是否允许在 GPU 忙时重训。
 *   默认 1（允许）—— 依据：reports/184 旧版守卫无忙检测，CUDA 期间也重训，
 *   带宽达到 1.56 且 Xid=0；而"打挂整机"已被证否（reports/185 §5，实为 S3 睡眠）。
 *   设 0 则保守：忙时只按回 SPEED、不重训（实测带宽会掉到 0.78）。
 */
/*
 * ★ 按回 SPEED 后等多久才允许重训（reports/197 实测下调）
 *
 * 这个值原名 3000，来自 reports/186 配方「⑤ 写 SPEED → ⑥ 等 ≥3s 再 Retrain」。
 * 实测扫描（tools/sweep-settle.sh，每个值跑一次真实"降级→恢复"）：
 *     3000ms → 5.63s 恢复      1500ms → 4.33s
 *      800ms → 3.63s           500ms → 3.43s      300ms → 3.22s
 * 全部 OK、无一次掉卡。收益在 800ms 之后已明显递减（瓶颈转到 Retrain 生效的
 * ~1.8s 物理延迟 + debounce），故取 800 留 2.6 倍余量，不取更激进的 300。
 *
 * ⚠ 不能设到毫秒级：reports/186 铁律 #1 —— 写完 SPEED 立刻重训会死锁。
 */
static int speed_settle_ms = 800;
module_param(speed_settle_ms, int, 0644);
MODULE_PARM_DESC(speed_settle_ms, "按回 SPEED 后等待多少 ms 才允许重训，默认 800");

static int allow_busy_retrain = 1;
module_param(allow_busy_retrain, int, 0644);
MODULE_PARM_DESC(allow_busy_retrain, "1=GPU忙时也重训(默认)；0=忙时跳过");
module_param(guard_period_ms, int, 0644);
MODULE_PARM_DESC(guard_period_ms, "守卫周期 ms，默认 200");

/* ---------- 守卫状态 ---------- */
static unsigned long last_retrain_jiffies;
/*
 * ★ 最后一次"按回 SPEED"的时刻。
 *   铁律（reports/186 #1）：写完 SPEED 后必须等 ≥3s 才能重训，否则重训无效
 *   （毫秒级重训甚至会死锁）。实测：守卫按回 SPEED 后 1 秒内重训，LNKSTA 一直 1041。
 */
static unsigned long last_speed_fix_jiffies;

/*
 * ★ 重训两步（reports/186 配方，reports/196 修正间隔）：
 *     第1步 写 0x8872C = 6  （触发 LTSSM 训练，异步）
 *     第2步 从根端口 Retrain（让协商真正发生）
 *
 *   只做第2步实测无效：旧版守卫重训 16 次，LNKSTA 始终 1041。
 *
 *   ★ 但两步之间的间隔**不是 3 秒**（reports/196 实测修正）：
 *     push_fn 用 msleep(50) 一次成功（104ms 内 LNKSTA=1042）；
 *     早先 guard 拿 retrain_min_ms(2000) 当 stage 间隔，连 4 轮全失败。
 *     现在用独立的 stage_gap_ms(150)，且 stage1 不再重新走防抖。
 */
static int retrain_stage;
static unsigned long stage_start_jiffies;  /* stage0 的时刻，用于算 stage_gap_ms */
static unsigned long last_attempt_jiffies; /* 上次完整尝试（stage0）的时刻 */
static int low_streak;              /* 连续读到 Gen1 的次数（防抖计数） */


/*
 * 是否有进程正在使用 GPU？
 *
 * ★ 不用 find_module()：Linux 6.12 已不再导出该符号（实测 modpost 报 undefined）。
 *   改为直接读 /sys/module/nvidia/refcnt —— 不依赖任何内核 API，最稳。
 *
 * 判据：refcnt 高于"基线"即认为忙。
 *   基线在模块 init 时采样（此时还没人开始用 GPU）。
 *   nvidia_uvm/nvidia_modeset 常驻会持有一份引用，所以必须用"高于基线"而非 ">0"。
 *
 * 保守：读不到就返回 true（当作忙），宁可不修，不可打挂。
 */
static int nvidia_ref_baseline = -1;

static int read_nvidia_refcnt(void)
{
    struct file *f;
    char buf[16] = {0};
    loff_t pos = 0;
    ssize_t n;
    int val;

    f = filp_open("/sys/module/nvidia/refcnt", O_RDONLY, 0);
    if (IS_ERR(f))
        return -1;
    n = kernel_read(f, buf, sizeof(buf) - 1, &pos);
    filp_close(f, NULL);
    if (n <= 0)
        return -1;
    if (kstrtoint(buf, 10, &val))
        return -1;
    return val;
}

static bool gpu_busy(void)
{
    int refs = read_nvidia_refcnt();

    if (refs < 0)
        return true;                 /* 读不到 → 保守当作忙 */

    if (nvidia_ref_baseline < 0) {
        nvidia_ref_baseline = refs;
        return false;
    }
    return refs > nvidia_ref_baseline;
}

static bool guard_enabled = true;
module_param(guard_enabled, bool, 0644);
MODULE_PARM_DESC(guard_enabled, "总开关；0=守卫完全不动作（只做初始推送）");

/* ---------- 第 2 阶段：守卫（安全版） ---------- */
static void guard_fn(struct work_struct *w)
{
    struct pci_dev *gpu;
    void __iomem *bar;
    u32 v, lk;
    u16 st = 0;
    bool low;

    if (guard_left == 0 && guard_rounds > 0)
        return;
    if (guard_left > 0)
        guard_left--;

    gpu = pci_get_device(NV_VENDOR, NV_DEV_P104, NULL);
    if (!gpu)
        goto next;

    bar = ioremap(pci_resource_start(gpu, 0), BAR0_LEN);
    if (bar) {
        v = readl(bar + OFF_CAP2);
        if (v == 0xFFFFFFFFu)
            goto unmap;

        /* ① SPEED 按回（纯小 MMIO 写，无副作用，可以每轮做） */
        lk = readl(bar + OFF_LINKCFG0);
        /*
         * ★★★ 三重校验，缺一不可（reports/196 —— 这是一次真实掉卡的根因）
         *
         * 曾只校验 CAP2 != 0xffffffff，就放心按回 SPEED。但 LINKCFG0 自己
         * 也可能在链路重训瞬态读到 0xffffffff，此时：
         *     0xFFFFFFFF & ~0x000C0000 = 0xFFF3FFFF
         *     0xFFF3FFFF | (1 << 18)   = 0xFFF7FFFF
         *                                    ↑ bit0 = 1 = COMMIT → 必掉卡！
         * 实测代价：设备进 C. ZOMBIE，只能整机重启。
         *
         * 所以：① 不能是 0xffffffff ② bit0(COMMIT) 必须为 0 ③ 才看 SPEED 位
         */
        if (lk == 0xFFFFFFFFu) {
            if (!bad_read_logged) {
                pr_warn("gen2kr: [guard] LINKCFG0 读到 0xffffffff，本轮跳过按回\n");
                bad_read_logged = true;
            }
            goto unmap;
        }
        bad_read_logged = false;
        if ((lk & 1u) != 0) {
            /* COMMIT 位已置位 —— 绝不在此基础上写回，只报警 */
            pr_warn_ratelimited("gen2kr: [guard] LINKCFG0 bit0(COMMIT)=1，拒绝写回（lk=%08x）\n", lk);
            goto unmap;
        }
        if (((lk >> 18) & 0x3) != 1) {
            writel((lk & ~0x000C0000u) | (1u << 18), bar + OFF_LINKCFG0);
            last_speed_fix_jiffies = jiffies;
            pushback_cnt++;
            pr_info_ratelimited("gen2kr: [guard] SPEED 按回 1（第 %d 次，原 %08x）\n",
                                pushback_cnt, lk);
        }

        /* ② 链路是否真的掉到 Gen1 —— 只在"稳态低"时才考虑重训 */
        if (pcie_capability_read_word(gpu, PCI_EXP_LNKSTA, &st))
            goto unmap;
        low = (st & PCI_EXP_LNKSTA_CLS) < PCI_EXP_LNKSTA_CLS_5_0GB;

        if (!low) {
            low_streak = 0;          /* 已经好了，清零 */
            retrain_stage = 0;       /* ★ 也重置 stage，避免下次掉速直接跳到 Retrain */
            goto unmap;
        }
        low_streak++;

        if (!guard_enabled)
            goto unmap;

        /* ============ stage 1：同一次尝试的第二步，不再重新防抖 ============ */
        if (retrain_stage == 1) {
            struct pci_dev *up;
            u16 ctl = 0;

            /* 只等 stage_gap_ms（模仿 push_fn 的 msleep(50)+余量） */
            if (time_before(jiffies,
                            stage_start_jiffies + msecs_to_jiffies(stage_gap_ms)))
                goto unmap;

            up = pci_upstream_bridge(gpu);
            if (up && !pcie_capability_read_word(up, PCI_EXP_LNKCTL, &ctl)) {
                pcie_capability_write_word(up, PCI_EXP_LNKCTL,
                                           ctl | PCI_EXP_LNKCTL_RL);
                pr_info("gen2kr: [guard] 根端口 Retrain（LNKSTA=%04x，stage 间隔 %dms）\n",
                        st, stage_gap_ms);
            }
            retrain_stage = 0;
            low_streak = 0;
            goto unmap;
        }

        /* ============ stage 0：需要完整的三重闸门 ============ */
        if (low_streak < debounce_cnt)
            goto unmap;

        /*
         * ★ 曾尝试在这里加「前置条件自愈」（TLS / 声明），已回滚（reports/198 §失败实验）
         *
         * 设想：驱动 reload 后会把端点 TLS 钳回 1、声明重置为 Gen1，
         *       所以守卫在这里重写 TLS=2 + 重新解锁声明。
         * 实测：**无效**。
         *   · 写 TLS 2 后立刻读回 1（`端点 TLS2 0001 → 0002` 每 600ms 重复，
         *     而下一轮又读到 0001）—— 被驱动钳住，抢不过
         *   · 声明解锁后 CAP2 仍 0x0002（CAP2 要经 0x8872C=6 才锁存，
         *     但即便触发了训练也救不回来）
         *   · 6 次 unbind/bind + 9 次解锁，链路始终 LNKSTA=1
         *   · 而且每 600ms 猛写配置空间本身有风险
         * ⇒ 回滚。正确做法是整机重启回到干净状态（见 reports/198 结论）。
         */
        if (time_before(jiffies,
                        last_attempt_jiffies + msecs_to_jiffies(retrain_min_ms)))
            goto unmap;
        /* ★ 按回 SPEED 后必须等够，否则重训无效（reports/186 铁律 #1） */
        if (time_before(jiffies,
                        last_speed_fix_jiffies + msecs_to_jiffies(speed_settle_ms)))
            goto unmap;
        if (!allow_busy_retrain && gpu_busy()) {
            pr_info_ratelimited("gen2kr: [guard] GPU 忙，本轮不重训\n");
            goto unmap;
        }

        /* 第1步：触发 LTSSM 训练 */
        writel(0x00000006, bar + OFF_LTSSM);
        retrain_stage = 1;
        stage_start_jiffies = jiffies;
        last_attempt_jiffies = jiffies;
        last_retrain_jiffies = jiffies;
        low_streak = 0;
        retrain_cnt++;
        pr_info("gen2kr: [guard] 触发训练 0x8872C=6（总第 %d 次，LNKSTA=%04x）\n",
                retrain_cnt, st);
unmap:
        iounmap(bar);
    }
    pci_dev_put(gpu);

next:
    if (guard_rounds > 0 && guard_left <= 0) {
        pr_info("gen2kr: 守卫结束，共按回 SPEED %d 次，重训 %d 次\n",
                pushback_cnt, retrain_cnt);
        return;
    }
    schedule_delayed_work(&gen2_work, msecs_to_jiffies(guard_period_ms));
}

/* ---------- 第 1 阶段：真正推 Gen2（在 workqueue 里，可睡眠） ---------- */
static void push_fn(struct work_struct *w)
{
    struct pci_dev *gpu;
    struct pci_dev *up;
    void __iomem *bar;
    u32 v;
    u16 ctl2, ctl, sta = 0;
    int i;

    pushing = false;
    gpu = pci_get_device(NV_VENDOR, NV_DEV_P104, NULL);
    if (!gpu) {
        pr_warn("gen2kr: 找不到 P104\n");
        return;
    }

    bar = ioremap(pci_resource_start(gpu, 0), BAR0_LEN);
    if (!bar) {
        pr_err("gen2kr: ioremap 失败\n");
        goto put;
    }

    pr_info("gen2kr: [push] 起始 cap2=%08x config=%08x lnkcfg0=%08x\n",
            readl(bar + OFF_CAP2), readl(bar + OFF_CONFIG),
            readl(bar + OFF_LINKCFG0));

    /* ① 解锁声明 */
    writel(0x00000001, bar + OFF_XP3G_OVR0);
    writel(0x00000004, bar + OFF_XP3G_OVR3);
    writel(0x00000000, bar + OFF_XP3G_VAL0);
    writel(0x00200000, bar + OFF_XP3G_VAL3);
    v = readl(bar + OFF_CONFIG);
    writel((v | (1u << 11) | (1u << 13) | (1u << 15)) & ~((1u << 12) | (1u << 14)),
           bar + OFF_CONFIG);
    v = readl(bar + OFF_VSEC);
    writel((v & ~(1u << 12)) | 1u, bar + OFF_VSEC);

    /* ② 清 DIS_G2 */
    v = readl(bar + OFF_CYA);
    writel(v & ~(1u << 2), bar + OFF_CYA);

    /* ③ SPEED = 1（Pascal），绝不发 COMMIT */
    /*
     * ★ 与 guard 同一道防线（reports/196）：从 0xffffffff 派生会带上 COMMIT。
     *   push 阶段读到 ffffffff 的概率较低，但一旦发生就是必掉卡，必须校验。
     */
    v = readl(bar + OFF_LINKCFG0);
    if (v == 0xFFFFFFFFu || (v & 1u) != 0) {
        pr_err("gen2kr: [push] LINKCFG0=%08x 不可信（ffffffff 或 COMMIT=1），跳过 SPEED 写入\n", v);
    } else {
        writel((v & ~0x000C0000u) | (1u << 18), bar + OFF_LINKCFG0);
    }

    /* ④ XVE_OVR */
    writel(0x00000006, bar + OFF_LTSSM);

    pr_info("gen2kr: [push] 写完 cap2=%08x config=%08x lnkcfg0=%08x cya=%08x\n",
            readl(bar + OFF_CAP2), readl(bar + OFF_CONFIG),
            readl(bar + OFF_LINKCFG0), readl(bar + OFF_CYA));
    iounmap(bar);

    /* 参考 patch 的 msleep(50) */
    msleep(50);

    /* ⑤ 两端 TLS=5GT，根端口 Retrain */
    if (!pcie_capability_read_word(gpu, PCI_EXP_LNKCTL2, &ctl2)) {
        pcie_capability_write_word(gpu, PCI_EXP_LNKCTL2,
                                   (ctl2 & ~PCI_EXP_LNKCTL2_TLS) |
                                   PCI_EXP_LNKCTL2_TLS_5_0GT);
        pr_info("gen2kr: 端点 TLS2 %04x -> %04x\n", ctl2,
                (ctl2 & ~PCI_EXP_LNKCTL2_TLS) | PCI_EXP_LNKCTL2_TLS_5_0GT);
    }

    up = pci_upstream_bridge(gpu);
    if (up) {
        if (!pcie_capability_read_word(up, PCI_EXP_LNKCTL2, &ctl2)) {
            pcie_capability_write_word(up, PCI_EXP_LNKCTL2,
                                       (ctl2 & ~PCI_EXP_LNKCTL2_TLS) |
                                       PCI_EXP_LNKCTL2_TLS_5_0GT);
            pr_info("gen2kr: 根端口 TLS2 %04x -> %04x\n", ctl2,
                    (ctl2 & ~PCI_EXP_LNKCTL2_TLS) | PCI_EXP_LNKCTL2_TLS_5_0GT);
        }
        if (!pcie_capability_read_word(up, PCI_EXP_LNKCTL, &ctl)) {
            pcie_capability_write_word(up, PCI_EXP_LNKCTL, ctl | PCI_EXP_LNKCTL_RL);
            pr_info("gen2kr: 根端口 LNKCTL %04x -> %04x (Retrain)\n",
                    ctl, ctl | PCI_EXP_LNKCTL_RL);
        }
    }

    /* ⑥ 轮询 */
    for (i = 0; i < 20; i++) {
        msleep(100);
        if (pcie_capability_read_word(gpu, PCI_EXP_LNKSTA, &sta))
            continue;
        if ((sta & PCI_EXP_LNKSTA_CLS) >= PCI_EXP_LNKSTA_CLS_5_0GB) {
            pr_info("gen2kr: ★ SUCCESS Gen%u LNKSTA=%04x（第 %d 次）\n",
                    sta & PCI_EXP_LNKSTA_CLS, sta, i + 1);
            break;
        }
    }
    if (i == 20)
        pr_warn("gen2kr: 未升速，LNKSTA=%04x\n", sta);

    /* ⑦ 开始守卫 */
    last_retrain_jiffies = jiffies;
    last_attempt_jiffies = jiffies;
    stage_start_jiffies = jiffies;
    retrain_stage = 0;
    low_streak = 0;
    guard_left = (guard_rounds > 0) ? guard_rounds : 1;
    schedule_delayed_work(&gen2_work, msecs_to_jiffies(100));

put:
    pci_dev_put(gpu);
}

static DECLARE_WORK(push_work, push_fn);

/* ---------- kretprobe handler：只调度，不做事 ---------- */
static int ret_handler(struct kretprobe_instance *ri, struct pt_regs *regs)
{
    u32 lk;

    if (triggered)
        return 0;
    triggered = true;

    /* ★ 极快路径：用预映射的 BAR0 直接写 SPEED（只 writel，不睡眠）
     *   实测 workqueue 版会晚几个 tick，错过 probe 刚返回的窗口；
     *   第一次在原子上下文里直接做反而成功过（LNKSTA=1042）。 */
    if (early_bar) {
        lk = readl(early_bar + OFF_LINKCFG0);
        /* ★ 同样必须挡 COMMIT（reports/196）：只判 ffffffff 不够，
         *   若 lk 的 bit0 已是 1，按回 SPEED 会把 COMMIT 一起写下去 → 掉卡 */
        if (lk != 0xFFFFFFFFu && (lk & 1u) == 0) {
            writel((lk & ~0x000C0000u) | (1u << 18), early_bar + OFF_LINKCFG0);
            writel(0x00000006, early_bar + OFF_LTSSM);
            pr_info("gen2kr: [fast] 直接写 SPEED=1，原 %08x → %08x\n",
                    lk, readl(early_bar + OFF_LINKCFG0));
        } else {
            pr_warn("gen2kr: [fast] LINKCFG0=%08x 不可信，跳过直接写（交给 workqueue）\n", lk);
        }
    }

    if (!pushing) {
        pushing = true;
        schedule_work(&push_work);
        pr_info("gen2kr: nv_pci_probe 已返回，已排队重训\n");
    }
    return 0;
}

static int __init gen2kr_init(void)
{
    int ret;

    memset(&krp, 0, sizeof(krp));
    krp.kp.symbol_name = "nv_pci_probe";
    krp.handler = ret_handler;
    krp.maxactive = 4;

    INIT_DELAYED_WORK(&gen2_work, guard_fn);

    /* 采样 nvidia.ko 的模块引用基线（供 gpu_busy 判据使用） */
    {
        int rc = read_nvidia_refcnt();
        if (rc >= 0) {
            nvidia_ref_baseline = rc;
            pr_info("gen2kr: nvidia refcnt 基线 = %d\n", rc);
        } else {
            pr_warn("gen2kr: 读不到 /sys/module/nvidia/refcnt，gpu_busy 将恒为 true\n");
        }
    }

    /* 预映射 BAR0，供 handler 里无睡眠写使用 */
    {
        struct pci_dev *g = pci_get_device(NV_VENDOR, NV_DEV_P104, NULL);
        if (g) {
            early_bar = ioremap(pci_resource_start(g, 0), BAR0_LEN);
            pr_info("gen2kr: 预映射 BAR0 @ %llx -> %px\n",
                    (unsigned long long)pci_resource_start(g, 0), early_bar);
            pci_dev_put(g);
        }
    }

    ret = register_kretprobe(&krp);
    if (ret) {
        pr_err("gen2kr: register_kretprobe 失败 %d\n", ret);
        if (early_bar) { iounmap(early_bar); early_bar = NULL; }
        return ret;
    }
    pr_info("gen2kr: 已在 nv_pci_probe 上挂 kretprobe\n");
    return 0;
}

static void __exit gen2kr_exit(void)
{
    unregister_kretprobe(&krp);
    cancel_work_sync(&push_work);
    cancel_delayed_work_sync(&gen2_work);
    if (early_bar) { iounmap(early_bar); early_bar = NULL; }
    pr_info("gen2kr: 已卸载\n");
}

module_init(gen2kr_init);
module_exit(gen2kr_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Push NVIDIA P104-100 PCIe link to Gen2 after probe");
