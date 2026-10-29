// ###########################################################################
// ##  ❌ 已证否，请勿使用（reports/187 N198）
// ##
// ##  实测后果：挂上后 `_nv053912rm` 被触发 9.26 亿次 / 约 10 分钟
// ##            （≈154 万次/秒），load average 从 ~1 升到 5.95。
// ##
// ##  原因：
// ##    1. `_nv053912rm` 被 NVIDIA 标记为 **notrace**。
// ##       bpftrace 会明确拒绝：`is not traceable ... cannot attach kprobe`，
// ##       但 `register_kprobe` **不检查** 该属性，硬挂会破坏函数执行。
// ##    2. 该函数内部疑似"写 + 读回校验 + 重试"，我们改了 r8（要写的值），
// ##       校验永远失败 ⇒ 无限重试。
// ##
// ##  验证：卸载本模块后用 mmap 高频采样 0x8C040，166 万次采样 0 变化
// ##        ⇒ 驱动正常状态下根本不写该寄存器，9.26 亿次全是本模块造成的。
// ##
// ##  保留此文件仅作方法论记录 —— 其中的**重定位定位 call 目标**思路是对的
// ##  （见 tools/find_reloc.py），只是目标函数恰好不可插桩。
// ##  主方案请用 gen2kr（kretprobe + 安全守卫）。
// ###########################################################################
// SPDX-License-Identifier: GPL-2.0
/*
 * gen2kp.c —— kprobe 拦截 NVIDIA RM 的 BAR0 写函数，阻止驱动把 SPEED 写成 Gen1
 *
 * ## 为什么这个方案是"治本"
 *
 * 静态分析（reports/185）+ 动态观测（reports/187）确认了完整因果：
 *   1. 驱动有一段例程：读 0x8C040 → 读 0x88088 取当前速率 → 按当前速率回写 SPEED
 *      （sp=1 就写 SPEED=2，即请求 Gen1）
 *   2. 每次 CUDA 初始化都会触发它 → SPEED 被写成 2 → 链路真的降回 Gen1
 *   3. 实测时序：`[3.08s] SPEED 1→2, LNKSTA 1042→1041`，CUDA 启动的瞬间同时发生
 *
 * 之前靠"守卫每 200ms 按回 SPEED=1"只能部分缓解：按回后还需要一次重训才生效，
 * 而 DMA 期间重训被禁止，于是整个 DMA 期间链路都是 Gen1（带宽 0.78）。
 *
 * **本模块在唯一入口拦截，让驱动永远写不进 2** —— SPEED 始终是 1，
 * 链路在下次自然重训时就协商成 Gen2，不需要高频守卫，也不需要 DMA 期间重训。
 *
 * ## 拦截点（由 `tools/find_reloc.py` 解析重定位得出，已用 kallsyms 地址核对）
 *
 *   `.text+0x126cb0` 处 `call` → 符号 `_nv053912rm`（.text+0x5ff6c0）
 *   该函数是 RM 的 BAR0 寄存器**写**封装；相邻的 `_nv053907rm`（0x5ffda0）是读封装。
 *
 *   调用约定（x86-64 SysV，由反汇编推出）：
 *       rdi = pGpu
 *       rsi = 0
 *       rdx = 0
 *       rcx = 寄存器偏移（例如 0x8C040）
 *       r8d = 要写的值
 *       r9d = 0
 *
 * ## 行为
 *
 *   pre_handler 里：
 *     if (rcx == 0x8C040 && SPEED[19:18] == 2)   → 把值改成 SPEED=1
 *   其余调用一律放行，零干预。
 *
 *   只对 10de:1b87（P104-100）生效；找不到设备时不做任何修改。
 */
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/pci.h>
#include <linux/ptrace.h>

#define NV_VENDOR   0x10de
#define NV_DEV_P104 0x1b87

#define OFF_LINKCFG0   0x8C040
#define SPEED_MASK     0x000C0000u   /* bits[19:18] */
#define SPEED_GEN2     1u            /* ★ Pascal: 1 = 5.0 GT/s */
#define SPEED_GEN1     2u            /* ★ Pascal: 2 = 2.5 GT/s */

/* 符号名带版本号，做成参数以便驱动升级后改 */
static char *sym_name = "_nv053912rm";
module_param(sym_name, charp, 0444);
MODULE_PARM_DESC(sym_name, "BAR0 写函数符号名（默认 _nv053912rm）");

static struct kprobe kp;
static unsigned long hit_total;
static unsigned long hit_patched;
static bool gpu_present = true;

static void refresh_gpu_present(void)
{
    struct pci_dev *g = pci_get_device(NV_VENDOR, NV_DEV_P104, NULL);
    gpu_present = (g != NULL);
    if (g)
        pci_dev_put(g);
}

static int pre_handler(struct kprobe *p, struct pt_regs *regs)
{
    u32 offset, value, speed;

    hit_total++;

    offset = (u32)regs->cx;
    if (offset != OFF_LINKCFG0)
        return 0;

    value = (u32)regs->r8;
    speed = (value & SPEED_MASK) >> 18;

    if (speed != SPEED_GEN1)
        return 0;                    /* 不是"请求 Gen1"，放行 */

    if (!gpu_present)
        refresh_gpu_present();
    if (!gpu_present)
        return 0;                    /* 不是我们的卡，不干预 */

    /* ★ 改成请求 Gen2 */
    regs->r8 = (value & ~SPEED_MASK) | (SPEED_GEN2 << 18);
    hit_patched++;

    pr_info_ratelimited("gen2kp: 拦截 SPEED 2→1 @0x8C040  %08x → %08lx（第 %lu 次）\n",
                        value, (unsigned long)regs->r8, hit_patched);
    return 0;
}

static int __init gen2kp_init(void)
{
    int ret;

    memset(&kp, 0, sizeof(kp));
    kp.symbol_name = sym_name;
    kp.pre_handler = pre_handler;

    ret = register_kprobe(&kp);
    if (ret) {
        pr_err("gen2kp: register_kprobe(%s) 失败 %d\n", sym_name, ret);
        pr_err("gen2kp: 可能是符号名不匹配（驱动升级会改版本号），"
               "用 sym_name=... 指定\n");
        return ret;
    }
    refresh_gpu_present();
    pr_info("gen2kp: 已挂 kprobe 于 %s (addr=%px)，P104 存在=%d\n",
            sym_name, kp.addr, gpu_present);
    return 0;
}

static void __exit gen2kp_exit(void)
{
    unregister_kprobe(&kp);
    pr_info("gen2kp: 已卸载。总调用 %lu，拦截改写 %lu 次\n",
            hit_total, hit_patched);
}

module_init(gen2kp_init);
module_exit(gen2kp_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Intercept NVIDIA RM BAR0 write to keep PCIe SPEED at Gen2 (P104)");
