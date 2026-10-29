# STATUS.md 的 Gen2 相关章节快照

> 从 `/data/labs/nvidia/STATUS.md` 抽取（原文件 97185 字符），
> 共 14 个二级章节，按原顺序排列。

---

## ★★★★★★★★ 部署特性：补丁只"保持"、gen2kr 才"建立"（2026-10-29 10:32，报告 200）★★★★★★

### 「装个打过补丁的驱动就一劳永逸」——**实测否证**
带补丁重启（`md5=514de9d4`）、gen2kr 无任何自动加载配置：
```
LnkSta = Speed 2.5GT/s      ← 仍 Gen1
LCAP2  = 0x0002             ← 声明没解锁      TLS = 1
0x8841C= 0xc0b40500         ← 原厂默认
带宽   = pinned 0.779 / 0.855   ← Gen1 水平
```
补上 `20-install.sh` → Gen2 + **pinned 1.559 / 1.707**。

⇒ **补丁 + gen2kr 是"保持"和"建立"两件事，缺一不可。**
⇒ 每次重启后必须手动跑一次 `sudo bash bin/20-install.sh`（幂等，约 6 秒）。

两种丢失后果不同：补丁丢→链路反复跳；**gen2kr 丢→开机就 0.78**（最常见故障）。

### ★ `unbind/bind` 不会重置声明
解绑/绑定前后 `CAP2` 都 0x0006、链路都 Gen2 ⇒ 声明解锁**跨 unbind/bind 保持**。
复位只有：`rmmod nvidia && modprobe nvidia`（快，有锁死风险）或整机断电。
**不能拿 unbind/bind 当"复位到原厂"用。**

### 自动化三选项（当前 A：手动）
A 手动跑 20-install（**采用，无侵入**）｜B `softdep nvidia post: gen2kr` + gen2kr 加
init 推送（未实现未验证）｜C systemd service（用户明确不要）。

---

## ★★★★★★★★★★★★ 突破：2 字节补丁阻止降级，守卫可退休（2026-10-29 09:35，报告 199）★★★★★★★★★★

**思路：别让它降，而不是降了再救。**

### 先纠正 reports/195/197 的错误结论
「SPEED=2 是端点内部回写、CPU 不可见」**错**（那次断点没生效）。四点断点实证：
`entry(DX=1) → 0x127e65(AX 0x20505800) → 0x127edd(R8=0x20585800) → ret`
⇒ SPEED 完全由软件写入。

### 补丁：只改 2 字节，平移 mode 白名单边界
```asm
- 127d66: lea -0x1(%rdx),%eax   ; mode-1
- 127d81: cmp $0x2,%eax
+ 127d66: lea -0x2(%rdx),%eax   ; mode-2
+ 127d81: cmp $0x1,%eax
  127d84: ja 1280c8             ; 不动
```
`(mode-2)>1` 无符号：mode=1 下溢 → **bail**；2/3 继续；≥4 bail（与原逻辑等价）。
**唯一行为差异 = mode==1 从"执行"变"bail"。** bail 返回 `%r12d`=0=**CUDA_SUCCESS**。
md5 `7f6805ed…` → `514de9d4…`；备份 `.orig_nogen1`；工具 `tools/patch_no_gen1.py`。

### 决定性验证（摘掉守卫只留补丁）
3 轮 cuInit + 再等 10s：**CAP2=0x0006 LNKSTA=2 TLS=2 SPEED=1，一次没掉**。
无守卫长测 512MB×15：pinned H2D **1.558~1.564**、D2H **1.700~1.709**；测后仍 5GT/s。
**20 min 长跑：353 轮 / 353 成功 / 0 失败，平均 0.70s、最慢 0.83s，Xid 0。**
**守卫回归测试**：补丁+gen2kr 同载 → `guard 日志条数: 0`，守卫零动作，纯安全网。

### 改变
| | 旧（守卫） | 新（补丁） |
|---|---|---|
| 链路 | Gen1/Gen2 反复跳，3.8s 拉回 | **常驻 Gen2，根本不降** |
| 常驻模块 | 需要 | **不需要**（push 完可卸） |
| 测量敏感度 | 强（短测必踩坑） | 不敏感 |

### 遗留
二进制补丁（驱动升级会丢）；仍需声明解锁；**只针对 Pascal**（Ampere 的 SPEED 编码不同，不能照搬）。

---

## ★★★★★★★★★★★★ 机制终局：驱动主动要求 Gen1；状态污染只能重启（2026-10-29 09:05，报告 198）★★★★★★★★★★

### ★★★★★ `_nv017105rm` / `_nv017102rm` = 驱动的 `set_link_speed(mode)`
mode 1→`or $0x80000` SPEED=2 (**Gen1**)、2→`or $0x40000` SPEED=1 (**Gen2**)、3→SPEED=0 (**Gen3**)；
mode≥2 门禁 `obj[0x954]`(Gen2)/`obj[0x955]`(Gen3)，为 0 直接 bail。`_nv017102rm` 多 mode 4。
**动态 perf 4/4：每次 `cuInit` 都以 mode=1 调用它**，调用点在 **`rm_init_adapter+0xcd`**。
`_nv017755rm` 逻辑：`if (mode == 当前速率) return;` ⇒ 链路 Gen2 而 mode=1 ⇒ **主动降回 Gen1**。

### ★ 能力位来源
`_nv017279rm` = 五路能力按位与（直接调用 + `obj+0x1a8/0x1b0/0x1b8/0x1c0`）
→ `_nv017812rm(dev,obj,flags)`：`flags&0x04→obj[0x954]`。
**`obj[0x954]` 运行时值仍未拿到**（需冷启动首次 modprobe 时抓，reload 后已错过）。

