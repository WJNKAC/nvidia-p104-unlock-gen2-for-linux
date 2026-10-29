# P104-100 的 Xorg 配置

P104-100 是 **3D controller（PCI class 0302）**，没有物理显示输出。
但装了 NVIDIA 驱动后**照样能起 X server** —— 只要允许"空初始配置"，
它会建一个没有 CRTC 的 X screen，OpenGL 渲染完全正常。

本目录提供两条路，**先用 offload，不够再用专用 X**。

---

## 推荐：PRIME offload（不需要任何配置文件）

Intel 核显负责输出，P104 负责渲染。当前桌面是 Wayland 还是 X11 都能用：

```bash
__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia <程序>
```

自测：

```bash
__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia glxinfo -B | grep -E "renderer|memory"
```

本机实测（Intel HD P4600 + P104-100，驱动 580.178.04）：

| | 渲染器 |
|---|---|
| 不加环境变量 | `Mesa Intel(R) HD Graphics P4600/P4700 (HSW GT2)` |
| 加 offload | `NVIDIA P104-100/PCIe/SSE2`，8192 MB，`direct rendering: Yes` |

**这条路零配置、不改变现有显示，出问题只需去掉两个环境变量。**

### ⚠ 在这个会话里测帧率，会得到一个假数字

在**非活动的 Wayland 会话**里（`loginctl show-session <sid> -p Active` 为 `no`，
典型场景是只通过 SSH 连过来、VT 不在这个会话上），合成器不合成，
客户端的 `SwapBuffers` 被节流 ⇒ `glxgears` 只有 **~2 FPS**。

**这不是 P104 或 offload 的问题 —— 核显原生跑同样只有 ~2 FPS**：

| 命令（同一个非活动会话里） | 结果 |
|---|---|
| `glxgears`（Intel 原生） | 1.7 ~ 1.8 FPS |
| offload 到 P104，`glxgears` | 2.1 ~ 2.2 FPS |
| offload 到 P104 + `vblank_mode=0 __GL_SYNC_TO_VBLANK=0` | ~1900 FPS |
| 专用 X（`:8`）上 `glxgears` | ~23000 FPS |

⇒ offload 是好的（渲染器确实换成了 P104），只是这个会话里测不出真性能。
要可信的数字，上专用 X，或先关 vblank。

---

## 备选：专用 X server

什么时候需要：

- 程序不吃 PRIME offload（少数老程序、直接开 `/dev/nvidia*` 的）
- 想整屏都是 P104（跑 benchmark）
- 想用 `nvidia-settings` 调 P104 的 fan / power

```bash
sudo ./xorg-p104.sh start      # 起在 :8
DISPLAY=:8 glxinfo -B          # 应显示 NVIDIA P104-100
sudo ./xorg-p104.sh stop
```

完整动作：

| 动作 | 说明 |
|---|---|
| `status` | 是否在跑 + GL 渲染器（只读，不需要 root） |
| `start` | 起一个专用 X（默认 `:8`，需 root） |
| `stop` | 停掉 |
| `restart` | stop + start |
| `gl` | 在专用 X 上跑 `glxinfo -B` |
| `offload` | 打印 offload 用法（就是上面那条命令） |

改显示号：`P104_DISPLAY=:9 sudo ./xorg-p104.sh start`

---

## ⚠ 千万别把它放进 `/etc/X11/xorg.conf.d/`

这是实测踩出来的：

`p104-xorg.conf` 里只有 P104 一个 Device。放进 `xorg.conf.d/` 后，
**P104 会抢到 screen 0**，而 Intel 压根不被加载。P104 没有物理输出 ⇒
X11 桌面直接黑屏。

对比实测（同一个 Xorg，唯一差别是有没有那个文件）：

| | 装了 `xorg.conf.d/20-nvidia-p104.conf` | 不装 |
|---|---|---|
| screen 0 | `NVIDIA(0)` = P104（无输出） | `modeset(0)` = Intel |
| GL 渲染器 | NVIDIA P104-100 | Mesa Intel HD P4600 |
| Intel 是否加载 | **完全没有** | 是 |

所以本目录的配置**一律用 `Xorg :N -config <file>` 显式指定**，
脚本已经这么做了。

（顺带说明：Bumblebee 的 `/etc/bumblebee/xorg.conf.nvidia` 也是同样思路 ——
它放在 `/etc/bumblebee/` 而不是 `xorg.conf.d/`，由 bumblebee 按需传给 Xorg。）

---

## 文件

| 文件 | 说明 |
|---|---|
| `p104-xorg.conf` | 专用 X 的配置（含 `ServerLayout` 关掉 `AutoAddDevices`，避免抢键鼠） |
| `xorg-p104.sh` | 起/停专用 X 的管理脚本 |

X 的日志在 `/tmp/xorg-p104-<显示号>.log`。

---

## 实测记录

在**刚重启、卡还是原厂 Gen1 状态**（`LnkCap MaxSpeed=2.5GT/s`，gen2kr 未加载）下验证，
说明 Xorg 这条路**不依赖 Gen2 解锁**：

```
$ sudo ./xorg-p104.sh status
── 专用 X :8 ──
   未运行

$ sudo ./xorg-p104.sh start
── 启动 Xorg :8 ──
   $ /usr/lib/xorg/Xorg :8 -config p104-xorg.conf
✓ 已启动

── 验证渲染器 ──
   direct rendering: Yes
       Dedicated video memory: 8192 MB
   OpenGL renderer string: NVIDIA P104-100/PCIe/SSE2
✓ 渲染器正确

$ DISPLAY=:8 glxgears
115937 frames in 5.0 seconds = 23187.330 FPS
116368 frames in 5.0 seconds = 23273.553 FPS

$ sudo ./xorg-p104.sh stop
✓ 已停止
```

启动专用 X 期间，当前 KDE Wayland 会话不受影响（`kwin_wayland` 仍在），
键鼠也没有被抢（`AutoAddDevices "false"` 生效）；停止后 `Xorg` 进程数归 0。
