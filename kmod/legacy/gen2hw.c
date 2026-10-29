// SPDX-License-Identifier: GPL-2.0
/*
 * gen2hw.c —— 用硬件断点观测（可选改写）NVIDIA RM 的寄存器访问函数
 *
 * ## 为什么用硬件断点而不是 kprobe
 *
 * `_nv053912rm` 被 NVIDIA 标记为 **notrace**：
 *   · bpftrace 直接拒绝：`is not traceable ... cannot attach kprobe`
 *   · `register_kprobe` 不检查该属性，能返回 0，但在其上强制 int3 会破坏执行
 *     —— 实测触发 9.26 亿次 / 10 分钟，load 5.95（reports/187）
 *
 * **硬件断点（DR0-DR3 + CPU 的 #DB 异常）走的是完全不同的机制，不受 notrace 约束。**
 *
 * ## 本模块能做什么
 *
 * 1. 纯观测模式（默认）：记录每次调用的 offset / value / 返回地址，不改任何东西
 *    → 用来回答"CUDA 启动时到底写没写 0x8C040、从哪一行代码调用"
 * 2. 改写模式（`patch=1`）：当 offset==0x8C040 且 SPEED==2 时，把要写的值改成 SPEED=1
 *    → 让驱动永远写不进 Gen1，且**不改变函数的控制流**
 *
 * 第 2 点如果成立，就能替代 kretprobe + 常驻守卫（后者需要不停按回 SPEED）。
 *
 * ## 用法
 *
 *   ADDR=$(sudo grep -w _nv053912rm /proc/kallsyms | awk '{print $1}')
 *   sudo insmod gen2hw.ko addr=0x$ADDR
 *   # 观测
 *   sudo dmesg -w | grep gen2hw
 *   # 改写
 *   sudo insmod gen2hw.ko addr=0x$ADDR patch=1
 *
 * 注意：x86 只有 4 个硬件断点，本模块只占 1 个。
 */
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/perf_event.h>
#include <linux/cpumask.h>
#include <linux/ptrace.h>
#include <linux/pci.h>
#include <linux/sched.h>

#define NV_VENDOR   0x10de
#define NV_DEV_P104 0x1b87

/* 这些是稳定的 ABI 数值（内核头文件里没暴露） */
#ifndef PERF_TYPE_BREAKPOINT
#define PERF_TYPE_BREAKPOINT  5
#endif
#ifndef HW_BREAKPOINT_X
#define HW_BREAKPOINT_X       4
#endif
#ifndef HW_BREAKPOINT_LEN_1
#define HW_BREAKPOINT_LEN_1   1
#endif

#define OFF_LINKCFG0 0x8C040
#define SPEED_MASK   0x000C0000u
#define SPEED_GEN2   1u
#define SPEED_GEN1   2u

static unsigned long addr;
module_param(addr, ulong, 0444);
MODULE_PARM_DESC(addr, "被观测函数的虚拟地址（从 /proc/kallsyms 取）");

static int patch = 0;
module_param(patch, int, 0644);
MODULE_PARM_DESC(patch, "1=把 0x8C040 的 SPEED=2 改写成 SPEED=1；0=只观测");

static int log_all = 0;
module_param(log_all, int, 0644);
MODULE_PARM_DESC(log_all, "1=打印所有调用（含非 0x8C040），调试用；0=只打印 0x8C040");

static int stop_zero = 0;
module_param(stop_zero, int, 0644);
MODULE_PARM_DESC(stop_zero, "1=只要 SPEED 已是目标值就停止计数（省日志）");

static struct perf_event *__percpu *hwbp;

static unsigned long n_total;      /* 总命中 */
static unsigned long n_lnkcfg;     /* offset==0x8C040 */
static unsigned long n_patched;    /* 实际改写次数 */
static unsigned long last_ret;     /* 最近一次 0x8C040 访问的返回地址 */
static u32 last_value;

/* 返回地址 → 模块内偏移（nvidia.ko 的 .text 基址） */
static unsigned long nvidia_text_base(void)
{
    /* /sys/module/nvidia/sections/.text 不方便在核里读；
     * 用 kallsyms 里任一个已导出符号反推更麻烦，这里只报原始返回地址，
     * 由用户态脚本按 .text 基址换算。
     */
    return 0;
}