### ★★★★ `0x8872C` 驱动零引用
`.text` 里 `mov $0x8872C` **0 处** ⇒ 我们写的 XVE_OVR 完全来自参考项目，
驱动从不碰它，时序不可预测是必然。

### ★★★★ 修正 reports/186：COMMIT 是驱动的正常提交步骤
`_nv017105rm+0x1ff`：`and $0xfffffff0` → `or $0x1,%r15d` → 写 `0x8C040`，前面还有回读轮询。
⇒ 「写 COMMIT 必死」是**误归因**，致命的是**缺前置步骤就写**。⚠ 外部仍不要写。

### ★★★★ registry 键无效
`RMPcieLinkSpeed` / `RMPcieStickyGenSpeed` / `RMPcieGenSwitchOnPmu` 在 `_nv017157rm` 里读，
`RMPcieLinkSpeed` → `obj[0xa38]`。**实测 `=2` 与 `StickyGenSpeed=1` 都挡不住降级。**

### ★★★★★ 状态污染只能重启
多次 reload + registry 实验后锁死：`LnkCap MaxSpeed=1` / `CAP2=0x0002` / `TLS=1`，
但 `0x8841C` 与 `0x8872C` 都写对了，`LNKSTA` 恒 1。
挽救全失败：unbind/bind ×6、重训 ×13、重写 TLS=2 ×20（下轮读回 1）、重解锁 ×9。
**重启后一次 push 成功** ⇒ **纪律：出现"CAP2 恒 0x0002 + TLS 恒 1 + 多次 push 失败"直接重启，不要再试。**

### ★★★ 稳定性（被污染实验打断）
72 轮降级-恢复：平均 **3.45s**、最慢 4.17s；pinned H2D 1.554~1.564、D2H 1.708~1.710 GB/s；Xid 0；无退化。

### $ 教训：又一次拿 nvidia-smi 当判据（被用户纠正）
`Device Max: 1` 是驱动缓存；同刻寄存器 `LnkCap=0x00454c42 MaxSpeed=2`、`LnkCap2=0x06`、
`LnkSta=0x1042`、`LnkCtl2=0x0002` 全 Gen2。**PCIe 判据只能寄存器。**

---

## ★★★★★★★★★★★★ 守卫提速 60% + 用户态写 BAR0 会打死设备（2026-10-29 08:50，报告 197）★★★★★★★★★★

### 结果
| 指标 | 前 | 后 |
|---|---|---|
| 降速恢复 | 9.59s | **3.29 / 4.22 / 4.00s（均 3.84s）** |
| pinned H2D | 1.554 | **1.564 ~ 1.566** GB/s |
| pinned D2H | 1.709 | **1.708 ~ 1.710** GB/s |
| Xid | 0 | 0 |

参数：`stage_gap_ms=150`（新增）、`debounce_cnt` 10→**3**、`speed_settle_ms` 3000→**800**、
`retrain_min_ms` 语义收窄为"两次完整尝试之间"。

### `speed_settle_ms` 扫描（`tools/sweep-settle.sh`）
3000→5.63s、1500→4.33s、**800→3.63s**、500→3.43s、300→3.22s，**五次全 OK 零掉卡**。
800 后收益递减（瓶颈=Retrain 生效 ~1.8s 物理延迟 + debounce）⇒ 取 800 留 2.6 倍余量。

### ★★★★★ 用户态 mmap 写 0x8C040 会打死设备，内核 writel 不会
| 路径 | 场景 | 结果 |
|---|---|---|
| 用户态 mmap | 链路刚降级（0.5s 内） | **2/2 掉卡** |
| 用户态 mmap | 驱动未加载 / 链路静止 / 等 ≥5s | 多次成功 |
| **内核 writel** | 链路刚降级（≤200ms） | **几十次按回、几十分钟，0 掉卡** |

读值合法（`0x20585800`，bit0=0）、校验通过仍掉卡；**dmesg 无 Xid**。
⇒ **改 BAR0 链路控制寄存器必须走内核路径。绝不用用户态脚本做"降级后立即按回"。**
`tools/probe-selfheal.sh` 已加硬拦截；只读排查改用 `tools/selfheal.py`。

### 新增环境坑
**NFS root_squash**：`/data/labs/nvidia` 属主是 union，`sudo make` 写不进去
⇒ **编译用 union 身份，insmod/rmmod 才用 root**。

---

## ★★★★★★★★★★★★ 守卫提速 41% + 挖出必掉卡的 COMMIT 缺陷（2026-10-29 08:35，报告 196）★★★★★★★★★★

### ★★★★★ 头号陷阱：「只要不写 bit0 就安全」是错的
读-改-写时从垃圾读值派生，会把 COMMIT 带进去：
```
lk = readl(0x8C040)          // 重训瞬态常读到 0xffffffff
writel((lk & ~0xC0000) | 0x40000 & ...) 
     0xFFFFFFFF & ~0xC0000 = 0xFFF3FFFF  |0x40000 = 0xFFF7FFFF
                                                        ↑ bit0=1 COMMIT → 必掉卡
```
实测代价：C. ZOMBIE，**dmesg 无任何 Xid**（硬件级静默失联）。
**铁律：写回前必须双校验 ① != 0xffffffff ② 所有"写 1 致死"位为 0。**
已修 5 处：`gen2kr.c` guard/push/ret_handler、`safe-gen2.py` ⑤、`probe-selfheal.sh`。
（旧 guard 只校验 CAP2，完全不校验 LINKCFG0。）

**语义坑**：`guard_enabled=0` **停不掉按回 SPEED**（按回在 ①，该检查在 ③）
⇒ 真要停守卫必须 `rmmod gen2kr`。

