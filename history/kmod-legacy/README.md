# 早期实验版本（不参与编译）

这两个是找对方案之前的尝试，保留作历史参考。

| 文件 | 做法 | 结论 |
|---|---|---|
| `gen2kp.c` | kprobe 挂在 RM 的写 SPEED 函数上 | ✗ 那些函数是 **notrace**，`register_kprobe` 被拒；且 `register_kprobe` 不检查 notrace 属性，一度造成 9 亿次调用、load 5.95 |
| `gen2hw.c` | perf 硬件断点（`register_wide_hw_breakpoint`） | 技术上可行，但 x86 只有 4 个调试寄存器，能同时观测的点太少 |

最终方案是 `../gen2kr.c`：kretprobe 挂 `nv_pci_probe`（这个函数**是**可插桩的），
在 probe 返回瞬间推送，配合 2 字节补丁保持链路。

## 想重新编译它们
```bash
obj-m := gen2kr.o gen2kp.o gen2hw.o    # 改 kmod/Makefile 的 obj-m
```
