# README.md 的 P104 Gen2 章节快照

> 从 `/data/labs/nvidia/README.md` 抽取（原文件 265493 字符）。

---

P104-100 PCIe Gen2 课题（reports 176-197）

| 报告 | 结论 |
|---|---|
| 176-178 | 三态判活（D3COLD/GONE/ZOMBIE）；`0xffffffff` 无鉴别力 |
| 184 | **kretprobe 挂 `nv_pci_probe` 成功**：Gen2 x4，pinned 1.56/1.71 GB/s，Xid 0 |
| 186 | 三条确定性致命操作（COMMIT / 端点 Retrain / 训练未完成时写 SPEED） |
| 191-192 | 锁定环定位 `_nv017281rm`；★ `perf script` 必须 `-F iregs`，否则寄存器永远读不到 |
| 193 | 补丁安全性定案（唯一入口/出口，落点只写 0x8C040） |
| 194 | ★「零引用≠死代码」：RM 67.5% 函数零入边，靠对象槽间接调用 |
| 195 | 补丁点确证执行（N ctx ⇒ N 次）；★ 但单独用无效，RM 已按 Gen1 初始化 |
| 196-197 | 守卫提速 60%（9.59s→3.84s）；★★ 用户态 mmap 写 0x8C040 **2/2 掉卡**，内核 writel 安全 |
| 198 | 机制终局：驱动在 `rm_init_adapter` 里**主动要求 Gen1**；`0x8872C` 驱动零引用；COMMIT 非毒药；状态污染只能重启 |
| **199** | **★★★ 突破**：2 字节补丁阻止降级，链路常驻 Gen2；353 轮长跑零掉速、1.56/1.71 GB/s |
| **200** | 部署特性：**补丁只"保持"、gen2kr 才"建立"**；装补丁驱动≠一劳永逸，重启须跑一次 20-install |
| **201** | **免重载推送**：`unbind/bind` 就够（触发同一个 `nv_pci_probe`），新增 `bin/push.sh`；修正"reload 会复位声明"的旧结论 |

**交付包**：`/data/labs/nvidia/nvidia-p104-unlock-gen2-for-linux/`（自包含，已从原厂状态完整复现）

**P104 最终方案（reports/199）**：
```bash
sudo python3 pcie-gen2/tools/patch_no_gen1.py check    # 自查补丁是否还在
sudo python3 pcie-gen2/tools/patch_no_gen1.py apply    # 驱动升级后需重新打
# 然后 modprobe nvidia → insmod gen2kr.ko → unbind/bind 一次
```

**P104 核心纪律（血泪，勿犯）**：
1. **判据只能是寄存器**：`LNKSTA[3:0]==2`。`nvidia-smi` 任何 PCIe 字段都是缓存，一律不用
2. 改 BAR0 链路控制寄存器**必须走内核路径**（用户态 mmap 在链路动态时 2/2 掉卡）
3. 读-改-写前双校验：`!= 0xffffffff` **且** `(lk & 1) == 0`（COMMIT 位）
4. **出现「CAP2 恒 0x0002 + TLS 恒 1 + 连续 push 失败」直接重启**，不要再试（状态污染）
5. 评价带宽必须 `512MB×15`；`128MB×3` 全落在恢复窗口内，只会测到 0.78
6. 远程 reboot 前查 `grub-editenv - list`，`recordfail=1` 会卡在 GRUB 等人工按键
7. NFS root_squash：编译用 union 身份，`insmod/rmmod` 才用 root
8. 不要挂 `nvitop`/任何轮询 `nvidia-smi` 的监控（Xid 79 打死过设备一次）
9. `perf script` 必须 `-F comm,iregs`，否则寄存器永远读不到（两次导出错误结论）
10. `grep 'Speed 5GT/s'` 会误匹配 `2.5GT/s` —— 用寄存器判


配套硬规则：
1. 跨版本数据只能来自**同一脚本的不同参数**，脚本本身入库（`analysis/probes/`），不接受现场手写。
2. 每次采样必须**声明相位**（LOADING / 场景），且用自动判定（窗口截图体积或 HUD `FPS:`），不接受 `sleep` 猜。
3. 测量前必须清零：`pkill -9 -x <app>; pkill -9 -x strace` + 确认 `nvidia-smi` 回到 `0%, ~6W, 139MHz`。
4. 数字分两类，永不混用：**结构**（栈、调用链、偏移）与**频次/时长**（每帧次数、µs）。`-k` 只能用于前者。
5. 任何"某线程/某段时间占比"的结论，必须写明**采样率**，并论证它能分辨的最小占空比。