### ★★★★ 恢复耗时 9.59s → 5.65s（-41%）
`retrain_min_ms` 本意"两次完整尝试间隔"，却被用来卡 **stage0→stage1**，白等 2 秒。
新增 `stage_gap_ms`(150) 专管 stage 间隔；`debounce_cnt` 10→3；
`retrain_stage==1` 独立分支跳过防抖；链路恢复时重置 `retrain_stage=0`。
实测 stage 间隔 204ms。剩余 5.65s = `speed_settle_ms` **3.06s** + stage 0.2s + Retrain 生效 1.8s + 第2轮 1.84s。

**日志判读坑**：`根端口 Retrain（LNKSTA=1041）` 的 1041 是 **Retrain 之前**读的，
实测 Retrain 后约 **1.8s** 链路才变 ⇒ "日志全 1041 但最后成功"是正常的。

**降级后两端 TLS2 与 Gen2 时完全一致**（`0002`/`0042`/LCAP2 `0x0006`/SPEED=1）
⇒ 条件全满足却停 Gen1，必须显式重训。

### 状态
设备 C. ZOMBIE（`reset_subordinate` 已证无效），需整机重启；`recordfail` 已确认为空。
重启后第一件事：跑 `tools/probe-selfheal.sh` 验证 `speed_settle_ms=3000` 是否需要。

---

## ★★★★★★★★★★★★ 动态闭环：补丁确证执行、精确生效，但单独无效（2026-10-29 09:xx，报告 195）★★★★★★★★★★

### ★ 推翻 reports/191 的假结论（工具误用铁律）
`perf script` **默认不输出寄存器**，必须 `-F comm,iregs`。少了它 grep 寄存器永远 0 次。
reports/191「`_nv053912rm` 的 CX 里 0x8C040 出现 0 次」**作废**：
重测 80375 次调用，`CX==0x8c040` 命中 **1 次**，R8=`0x20085800`（1 ctx 写 1 次，吻合）。

### ★ 补丁点确证执行（`_nv017281rm` notrace，只能 perf 执行断点）
空闲 20s **0 次**；1 ctx→1 次；5 ctx→**5 次**；`0x127000`(or $0x80000) 分支端点 **5/5** 且 `DX=0x1`(=sp=1)。
完整链：`ctx创建 → _nv017281rm → 读 0x8C040/0x88088(DX=1) → 0x127000 or 0x80000 → _nv053912rm(CX=0x8c040,R8=0x20085800) → SPEED=2 锁 Gen1`

### ★ 补丁精确生效
`0x127043: 0x08→0x04` → `or $0x40000`；写入值 `0x20085800`(SPEED=2) → **`0x20545800`(SPEED=1)**，其余位未动。

### ★★★★★ 但单独补丁无效
SPEED=1 + 声明解锁 + 根端口重训 ⇒ 链路确实升 Gen2（5GT/s 无 downgraded）。
**但一跑 DMA 立刻降级，带宽 0.776/0.852 = Gen1 水平**，无 Xid。
RM 已按 Gen1 完成内部数据面初始化 ⇒ **唯一有效时机仍是 `nv_pci_probe` 那一瞬（gen2kr）**。

### ★★★★★ 补丁已回滚；gen2kr 重编后仍有效（本节结论）
- `gen2kr.ko` 原为 6.1.0-50 编译，**6.12 上必须重编**：`cd kmod && make`（gcc 14.2，无需 CC=gcc-12）
- 重编后 `insmod` + `unbind/bind`：`★ SUCCESS Gen2 LNKSTA=1042（第 1 次）`
- **长测 `512MB×15`：pinned H2D 1.554 / D2H 1.709 GB/s**（复现 reports/184 的 1.560/1.699）
- **短测 `128MB×3` 只有 0.778**：bwtest2 一建上下文端点即降级，守卫要 **~20s** 才拉回，
  短测全程落在恢复窗口 ⇒ **必须 512MB×15 以上，或链路就绪后立刻起测**
- `[push] lnkcfg0=20545800`：**gen2kr 自己就写 SPEED=1**，补丁在这一层冗余
- 补丁：单独无效 / 合 gen2kr 冗余 / 是否有害证据不足（各 1 次），**已回滚（md5 7f6805ed…）**

### ★ 不是"第二个写入者"
Gen2 起点采样 320 万次，抓到 **0.090s 同一瞬间** `0x8C040 SPEED1→2` 且 `LNKSTA 2→1`。
排查：`_nv017105rm` 执行 1 次但内部两处 `or $0x80000` 各 0 次；单轮 minictx 中
`CX=0x8c040` 只 1 次且值恒为 SPEED=1，终值 SPEED 仍为 1。
⇒ **SPEED 变 2 是端点内部（硬件/GSP）在降级时回写，CPU 不可见，软件拦不了。**
⇒ 不要再找"第二个写 SPEED 的软件函数"。

### ★ gen2kr 需重编
`gen2kr.ko` 是旧内核（6.1.0-50）编译的，6.12.113 上不能直接 insmod。
`cd pcie-gen2/kmod && make -C /lib/modules/$(uname -r)/build M=$PWD`（内核 gcc-14 编，本机也是 14.2，不需要 CC=gcc-12）

### 新工具
`probe-lock.sh` `probe-run.sh` `probe-cuda.sh` `probe-loop.sh` `probe-writer.sh`
`probe-one-ctx.sh` `probe-multi.sh` `probe-patch2.sh` `probe-who-writes.sh`
`trace-downgrade.sh` `unlock-decl.py` `../minictx.c`（零 DMA）

---

## ★★★★★★★★★★★★ 补丁安全性定案 + 三种偏移混淆（2026-10-29 08:25，报告 193）★★★★★★★★★★