static void hw_handler(struct perf_event *bp,
                       struct perf_sample_data *data,
                       struct pt_regs *regs)
{
    u32 offset = (u32)regs->cx;
    u32 value  = (u32)regs->r8;
    u32 speed;

    n_total++;

    if (offset != OFF_LINKCFG0) {
        if (log_all && (n_total % 1000 == 1))
            pr_info("gen2hw: [其它] rcx=%#x r8=%#x ret=%#lx comm=%s\n",
                    offset, value, regs->sp ? *(unsigned long *)regs->sp : 0UL,
                    current->comm);
        return;
    }

    n_lnkcfg++;
    last_value = value;
    last_ret = regs->sp ? *(unsigned long *)regs->sp : 0UL;
    speed = (value & SPEED_MASK) >> 18;

    if (!stop_zero || n_lnkcfg <= 20)
        pr_info("gen2hw: 写 0x8C040 值=%#010x SPEED=%u ret=%#lx comm=%s patch=%d\n",
                value, speed, last_ret, current->comm, patch);

    if (!patch)
        return;

    if (speed != SPEED_GEN1)
        return;                     /* 已经是目标值，不干预 */

    /* ★ 只改参数寄存器，不动控制流 */
    regs->r8 = (value & ~SPEED_MASK) | (SPEED_GEN2 << 18);
    n_patched++;
    pr_info_ratelimited("gen2hw: 改写 SPEED 2→1  %#x → %#lx（第 %lu 次）\n",
                        value, (unsigned long)regs->r8, n_patched);
}

/* 每个 CPU 一个 perf_event */
static struct perf_event *bp_per_cpu[NR_CPUS];

static int __init gen2hw_init(void)
{
    struct perf_event_attr attr = {0};
    int cpu, ok = 0;

    if (!addr) {
        pr_err("gen2hw: 必须提供 addr=<函数地址>\n");
        return -EINVAL;
    }
    if (addr < 0xffffffff80000000UL) {
        pr_err("gen2hw: addr=%#lx 不像内核地址\n", addr);
        return -EINVAL;
    }

    attr.type   = PERF_TYPE_BREAKPOINT;
    attr.size   = sizeof(struct perf_event_attr);
    attr.bp_type = HW_BREAKPOINT_X;
    attr.bp_addr = addr;
    attr.bp_len  = HW_BREAKPOINT_LEN_1;
    attr.sample_period = 1;
    attr.wakeup_events = 1;

    for_each_online_cpu(cpu) {
        struct perf_event *bp;

        bp = perf_event_create_kernel_counter(&attr, cpu, NULL,
                                              hw_handler, NULL);
        if (IS_ERR(bp)) {
            pr_err("gen2hw: CPU%d 注册失败 %ld\n", cpu, PTR_ERR(bp));
            continue;
        }
        bp_per_cpu[cpu] = bp;
        perf_event_enable(bp);
        ok++;
    }

    if (!ok) {
        pr_err("gen2hw: 所有 CPU 都注册失败\n");
        return -ENODEV;
    }

    pr_info("gen2hw: 硬件断点已设于 %#lx，成功 %d 个 CPU  patch=%d log_all=%d\n",
            addr, ok, patch, log_all);
    return 0;
}

static void __exit gen2hw_exit(void)
{
    int cpu;

    for_each_online_cpu(cpu) {
        if (bp_per_cpu[cpu]) {
            perf_event_disable(bp_per_cpu[cpu]);
            perf_event_release_kernel(bp_per_cpu[cpu]);
            bp_per_cpu[cpu] = NULL;
        }
    }
    pr_info("gen2hw: 已卸载。总命中 %lu，0x8C040 命中 %lu，改写 %lu，最后值=%#010x ret=%#lx\n",
            n_total, n_lnkcfg, n_patched, last_value, last_ret);
}

module_init(gen2hw_init);
module_exit(gen2hw_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Hardware-breakpoint observer/patcher for NVIDIA RM register access");
