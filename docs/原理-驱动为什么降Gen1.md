# 原理：驱动为什么每次 cuInit 都把链路降回 Gen1

## 一句话

驱动内部有个 `set_link_speed(mode)`，`mode` 就是 PCIe 代数（1/2/3）。
**每次 `cuInit` 走 `rm_init_adapter` 时，它都以 `mode=1` 被调用**，
而 `mode=1` 在这个函数里对应写 `SPEED=2`（Pascal 的 2.5GT/s）。
所以不是"链路掉了"，是**驱动主动要求降级**。

---

## 一、`_nv017105rm` 是驱动的 `set_link_speed(mode)`

`.text+0x127d60`，`0x49e` 字节。入口是一段 mode 白名单检查：

```asm
127d66: lea  -0x1(%rdx),%eax     ; eax = mode - 1
127d81: cmp  $0x2,%eax
127d84: ja   1280c8              ; (mode-1) > 2 → bail（mode 不在 {1,2,3}）
127d8a: cmp  $0x2,%edx  ; je 1280d9    ; mode==2 → 检查能力位 obj[0x954]
127d93: cmpl $0x3,0xc(%rbp) ; je 128169  ; mode==3 → 检查能力位 obj[0x955]
127d9d:                                  ; mode==1 顺序落入（不是跳转来的）
```

三条分支各写什么：

| mode | 指令 | SPEED[19:18] | 速率 |
|---|---|---|---|
| 1 | `or $0x80000,%eax`（127e65） | 2 | **2.5GT/s = Gen1** |
| 2 | `or $0x40000,%eax`（12819b） | 1 | **5.0GT/s = Gen2** |
| 3 | 清 SPEED（eax 已在 127e4e 清过） | 0 | **8.0GT/s = Gen3** |

⇒ **`mode` 直接就是代数。** 这是 Pascal 的编码，Ampere 不一样。

### 一个容易看错的分支

```asm
127ded: cmpb $0,0x4de5(%r13)
127df5: jne  1281a8          ; 看着像 bail
...
1281a8: (NV_PRINTF 打一条警告)  jmp 127dfb    ; ← 其实只是打日志然后继续
```

必须跟到目标地址去确认，否则会把"打日志"误判成"提前返回"。

### 唯一的 return 点

```asm
1280c8: pop  %rbx
1280c9: mov  %r12d,%eax       ; 返回值 = r12d
1280cc: pop  %r12
1280ce: pop  %r13
1280d0: pop  %r14
1280d2: pop  %r15
1280d4: add  $0x40,%rbp
1280d8: ret
```

`%r12d` 在入口就被 `xor %r12d,%r12d` 清零，而 mode=1 路径里对它唯一的改写
（`lea 0x1e08(%r13),%r12`）恰好在我们跳过的代码里。
⇒ **bail 返回 0 = CUDA_SUCCESS**，驱动认为设置成功，不报错、不重试。

这就是补丁能安全工作的前提。

---

## 二、调用链（perf 实测，`-g` 抓的）

每次 `cuInit` 都会走一遍，`DX=0x1`（mode=1），4/4 次：

```
_nv017105rm              ← set_link_speed(mode=1)
_nv017755rm+0x98         ← 直接调用者
_nv021133rm+0x20e
_nv021141rm+0x1c0
_nv021137rm+0x3e
_nv050280rm+0x2bd
_nv014838rm+0x2bb
_nv014904rm+0x682
_nv050526rm+0xcb
_nv015094rm+0x2cc
_nv050988rm+0x5d
_nv031219rm+0xbb
_nv031217rm+0x8a8
_nv031218rm+0x14d
_nv031502rm+0x3e
_nv000790rm+0x1123
rm_init_adapter+0xcd     ← ★ RM 适配器初始化
nv_open_device+0x4b7
nvidia_open+0x1d3
__x64_sys_openat
...
libcuda.so: cuInit
```

**`rm_init_adapter` 是分界线**：在这之前推 Gen2 有效，在这之后推就只是改寄存器、
RM 内部数据面仍按 Gen1 配置（SKP OS 顺序集、credits、MPSS 都是 Gen1 的）。

### `_nv017755rm` 的判断

```c
u32 _nv017755rm(dev, obj, u32 mode)      // r13d = mode
{
    r14d = LNKSTA & 0xf;                 // 当前链路速率
    if ((mode - 1) > 2) bail;
    eax = obj[0x9a8];                    // 缓存的最大支持速率
    if (eax < r14d && eax != 0) goto err;
    if (mode == r14d) return;            // ★ 目标 == 当前 → 什么都不做
    call obj[0x470](dev, obj, mode);     // → _nv017105rm 真正去改
}
```

⇒ 链路是 Gen2（`r14d=2`）而 `mode=1`，两者不等 ⇒ **每次都会主动降回 Gen1**。