### 定案：patch 影响范围严格限于 0x8C040
```
.text+0x126c87  cmp edx,0x1
.text+0x126c8a  je  0x127000        ← 唯一入口（全量扫描确认 1 处）
.text+0x127000  or  eax,0x80000     ← ★ patch 点（改 0x40000 = SPEED=1）
.text+0x127008  jmp 0x126ca1        ← 唯一出口（确认 1 处）
.text+0x126ca1  mov ecx,0x8c040 → call write → pop/ret   ← 只写这一个寄存器
```
**两个"唯一"就是安全性依据。**

### ★★ 三种偏移差 0x40（我两次搞错）
`文件偏移 = .text 内偏移 + 0x40`（`.text` sh_offset=0x40）

- 错误1：拿 `nv-kernel.o` 的节内偏移查 `nvidia.ko` 的 .text → 反汇编出占位桩
  （两文件 .text 布局不同：15,333,236 vs 15,198,308）
- 错误2：拿文件偏移 `0x127040` 查 .text 提取 → 看到日志代码，差点误判
- 工具 `tools/resolve_speedlock.py` 一次输出三种偏移 + 交叉校验

### 已知偏移对照（580.178.04）
```
签名 anchor  file=0x126cc7  .text+0x126c87
目标指令     file=0x127040  .text+0x127000  运行时 0xffffffffc1cb1000
待改字节     file=0x127043: 0x08 → 0x04
```

### ★ 最大不确定点：该分支是否真的被执行
该函数是"链路异常恢复"，正常不跑。**需先验证命中，再谈 patch 效果。**
重启后：perf 断点 `_nv017281rm` → 跑 CUDA；无命中则先人为制造一次掉速。

### patch 前提
1. **声明须已解锁**（`LCAP2=0x06`）2. 驱动重载

---

## ★★★★★★★★★★★★ 守卫三处修正 + pinned 稳定 1.560 + 恢复周期 10 秒（2026-10-29 07:15，报告 187/188）★★★★★★★★★★

### 先纠正上一轮的错误结论：不是宕机，是 S3 睡眠
`uptime 5:25` 从未重启；`01:20:29 systemd-logind: The system will suspend now!`
⇒ **"守卫打挂整机"撤回**。已 `systemctl mask sleep.target suspend.target
hibernate.target hybrid-sleep.target`（可 unmask 恢复）。
**方法论**：网络不可达 ≠ 宕机。查 `uptime` / `journalctl --list-boots` /
`journalctl -b | grep "PM: suspend"` 三条即可分辨。

### ★★ kprobe 拦截写函数：已证否（notrace 陷阱）
静态分析 + 重定位精确定位驱动 BAR0 写封装 `_nv053912rm`（.text+0x5ff6c0，
kallsyms 地址完全吻合），kprobe 它把 SPEED=2 改成 1。

**实测灾难**：`总调用 926,009,787，拦截改写 925,622,205 次`（≈154 万次/秒），
`load average 1 → 5.95`。卸载后立即恢复。

**根因**：该函数是 **`notrace`**。bpftrace 明确拒绝（`is not traceable`），
但 **`register_kprobe` 不检查此属性**，硬挂破坏执行（内部疑似"写+读回校验+重试"）。
`gen2kp.c` 已加警告头，仅作方法论记录。

**反证**：mmap 采样 166 万次 / 2 秒，`0x8C040` **0 变化** ⇒ 驱动稳态下根本不写它。

### ★ 守卫三处修正
| 缺陷 | 现象 | 修正 |
|---|---|---|
| GPU 忙时跳过重训 | pinned 0.777 | `allow_busy_retrain=1` |
| 重训缺 `0x8872C=6` | 重训 16 次全无效 | 两阶段：触发训练 → 等 → 根端口 Retrain |
| 按回 SPEED 后立即重训 | 无效（违反铁律 #1） | `speed_settle_ms=3000` |

### 最终实测（链路就绪，512MB×15）
```
pinned   H2D med 1.560  max 1.566   D2H med 1.699  max 1.709 GB/s
pageable H2D med 0.779  max 1.547   ← CUDA bounce buffer 限制，非链路问题
Xid 0
```

### ★ 关键特性：新 CUDA 上下文把链路打回 Gen1，恢复约 10 秒
```
新上下文 → 驱动写 SPEED=2 → 链路降 Gen1 → 守卫(0.6s)按回 SPEED
  → 等 3s → 0x8872C=6 → 等 2s → 根端口 Retrain → 恢复
```
⇒ 长任务 1.56；**每轮新进程、间隔 5–6s 则总测到 0.78**（总赶上恢复中）。
⇒ 工程上需"等链路就绪再计时"，或把首次 CUDA 初始化当预热。

### 待做
1. **≥30 min 长跑**（睡眠已 mask，可以做了）
2. systemd 服务化（开机自动 insmod + bind）
3. 缩短恢复周期（能否免掉 speed_settle 的 3 秒）
4. 辅助脚本：应用启动时主动等链路就绪

### 当前可用形态
```bash
modprobe nvidia
insmod kmod/gen2kr.ko allow_busy_retrain=1 debounce_cnt=3 retrain_min_ms=2000 speed_settle_ms=3000
echo 0000:01:00.0 > /sys/bus/pci/drivers/nvidia/unbind && sleep 2
echo 0000:01:00.0 > /sys/bus/pci/drivers/nvidia/bind
```

---

## ★★★★★★★★★★★★ ★★★ 静态定位驱动机制 + 补丁证否 + ★主机宕机（2026-10-29 02:00，报告 185）★★★★★★★★★★

### 静态分析：驱动按「当前速率」回写 SPEED（自我强化锁定环）

`.text+0x126c4e` 例程：读 `0x8C040` → 读 `0x88088` 取 LNKSTA → 清 SPEED[19:18] → 回写

