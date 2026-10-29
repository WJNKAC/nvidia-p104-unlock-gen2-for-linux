# 194 / N205 — 静态调用图推翻三条旧结论；ZOMBIE 软件恢复尝试失败

日期：2026-10-29
状态：设备已整机重启中（停在 GRUB，等人工按键）

---

## 0. 一句话

用**全量重定位调用图**打了三记校准，发现"零引用 ⇒ 死代码"这条路在 NVIDIA RM 上
**根本不成立**（67.5% 的函数零引用，连 53KB 的都是）。同时把 `0x8C040` 的 33 个
访问点**全部钉到了 11 个函数**上，确认它们都是"读-改-写链路速率"的调速点。
最后实测：**C. ZOMBIE 无法用根端口 `reset_subordinate` 救回**，只能整机重启。

---

## 1. 新工具（`pcie-gen2/tools/`）

| 工具 | 作用 |
|---|---|
| `callgraph.py` | ELF 全量调用图。`syms/callers/refs/graph/path/imms` 六个子命令 |
| `write_sites_8c040.py` | 把每个 `mov $0x8c040,%reg; call` 钉到「函数 + 偏移 + 调用目标」 |
| `rel_tables.py` | 挖 `.text` 内「同节相对地址表」（无重定位的间接分派）|
| `recover-zombie.sh` | 用根端口 `reset_subordinate` 尝试救 ZOMBIE（**结论：无效**）|

分析对象改为本地副本 `/data/labs/nvidia/ko-local/nvidia-6.12.113.ko`，
不必每次 ssh 到 union 读 `/lib/modules/...`。

---

## 2. ★ 环境变了（必须先看）

```
uname -r  = 6.12.113+deb13-amd64          ← 不是记忆里的 6.1.0-50-amd64
os        = Debian GNU/Linux 13 (trixie)  ← 不是 bookworm
NVRM      = 580.178.04（gcc 14.2.0 编译） ← 记忆里的 "gcc-12 才能编" 已过时
nvidia.ko md5 = 7f6805ed15f1ae8122aa78e65a5d48a0（原始，未打补丁）
```
`.ko` 重建于 10-28 09:25，但 **RM 的 `.text` 布局没变**：
`_nv017281rm` 仍在 `.text+0x1269f0`，锁定环仍在 `.text+0x126c4e~0x127048`，
签名 `83fa010f84700300` 与 `file=0x127040` 依旧成立 ⇒ 旧补丁偏移仍然有效。

`/lib/modules/.../updates/dkms/` 下有 `nvidia.ko.orig`（同一 md5），可随时回滚。

---

## 3. ★★★ 方法论纠错（最重要，别再犯）

### 3.1 一开始的错：只扫 `.rela.text`
`who_calls.py` 只遍历 `.rela.text*`。`_nv005948rm` 在 `.rela.text` 里 0 命中，
我差点据此判"死代码"。扩到**全部 SHT_RELA**（`.rela.rodata` 63,482 条、
`.rela.data` 4,188 条）后仍是 0，看似坐实 —— **但结论依然错**。

### 3.2 第二个错：把「节符号 + addend」当成假节点
`mov $imm32,%reg` 形式的重定位，符号常常是**节符号**（`STT_SECTION`，名字为空或 `.text`），
用 addend 表示节内偏移。只按符号名建图 ⇒ 这些边全部塌缩成一个假节点 `.text`。
修正后解出 9,685 条，零入边比例 74.3% → 67.5%。

### 3.3 决定性校准：**零引用比例本身就是判据**
| 尺寸桶 | 总数 | 零入边 | 零占比 |
|---|---|---|---|
| ≤0x10 桩 | 14,980 | 13,369 | 89.2% |
| ≤0x40 | 15,112 | 11,950 | 79.1% |
| ≤0x200 | 24,643 | 12,854 | 52.2% |
| ≤0x1000 | 6,050 | 2,904 | 48.0% |
| **>0x1000** | 282 | **158** | **56.0%** |

`_nv020152rm` 有 **53 KB**、`_nv005948rm` 有 **12 KB**，都是零入边。
⇒ **"零入边"在 RM 里没有任何鉴别力**，不能作为死代码证据。