这也解释了为什么"在 probe 时机推 Gen2"能成功：
推上去之后如果驱动不再被调用（或调用时 mode 与当前一致），链路就能保持。
但驱动每次 `cuInit` 都传 `mode=1`，所以旧方案必须靠守卫反复拉回。

---

## 三、能力位从哪来

`_nv017105rm` 在 `mode>=2` 时会先查能力位，为 0 直接 bail：

```asm
1280d9: cmpb $0x0,0x954(%rsi)   ; obj[0x954] = cap Gen2
1280e0: jne  127d9d
1280e6: jmp  1280c8             ; 为 0 → bail，什么都不写
```

能力位由 `_nv017812rm(dev, obj, flags)` 设置（`.text+0x11a230`，`0x73` 字节纯 setter）：

```asm
11a23c: test $0x4,%dl  ; je skip
11a241: movb $0x1,0x954(%rsi)   ; flags & 0x04 → cap Gen2
11a24d: movb $0x1,0x955(%rsi)   ; flags & 0x08 → cap Gen3
11a259: movb $0x1,0x956(%rsi)   ; flags & 0x10 → cap mode4
11a265: movb $0x1,0x957(%rsi)   ; flags & 0x20 → cap mode5
```

`flags` 来自 `_nv017279rm`（`.text+0x13c320`）的**五路按位与**：

```asm
13c337: call <直接调用>          → ebx
13c34c: call *(obj+0x1a8)        → r15d
13c362: call *(obj+0x1b0)        → r14d
13c373: call *(obj+0x1b8)        → 存 0xc(rbp)
13c391: call *(obj+0x1c0)        → eax
13c39c: edx = ebx & r15d & r14d & cap3 & eax
13c3b3: jmp _nv017812rm
```

任何一路缺 `0x04`，Gen2 能力就被清掉。

> **仍未闭环**：`obj[0x954]` 的运行时实际值没拿到。
> 抓到 `_nv017812rm` 需要冷启动后**首次** modprobe 的窗口，
> reload 之后已经错过（实测 unbind/bind 期间 0 命中）。

---

## 四、为什么不能只改声明

有一个很自然但错误的猜想：把端点的 `LnkCap2` 声明改成支持 Gen2，驱动就会要 Gen2。

**实测否证**：
- `LnkCap2` 的 BAR0 镜像（`0x880A4`）改了没用 ——
  驱动根本不以立即数形式读它（`.text` 里 `mov $0x880A4` 命中 **0 处**）
- config space 的 `LnkCap2` 在解锁后确实变成 `0x0006`
- 但驱动照样以 `mode=1` 调用 `set_link_speed`

⇒ 决定的不是声明，而是 `rm_init_adapter` 传下来的 `mode` 参数，和 `obj[0x954]`。

---

## 五、试过但无效的路（省得重走）

| 尝试 | 结果 |
|---|---|
| registry `RMPcieLinkSpeed=2` | 触发 CUDA 后仍降回 Gen1 |
| registry `RMPcieStickyGenSpeed=1` | push 阶段就没升上去 |
| 只 patch `_nv017281rm` 的 SPEED 写 | 被 `_nv017105rm(mode=1)` 覆盖 |
| 守卫重写端点 TLS=2 | 写 2 后下一轮读回 1（被驱动钳住） |
| 守卫重新解锁声明 | `CAP2` 恒 0x0002 |
| 手动推 Gen2 后跑 DMA | 链路 Gen2，带宽仍 0.78（RM 内部是 Gen1） |
| 找"第二个写 SPEED 的软件函数" | 不存在（曾经误判为端点内部回写，实为断点没生效） |

`_nv017157rm`（`.text+0x118990`）确实读 `RMPcieLinkSpeed` / `RMPcieGenSwitchOnPmu` /
`RMPcieStickyGenSpeed` 三个 key，且 `RMPcieLinkSpeed` 会写进 `obj[0xa38]`
（策略位域）+ `obj[0xa34]=1`（存在标记），但这两个 key 都不是"要不要 Gen2"的开关。

---

## 六、最终方案

```
① 2 字节补丁：_nv017105rm 的 mode 白名单从 {1,2,3} 收窄成 {2,3}
             ⇒ mode==1 走原有 bail，返回 SUCCESS，驱动不再降级
② gen2kr：在 nv_pci_probe 返回瞬间解锁声明 + 写 SPEED=1 + 训练 + 根端口 Retrain
             ⇒ 链路升到 Gen2，之后由补丁保持
③ 守卫：每 200ms 看一次 LNKSTA，补丁在位时零动作（安全网，防补丁随驱动升级丢失）
```

实测（无守卫）：353 轮 CUDA 上下文，**零降级**、Xid 0、
pinned H2D 1.563 GB/s / D2H 1.705 GB/s。