| 当前 LNKSTA | 写入 SPEED | Pascal 解码 | 结果 |
|---|---|---|---|
| sp=1 | `2` | 2.5GT | 锁死 Gen1 |
| sp=2 | `1` | 5GT | 保持 Gen2 |
| sp=3 | `0` | 8GT | 保持 Gen3 |

**不是主动钳制，是"照抄当前值"。** 冷启动链路 Gen1 → 它写 SPEED=2 → 永远 Gen1。
这就是为何「probe 时机」有效：它跑之前链路已是 Gen2，它自己就写 SPEED=1。

**方法论修正**：`.ko` 是 ET_REL（无 PT_LOAD，须用 section header）；
NVIDIA 访问 BAR0 是**把偏移当函数参数**（`mov $0x8c040,%ecx` + call），
不是 `[disp32+reg]`。搜 displacement 会 0 命中。

### 1 字节补丁：打了，无效，已回滚

补丁：文件偏移 `0x127043` 的 `0x08→0x04`（`or eax,0x80000` → `or eax,0x40000`）。
工具 `tools/patch_nvko.py`（签名唯一命中、自动 .orig 备份、check/apply/revert）。

**A/B 对照（空闲20s + nvidia-smi×3，统计 SPEED 被改回次数）**：
| 组 | 空闲回压 | smi×3 后回压 |
|---|---|---|
| 打补丁 | 0 | **3** |
| 原始驱动 | 0 | **3** |

**完全相同 ⇒ 补丁无效。** 根因：`.text` 里 **38 处** `or eax/r8d,0x80000`（SPEED=2），
逻辑被大量内联复制；且 `or reg,0x80000` 在别的寄存器上下文可能合法，批量改风险高。
⇒ **二进制补丁路线证否，kretprobe + 守卫才是正解。已回滚（md5 恢复 `7f6805ed…`）。**

### 带宽三次独立复现
pinned H2D **1.555 / 1.561 / 1.565**，D2H **1.707 / 1.709 / 1.709** GB/s。
pageable H2D 0.776~1.214 波动 ⇒ **真实应用必须 pinned / cudaMemcpyAsync**。
空闲 20s 守卫零动作（按回 0、重训 0），开销集中在有配置访问时；
每次 `nvidia-smi`（读配置空间）触发 1 次回压 + 2 次补重训。

### ★★★★★ 安全红线：主机宕机，需物理重启

**union 机器整机失联**（`No route to host`，ping 100% loss，>3 分钟未恢复）。
宕机前：常驻守卫（100ms 周期，Gen1 时每 5 轮补 Retrain）+ 连续 bwtest + 3 分钟 setpci 循环。

**最可能原因**：**在 GPU 正在 DMA 时反复发起链路重训**。实测每次配置空间访问都会让
LNKSTA 瞬时读到 1 → 守卫误判掉线 → 疯狂重训 → 中断在途 TLP → PCIe 树异常 → 整机锁死。
（C. ZOMBIE 是轻症，这次是重症。）

**当前版守卫是危险的。改好之前禁止在有实际计算任务的机器上跑。必须加：**
1. **防抖**：连续 10×100ms 都读到 Gen1 才重训
2. **CUDA 活跃检测**：有进程持有 `/dev/nvidia*` 时跳过重训
3. **重训频率上限**：最快 2 秒一次

**下一步顺序**：物理重启 → 改安全守卫 → 重做 ≥30 分钟长跑 → 再 systemd 服务化。

---

## ★★★★★★★★★★★★ ★★★ 成功：Gen2 达成，带宽翻倍，零 Xid（2026-10-29 01:35，报告 184）★★★★★★★★★★

**用户提出的 kprobe 思路完全成功。P104-100 + Linux 闭源 580.178.04 达成 PCIe Gen2 x4。**

```
端点 LNKSTA = 0x1042；lspci: Speed 5GT/s, Width x4
pinned  H2D 1.562 GB/s (原 0.780)   D2H 1.706 GB/s (原 0.855)     ≈ 2.0×
pageable H2D 0.776 不变（CUDA bounce buffer，非链路问题）
Xid = 0；设备存活；守卫可自愈
```

### 方案：`pcie-gen2/kmod/gen2kr.c`（已编译 389 KB）
kretprobe 挂 `nv_pci_probe`，返回瞬间把链路推到 Gen2。

**四个关键设计（每个都踩过坑）**
1. **handler 不能睡眠**：kretprobe 是原子上下文，`msleep`/`ioremap` →
   `BUG: scheduling while atomic`（第一次实测）
2. **但也不能全丢给 workqueue**：晚几个 tick 就错过窗口（实测失败）。
   **正解：init 时预映射 BAR0，handler 里只 `writel` SPEED=1 + XVE_OVR，零延迟**
3. **守卫必需（常驻，每 100ms）**：驱动会**持续把 `0x8C040` 写回 SPEED=2**
   （实测 20 秒内 7 次）。守卫 ①SPEED≠1 就按回 ②LNKSTA 仍 Gen1 就补根端口 Retrain
4. **必须先 `modprobe nvidia` 再 `insmod`**：`nv_pci_probe` 是模块局部符号，
   驱动未加载时不存在，`register_kretprobe` 返回 -2。用 `unbind → bind` 触发 probe

### 三步判断全部被实测证实（用户的推理）
| 用户的判断 | 验证 |
|---|---|
| "不是硬件限制 TLS" | ✅ 驱动不在时写 TLS=2，5 轮重训**回滚 0/5** |
| "是 nvidia.ko 主动改回去" | ✅ probe 前后对比：只改 `0x8C040.SPEED`(1→2)，不碰 TLS |
| "不需要 SEC2 booter" | ✅ 参考 patch `pcie-gen2-probe-retrain.patch` 全用普通 `writel` |