### 3.4 为什么抓不到：间接分派
`_nv005948rm` 的入口反汇编：
```asm
b3a240: mov %rdx,%r8
b3a243: movzwl 0x10(%rsi),%eax
b3a26a: cmove %rcx,%rsi
b3a272: mov %rsi,0x1a0(%rdi)      ← 往对象槽里装函数指针
b3a279: cmp $0x1,%rax ; je b3ace0
```
它有 **189 个"取地址"重定位**（157 个是节符号 + addend，即无名局部函数），
本质是**按槽位安装函数指针的构造函数**。调用方之后用 `call *0x1a0(%rdi)` 之类
**间接调用**，对重定位图完全不可见。
（`rel_tables.py` 已验证：`.text` 内**没有**同节相对地址表，4519 个命中点都在随机期望之下，是噪声。）

⇒ **静态无法判定 RM 函数的存活。** 要判必须动态（perf 断点）。

---

## 4. ★★★ 33 个 `0x8C040` 访问点全部定位

`write_sites_8c040.py` 结果（`.text` 偏移 / 调用目标 / 所在函数 / 函数内偏移）：

| 所在函数 | 处数 | 调用目标 | 说明 |
|---|---|---|---|
| `_nv017281rm` | 2 | 0x126c5d→`_nv053907rm`(读) 0x126cb0→`_nv053912rm`(写) | **锁定环所在函数** |
| `_nv017283rm` | 2 | 同上 | 兄弟函数 |
| `_nv017105rm` | 7 | 4 读 / 3 写 | |
| `_nv017102rm` | 7 | 4 读 / 3 写 | |
| `_nv017628rm` | 5 | 3 读 / 2 写 | |
| `_nv017629rm` | 5 | 3 读 / 2 写 | |
| `_nv017604rm` | 1 | `_nv049698rm` | 另一条封装 |
| `_nv051299/51300/51301/51302rm` | 各 1 | `_nv053907rm` | 只读 |

按调用目标汇总：`_nv053907rm` 19、`_nv053912rm` 12、`_nv049698rm` 2。

### 两个封装的角色（反汇编确认）
- **`_nv053907rm`**（`.text+0x5ffda0`，0x5e 字节，5,258 个入边）= **虚分派读**
  ```asm
  5ffdd4: mov %ecx,%esi      ; 偏移作第 3 参 → ESI
  5ffddd: jmp  *0x28(%rax)   ; 尾跳通过 ops 槽 +0x28
  ```
  （这解释了为何 perf 抓 CX 能读到 offset —— CX 就是第 3 个参数）
- **`_nv053912rm`**（`.text+0x5ff6c0`，0x1b1 字节，3,658 个入边）= **带重试循环的写**
  ```asm
  5ff6e0: mov %esi,0xc(%rbp)
  5ff6f4: cmp $0x20,%edi ; jne ...
  5ff747: test %ebx,%ebx ; jne 5ff730   ← 回环重试
  5ff773: lea 0x4120(%rdx),%rdi          ← BAR 映射基址在对象 +0x4120
  ```

### 11 个函数的安装器（谁把它们的地址写进对象槽）
| 函数 | 被谁以「取地址」方式安装 |
|---|---|
| `_nv017105rm` `_nv017102rm` `_nv017628rm`(×2) `_nv017629rm` `_nv017281rm` `_nv017283rm` | **`_nv005948rm`**（+0x96f/+0xe98/+0x13ef/+0x1557/+0x1586/+0x166c/+0x287f）|
| `_nv017604rm` | `_nv005949rm`（+0xd57/+0x1628/+…）|
| `_nv051299~51302rm` | `_nv006025rm` |

**三个安装器 `_nv005948rm`/`_nv005949rm`/`_nv006025rm` 自身全部零入边。**
⇒ 这印证了 3.4：安装器也是被间接调到的，静态看不见。

---

## 5. 对旧结论的修正

| 旧结论（reports/192-193）| 现在 |
|---|---|
| "锁定环只在链路异常时执行" | **未证实**。无法排除它根本不执行，也无法证明。 |
| "零引用 ⇒ `_nv005948rm` 是死代码 / 锁定环是死代码" | **推翻**。67.5% 的函数零引用，此判据无效。 |
| "`_nv005948rm` ↔ `_nv017282rm` ↔ `_nv017281rm` 是唯一路径" | 成立（唯一具名/节符号引用），但**不代表可达性**。 |
| `_nv053907rm` = 读、`_nv053912rm` = 写 | **确认**（本次反汇编证实）|

