# N200 — 修复 primusrun 无法启动 Heaven：无输出矿卡配 Bumblebee 的两个必需项

**日期**：2026-10-29 07:06–07:20
**现象**：`primusrun ./heaven` 报
`primus: fatal: Bumblebee daemon reported: error: [XORG] (EE) Unable to locate/open config directory: "/etc/bumblebee/xorg.conf.d"`
**结果**：已修复。`primusrun glxinfo` 现在报 `NVIDIA P104-100/PCIe/SSE2`，Heaven 能跑（GPU 73% / 138 W）。

---

## 0. 报错的第一行是噪音，真正原因在第三行

bumblebeed 日志的完整序列：

```
(EE) Unable to locate/open config directory: "/etc/bumblebee/xorg.conf.d"   ← 噪音（目录其实存在）
(EE) Failed to load module "mouse" (module does not exist, 0)               ← 噪音
(EE) No devices detected.                                                   ← ★ 根因
(EE) no screens found                                                       ← 后果
(EE) Server terminated with error (1)
```

前两条是 Xorg 找不到可选模块/空目录的正常抱怨，不影响启动。
`No devices detected` 才是 nvidia driver **完全没有认到 P104**。

---

## 1. 根因：P104 是 "3D controller"，且配置缺两项

```
$ lspci -nn | grep -iE "vga|3d"
00:02.0 VGA compatible controller [0300]: Intel ... (rev 06)          ← 核显，负责显示
01:00.0 3D controller [0302]: NVIDIA GP104 [P104-100] [10de:1b87]     ← ★ 0302，不是 0300
```

`.Heaven` 这类矿卡**没有显示输出**，PCI class 是 `0302`（3D controller）而不是
`0300`（VGA controller）。Xorg 的 nvidia driver 在默认配置下：
1. 只自动添加 `drm` 设备 —— 而这里拿到的是 `card0`（**核显**）
2. `AutoAddGPU=false` + `ProbeAllGpus=false` ⇒ 不会自己去扫
3. 没有 `BusID` ⇒ 不知道要绑定哪块
4. 没有 `AllowEmptyInitialConfiguration` ⇒ 即使找到卡，无显示设备也会判失败

日志里的证据：

```
(II) xfree86: Adding drm device (/dev/dri/card0)      ← 拿到的是核显的 card0
(II) NVIDIA dlloader X Driver  580.178.04
(II) NVIDIA Unified Driver for all Supported NVIDIA GPUs
(EE) No devices detected.                            ← driver 起来了，但没卡可绑
```

补充：`/dev/dri/` 里**只有 `card0` 和 `renderD128`**，P104 没有 `card1`/`renderD129`
（矿卡通常不创建 DRM 节点），所以 PRIME render offload 那条路也走不通（实测无输出、无报错）。

---

## 2. 修复：给 `/etc/bumblebee/xorg.conf.nvidia` 加两项

```diff
 Section "Device"
     Identifier  "DiscreteNvidia"
     Driver      "nvidia"
     VendorName  "NVIDIA Corporation"
+    # P104-100 在 01:00.0（3D controller，无显示输出）
+    BusID       "PCI:01:00:0"
     Option "ProbeAllGpus" "false"
     Option "NoLogo" "true"
     Option "UseEDID" "false"
+    # 矿卡无输出：必须允许空初始配置，否则 nvidia driver 报 No devices detected
+    Option "AllowEmptyInitialConfiguration" "true"
     Option "UseDisplayDevice" "none"
     Option "HardDPMS" "false"
 EndSection
```

**两项缺一不可：**
- `BusID "PCI:01:00:0"` —— 明确告诉驱动绑哪块卡
- `Option "AllowEmptyInitialConfiguration" "true"` —— 允许"没有输出设备也起 X"

原文件已备份：`/etc/bumblebee/xorg.conf.nvidia.bak.20261029-070751`

然后 `systemctl restart bumblebeed`。

---

## 3. 验证

```bash
export XAUTHORITY=/run/user/1000/xauth_lxbtwB   # KDE Wayland 会话下 Xwayland 的 auth
export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/1000
primusrun glxinfo -B
```

修复前：无输出 / 失败
修复后：

```
OpenGL vendor string: NVIDIA Corporation
OpenGL renderer string: NVIDIA P104-100/PCIe/SSE2
OpenGL version string: 4.6.0 NVIDIA 580.178.04
```

### Heaven 实跑（20 秒采样）

```
GPU util:  5%  → 68% → 73%
功耗:      60.94 → 138.51 → 138.05 W
显存:      335 → 685 MiB
温度:      43 → 45 → 46 °C
窗口:      Set 1920x1080 windowed video mode
Xid:       0
```

**确实在渲染**（73% util / 138 W / 温升 3°C）。

### 用的命令（非交互，绕开 GUI 启动器）

```bash
cd Unigine_Heaven-4.0/bin
primusrun ./heaven_x64 \
  -project_name Heaven -data_path ../ \
  -engine_config ../data/heaven_4.0.cfg \
  -system_script heaven/unigine.cpp \
  -video_app opengl
```

> `./heaven` 是 bash 包装脚本，会走 GUI 启动器（`browser_x64 -config ../data/launcher/launcher.xml`）。
> 修好 primusrun 后那个入口也能用，只是需要手点。

---

## 4. 可忽略的三个警告

```
GLRender::GLRender(): Unknown NVidia GPU
GPUMonitorPlugin::init(): can't initialize GPUMonitor
GPU: Unknown GPU x1
Video memory: 256 MB            ← 明显错，P104 有 8 GB
```

**原因**：Heaven 4.0 是 2013 年的程序（`Binary: Linux 64bit GCC 4.4.5 Release Feb 13 2013`），
不认识 Pascal 架构，且读显存的旧接口在新驱动上失效（只读到 256 MB 而非 8 GB）。

**不影响渲染**。但意味着：
- GPUMonitor 插件（右上角显存/频率显示）不可用
- 别信它报的 Video memory

---

## 5. 测性能时要注意的两点（沿用此前结论）

1. **Wayland 下 Xwayland 有 present 节流**，窗口呈现的帧率可能被封在 60 fps。
   历史记录：同卡同 Xwayland，`vkmark` 原生 Vulkan 1080p = 358 fps，
   而 `FIFO` 模式精确 60.0 fps。**判好坏不能只看 FPS，要同时看 GPU util。**
2. **`vblank_mode=0`** 保留（用户原来就加了），避免 vsync 限帧。

---

## 6. 与 PCIe Gen2 课题的关系

本次跑 Heaven 时链路是 **Gen1**（`LNKSTA=1041`，`LCAP2=0x06`），`Xid=0`。
`gen2kr` 模块在跑，但 GL 负载没有把链路推上去（该模块的触发点是 `nv_pci_probe`）。

⇒ 若要用 Heaven 测「Gen2 vs Gen1」的真实差异，需要先让链路到 Gen2
（`safe-gen2.py` 或 `gen2kr` + bind），并注意 `reports/188` 的"新上下文会把链路打回 Gen1"
这条特性 —— Heaven 启动时会新建 GL 上下文，可能同样触发。