### 为什么之前 20 多次都败
| 之前 | 现在 |
|---|---|
| 驱动跑起来很久后才改链路 | **probe 刚返回的瞬间改** |
| 改完不管 | 常驻守卫按回 + 补 Retrain |
| 在驱动不在时改（probe 会失败） | 驱动正常 probe，只在末尾改 |
| 以为要 SEC2 booter | 普通 writel 就够 |

**核心是时机**：必须让 RM 在"链路已是 Gen2"时完成内部数据面初始化。
RM 若已在 Gen1 下跑起来，事后再改 → 内部错配 → Xid 79。

### 操作步骤
```bash
modprobe nvidia
insmod /data/labs/nvidia/pcie-gen2/kmod/gen2kr.ko     # 默认 guard_rounds=0 常驻
echo 0000:01:00.0 > /sys/bus/pci/drivers/nvidia/unbind && sleep 2
echo 0000:01:00.0 > /sys/bus/pci/drivers/nvidia/bind
```

### 遗留
守卫停止则掉回 Gen1 → 需做 systemd 服务；未做长期稳定性测试；
pageable 带宽不变，真实应用需 pinned/async；当前只支持单卡。

---

## ★★★★★★★★★★★★ PCIe 课题转向：解锁已通，卡在"Gen2 承载不了流量"（2026-10-29 01:00，报告 179/180）★★★★★★★★★★


### ★★★ 决定性结果：Gen2 协商成功，但一跑真实 DMA 就 Xid 79 掉线

```
[第1轮] 端点 sp=2 | 根端口 sp=2 | 0x8C040=0x20045800      ← Gen2 达成，一轮
=== 立刻测带宽 ===
测前 sp=2  测后 sp=None  耗时 60.1s   cuInit=101
```
dmesg：
```
NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.
NVRM: GPU 0000:01:00.0: RmInitAdapter failed! (0x25:0xf:1636)
NVRM: GPU 0000:01:00.0: RmInitAdapter failed! (0x22:0x56:897)
```
**零 AER 错误**（无 Correctable/Bad TLP/Bad DLLP）⇒ 不是信号完整性问题，
更像「链路层到位 Gen2，GPU 内部数据面仍在 Gen1」—— 主机侧发起的 Retrain 不同步 GPU 数据面。

**⇒ 897/1636 归属定案：它们是 Xid 79 之后的善后错误，与速率无关。看到 897 先查 Xid 79。**

### ★★★ 三个"复位候选"全部实测（用户提问）

| 方法 | 结果 |
|---|---|
| `unbind → echo 0/1 > enable → bind` | ❌ `echo 0 > enable` 报**输入/输出错误**，内核拒绝 |
| `power/control` + runtime PM | ❌ `auto` 后 `runtime_status` 恒 `active`，不挂起 |
| `setpci` PMCSR → D3hot → D0 | ⚠ **安全但零效果**：PMCSR 写入成功、设备完好，6 个 BAR0 寄存器**逐字节不变** |
| `unbind → remove → rescan`（对照） | ✅ 可用，但**同样没复位核**（寄存器不变），只是拉回 D0 |

**⇒ 没有任何方法能复位 PCIe 核，而复位根本不需要。** `LINK_CAP2 0x02→0x06`
只是 `0x8841C` 几位的镜像。参考项目列为"决定成败的第 2 步"，是因为 **Windows 驱动会压回去**；
Linux 580.178.04 不碰这些位。
**⇒ 用户推测「D3hot ≈ DICS_DISABLE」不成立**：后者是总线级移除，前者只是电源态标记。

### ★★★ `0x8C040.SPEED` 是必需的（否证"纯标准路径"）

`retrain-only.py`：声明 `LCAP2=0x06` + 两端 TLS=2 + 根端口重训 6 轮 → **仍升不到 Gen2**。
所以 `0x8C040.SPEED=1` 不能省。（此轮全程无掉卡，证明其余四步都安全。）

### 现配方（去掉全部多余步骤）

```
① 声明解锁（安全）：0x8841C 置15/13/11清12/14（读值再OR，勿写绝对值）
                    0x88610 清12置0；0x8872C=6    ⇒ LCAP2 0x02→0x06 当场生效
② 根端口 LNKCTL2[3:0]=2（端点本就 2）
③ 0x8C040.SPEED[19:18]=1，**绝不发 bit0 COMMIT**；写完等 ≥3s
④ 只从根端口写 LNKCTL bit5 重训
```
不需要 remove / rescan / unbind-bind / D3hot / COMMIT / 常驻按住（60s・1700 采样回压 0 次）。

### 操作禁忌（每条都真实打死过设备）

| 操作 | 后果 |
|---|---|
| 写 `0x8C040.bit0`(COMMIT) | 必掉卡 |
| 写**端点** `0x88088` Retrain 位 | 打死端点 |
| 写完 SPEED 毫秒级立刻重训 | 打死端点（须等 ≥3s） |
| 往 `0x8841C` 写绝对值 `0xC0B4AD00` | 打死端点（必须读值再 OR） |
| **Gen2 下跑真实 DMA** | **Xid 79 掉线** |

### 下一步三条岔路
- **A** 让 GPU 自己发起重训（不写 COMMIT 的前提下）。候选 `0x8C040.bit22`（驱动置过）。
  逐位走查风险高（参考项目记录过会掉卡）
- **B** 接受本机 Gen1 x4 = 0.78 GB/s 封顶，转回 P106 每批延迟课题
- **C** 换直插 x16 槽 / 换转接卡，区分"卡不行"还是"这台机器链路不行"

### 带宽基线（`bwtest`，dlopen libcuda）
Gen1 x4：H2D **0.774–0.779** / D2H **0.851–0.854** GB/s。Gen2 目标 ~1.55 GB/s（**从未测到**）