**仍然成立的**：补丁本身安全（唯一入口 `0x126c8a je`、唯一出口 `0x127008 jmp`、
落点只写 `0x8C040`）、`file = text + 0x40`、签名唯一。
**仍然未知**：该分支是否被执行 —— 只能动态答。

---

## 6. C. ZOMBIE 软件恢复实验：失败（新增一条判据）

### 本次设备死因（dmesg 实证）
```
[5762] NVRM: Xid (PCI:0000:01:00): 79, pid=13176, name=nvitop, GPU has fallen off the bus.
[5762] NVRM: Xid (PCI:0000:01:00): 154, GPU recovery action 0x0 → 0x1 (GPU Reset Required)
[6945] NVRM: Attempting to remove device 0000:01:00.0 with non-zero usage count!
```
⇒ ZOMBIE 是 **nvitop 触发的 Xid 79**（监控程序在设备已异常时反复读，把它彻底打死）。
**教训：实验机上不要长期挂 `nvitop` / 任何轮询 `nvidia-smi` 的监控。**
`nvidia.ko` 卸不掉也是因为 nvitop 占着 `/dev/nvidia0`、`/dev/nvidiactl`、`/dev/nvidia-uvm`。

### 尝试过的恢复路径（按代价递增）
| 步骤 | 结果 |
|---|---|
| `pkill -9 nvitop` → `rmmod nvidia_uvm nvidia` | ✅ 成功（之前始终 EBUSY 的根因就是它）|
| 卸载过程中内核自动 `remove` 设备 | ✅ 设备离开总线（dmesg: `Continuing with GPU removal`）|
| 根端口 `0000:00:01.0/reset_subordinate`（kernel 6.12 新接口）| ⚠ 写入成功 exit=0，**但设备没回来** |
| `echo 1 > /sys/bus/pci/rescan` | ❌ 扫不到 |
| 结论 | **ZOMBIE/GONE 只能整机重启，软件无解** |

### 新工具 `recover-zombie.sh`
固化了上述流程（含 `--dry` 干跑）。**注意**：
- 驱动还绑着时执行 `reset_subordinate` 会让 ssh 会话被杀（实测 exit 137），必须**先卸模块**
- 卸模块前必须先 `pkill -9 -x nvitop`（或 `fuser -k /dev/nvidia*`）

---

## 7. ★ 重启踩到 GRUB recordfail

`systemctl reboot` 后机器**停在 GRUB 菜单不走了**（网络不通、ssh 22 关闭）。
原因：Debian 的 `/etc/grub.d/00_header` 里有
```sh
if [ "${recordfail}" = 1 ] ; then set timeout=-1 ; fi
```
`recordfail` 由 `grub-editenv` 维护。本次因为我们强制卸驱动 + 做总线复位，
上一次启动没走到 boot-complete，于是 `recordfail=1` ⇒ **timeout=-1 永久等待**。

**处理**：机器上按 Enter（默认第一项）即可继续。
**启动后务必清掉**：
```bash
sudo grub-editenv - list          # 看到 recordfail=1 就是这个原因
sudo grub-editenv - unset recordfail
```
**（记进操作禁忌：远程 reboot 前先 `grub-editenv list` 确认 recordfail 不为 1，
否则机器会卡在 GRUB 等人工按键，等于把远程操作变成必须到现场。）**

---

## 8. 下一步（重启回来后按顺序）

1. `grub-editenv - unset recordfail`，确认 `lsmod | grep nvidia` 干净
2. 确认设备回来：`lspci -Dn | grep 1b87`、`setpci 0.l` 不是 ffffffff
3. **（最高优先）动态判定锁定环是否执行**
   ```bash
   perf record -e probe:nv017281 --pfm-events=... # 或硬件断点
   # 触发条件：跑 CUDA（bwtest2）+ 人为制造一次 Gen2→Gen1 掉速
   ```
   若 `_nv017281rm` 一次不命中 ⇒ 补丁无意义，**从 gen2kr 直接删掉补丁路线**
4. 同样对 **`_nv053907rm`**（19 个点，从未单测过）做一次 perf 断点，
   确认它的 CX 里是否出现 `0x8C040` —— 这是"谁读 SPEED"的第一手数据
5. 若都命中：`patch_speed_lock.py apply` → 重载驱动 → `safe-gen2.py` →
   制造掉速，看链路能否**自己回 Gen2**（能则去掉常驻守卫）
6. 长期稳定性 ≥30 min（睡眠已 mask）