### 产出
`gen2-bw.py`（抓到 Xid 79 的关键脚本）、`pci-state.sh`、`bar0peek.py`、
`retrain-only.py`、`speed-once.py`、`hold.py`、`test-reset-methods.py`、`gen2-v2.py`

---

## ★★★★★★★★★★★★ PCIe Gen2 课题（2026-10-29 00:20，报告 176/177/178）★★★★★★★★★★


### ★★★ 因果已切干净：三个寄存器各管一件事

| 目标 | 需要什么 | 证据 |
|---|---|---|
| 声明解锁 `LCAP2 0x02→0x06` | `0x8841C`(置15/13/11,清12/14) + `0x88610`(清12,置0) + `0x8872C=6` | 第1步做完**当场**就是 `0x06` |
| 链路升 Gen2 `LNKSTA=0x1042` | 只需 `0x8C040.SPEED[19:18]=1`（Pascal 编码 1=5GT/s） | C/E 两组独立复现 |
| **掉卡** | `0x8C040.bit0`(**COMMIT**) | 四次实验，写 COMMIT 必掉 |

**COMMIT 是纯粹的毒药，永远不要写。** 参考项目之所以要它，是因为 Windows 驱动会回压
（必须抢在驱动前强制提交），而 Linux 580.178.04 **根本不写 `0x8C040`**。

### ★★★ 已否证的参考项目结论（P104-Gen2x4 v1.6）

- ❌ "必须发 COMMIT 才能提速" → 只写 SPEED 就够，硬件自己重训
- ❌ "复位后要等 25 秒声明才锁存" → 4 秒内锁存，甚至第 1 步做完当场就有
- ❌ "必须常驻按住否则掉回 Gen1" → 按住 60 s / **1700 次采样 100%**、**驱动回压 0 次**
  ⇒ **Linux 上不需要看门狗/常驻进程**（这是本次最大的工程收益）
- ⚠ `nvidia-smi` 的 `pcie.link.gen.*` 读驱动缓存，**不可当判据**（寄存器已 Gen2 它仍报 1）

### ★★★ 掉卡三种状态必须分开（新工具 `pcie-gen2/pci-state.sh`）

`0xffffffff` 与 "`enable=1` 拉不回 D0" 在三态里都出现，**无鉴别力**。

| 状态 | `lspci` | `power_state` | `cfg 0x00` | `CAP_EXP` | 可救 |
|---|---|---|---|---|---|
| A. D3COLD（没上电） | 在 | **`D3cold`** | ffffffff | **能解析** | ✅ unbind→remove→rescan |
| B. GONE（链路断） | **不在** | — | No devices | — | ❌ 重启 |
| C. ZOMBIE（端点死锁） | 在 | **`D0`** | ffffffff | **not found** | ❌ 重启 |

- **A 的关键**：remove 前**必须先 unbind**，否则驱动把设备留在 D3cold 拉不回
- **C 最阴**：`power_state=D0` 是内核软件状态，**不证明硬件上电**；
  `lspci` 也会返回"存在" ⇒ **`alive()` 只查 lspci 会把半死设备当活的**（已踩）
- 判活必须双查：配置空间 + BAR0 镜像 `0x880A4`；`0xffffffff` 要**连续 20 次**才算失效

### 带宽基线（`pcie-gen2/bwtest`，dlopen libcuda，无需 CUDA toolkit）

Gen1 x4：H2D **0.774** / D2H **0.851** GB/s（与参考项目 0.78/0.85 吻合）
目标 Gen2 x4 ≈ 1.55 GB/s

### 遗留（未解决）

1. **写 `SPEED=1` 有概率掉卡**：E 组成，F/V3 败，前置状态逐字节相同。
   已排除"根端口 TLS 不一致"假设（V3 实验）
2. **驱动在 Gen2 下共存未验证** —— 最后自己 `rmmod/modprobe` 把唯一机会浪费了。
   897/1636 很可能不是速率校验失败，而是**设备半死时 RM 初始化超时**的通用错误
3. **流程教训**：`alive()` 判据曾骗过流程检查，带着半死设备走 remove，最终彻底掉线

### 产出

- `gen2-recipe.py`（旧，含 COMMIT，禁用）
- **`gen2-v2.py`**（现行）：`--mode=nocommit|speedonly|latespeed|latecommit|full`、
  `--no-unbind`、`--reload`、安全网 `mmio_alive()`（GRACE=20）、状态 dump `state-*.json`
- `bwtest.c` / `bwtest`：真带宽实测
- `pci-state.sh`：三态判别探针
- `watch-cfg.py`：只读观察 `0x8841C` 位变化

---

## ★★★★★★★★★★★★★ 零之前（2026-10-28 10:40，报告 171）★★★★★★★ 补丁已固化到文件（两字节 + 回滚）

### 改动
| 偏移 | 原 | 新 | 含义 |
|---|---|---|---|
| `0x15c375e` | `0x74`(je) | **`0xeb`(jmp)** | 无条件跳过发射点1 |
| `0x1841fe0` | `0x75`(jne) | **`0xeb`(jmp)** | 无条件跳过发射点2 |

目标：`/usr/lib/x86_64-linux-gnu/libnvidia-glcore.so.525.116.03`
原 md5 `276992071408f0828704f12827c85ae1` → 新 `84bff6eb3940df59ee607dc117223334`
**备份：同目录 `.orig`（md5 = 原值）**

`74/75/eb` 都是 2 字节 rel8 短跳转，**长度一致可原地替换**（换 `e9` 会破坏布局，绝不能干）。

### 验证（全部**无任何 LD_PRELOAD**）
- 磁盘：两字节均为 `0xeb` ✓
- **进程内存**（起 GL 程序读 `/proc/pid/mem`）：两字节均 `0xeb` ✓ **（这步不能省，磁盘改了≠进程用了新文件）**
- 性能 `n83b -M 7 -c 400 -n 400`：**map_p50 3.641ms / fps 239.67 / chk 5150**（原版 52.276/18.96/5150）
- 命令流：`method 0x3998` **样本 0 条、活跃段命中 0** ⇒ 那条 240 命令完全消失 ✓
- Heaven：91~95% 利用率、93~107W、31 线程、CPU 246%、画面持续推进（两次截图 md5 不同）= 正常渲染
  ⚠ **未取到 Heaven 屏幕 FPS**：`-benchmark` 模式**隐藏 FPS 叠层**且日志从不写 FPS；26 场景 1080p 全高画质要跑 10min+，本次未等完。**Heaven 只有功耗证据，精确帧率以 n83b 为准。**

### 工具 `tools/patch_glcore.py`（apply / revert / check）
设计要点：**改写前逐字节核对期望原值**（不匹配即中止）、**备份只在首次创建**（否则重复 apply 会把已打补丁的文件当备份）、**原子写临时文件+os.replace**（防中断损坏 .so 导致起不来）、**check 区分未打/已打/未知**（未知=位置漂移告警）。

### 回滚
```
sudo python3 tools/patch_glcore.py revert /usr/lib/x86_64-linux-gnu/libnvidia-glcore.so.525.116.03
```
回滚后需重启使用该库的进程。**重装驱动会覆盖该文件**（补丁自动失效）—— 既是升级需重打的麻烦，也是出问题重装即恢复的安全网。

### 性质（必须说清）
**解决**：不再需要 LD_PRELOAD；任何程序（含 primusrun 启动的）自动生效；绕过"Unigine 用 dlopen 钩子拦不到"的问题。
**不解决**：驱动升级即失效（偏移随版本变）；重装同版本被覆盖；`dpkg -V` 会报该文件被改；**未做长时间稳定性测试**；`+0xc18` 还被其他 4 处引用，未逐一验证。
**它不是"修好了驱动"** —— 是在别人的二进制里跳过一段逻辑，属于逆向研究的固化，不是驱动修复。

### 换版本时的偏移找法
1. 搜 `66 0e 01 20 f0 00 00 00`（`movabs 0x...20010E66`）→ 发射点
2. 发射点前应见 `80 b8 18 0c 00 00 00`（`cmp byte [rax+0xc18],0`）
3. 紧随的条件跳转 `74 xx`/`75 xx` 即补丁位置
4. **改 `74/75` → `eb`，只改首字节**

---

## ★★★★★★★★★★★★ 零之前（2026-10-28 09:40，报告 170）★★★★★★★★ ★ 静态根因 + 治本补丁：525 完全追平 515

### ★★★ 治本补丁（2 字节）实测：525 = 515
| 配置 | map_p50 | fps | **chk** |
|---|---|---|---|
| **525 基线** | **52.276 ms** | **18.96** | 5150 |
| **525 治本（跳过发射）** | **3.448 ms** | **256.56** | **5150** |
| 525 治标（实参 240→16） | 7.016 ms | ~133 | 5150 |
| **515 参考** | **≈3.4 ms** | **≈258** | 5150 |

**治本后 525 完全追平 515（3.448 vs ≈3.4 ms / 256.6 vs ≈258 fps），chk 一致。**
**⇒ 真根因不是"240 太大"，而是"这条命令本不该发"。**

### 静态根因（报告 170）
| 项 | 515 | 525 |
|---|---|---|
| `movabs 0x...20010E66`（那条 240 命令） | **0 处** | **2 处** |
| `cmp byte [rax+0xc18],0`（策略位检查） | **0 处** | **4 处** |
| 发射函数 `0x15c2e50` 机器码 | **找不到** | 存在（11856 B） |

**⇒ 515 不是"延迟更小"，是整套机制不存在。**

两个发射点：`glcore+0x15c3767`（在 `0x15c2e50`，即报告155 的 legacy 状态机内）、
`glcore+0x1842131`（在 `0x1841fb0`）。**两者检查同一个全局策略位 `+0xc18`。**
置位逻辑在 `0x15bc880`：`r8d` 不为 0x200/0x201/0x204 时置位；调用点 `0x15cecee` 传 `xor r8d,r8d`（=0）⇒ **必然置位**。

### ★ 关键方法论：关标志位要从"读侧"下手，不要从"写侧"
| 尝试 | 结果 |
|---|---|
| 改 `0x15bd01a`（`mov [rax+0xc18],1` 的 imm8） | **完全无效** |
| 再补 `0x1452699`（`mov [rbp+0xc18],1` 的 imm8） | **仍然完全无效** |

原因：`+0xc18` 有 48 处引用，除立即数写入外还有 `mov [reg+0xc18], <寄存器>`（6 处）及可能的 memcpy/批量填充。
**⇒ 改走"读侧"：把两个发射点的 `je/jne` 改成 `jmp`（`74→eb`、`75→eb`），不依赖标志位任何来路。**

### Heaven 侧
治本后 **90~94% 利用率、94~108 W**（base 是 100%/55~70 W）。**功耗接近翻倍 = GPU 真在渲染。**
⚠ 本轮 Heaven **未取到屏幕 FPS**（非 benchmark 场景叠层不在可见区，全图扫描未找到）⇒ Heaven 只有功耗证据，**精确帧率以 n83b 数据为准，不硬凑数字**。

### 遗留
1. **仍是运行时补丁**（改内存代码），驱动升级即失效；未做长时间稳定性测试。
2. `+0xc18` 的完整来路未查清（48 处引用里未逐一分辨）。
3. `r8d` 语义（0x200/0x201/0x204 分别代表什么）未知。
4. **不能据此认为"525 可修"就已解决** —— 这是研究结论，不是交付方案。

---

