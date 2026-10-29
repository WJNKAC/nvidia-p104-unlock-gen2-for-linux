# P104-100 的空闲压功耗 Xorg

## 先说结论：多数情况不需要本目录

省电的机制只有一条：**有一个真正的 Xorg 占着这张卡**。
只要 Xorg 在，系统的 `/usr/share/X11/xorg.conf.d/nvidia-drm-outputclass.conf`
（`MatchDriver "nvidia-drm"` → `Driver "nvidia"`）就会自动把 nvidia 驱动挂上，
卡随即进 P8。**不需要任何自定义配置。**

| 你的情况 | 要不要本目录 |
|---|---|
| 跑 **X11 桌面会话** | **不要**。SDDM 起的那个 Xorg 就已经占着卡了 |
| 装了 **bumblebee** 并且它留着 X | **不要**。`/etc/bumblebee/xorg.conf.nvidia` 就是这份配置，本项目只是复用它 |
| 跑 **Wayland 桌面**（如本机 KDE 6） | 需要。Wayland 下只有 `Xwayland`，**它不算**（实测卡仍停在 P0 烧 22 W） |
| 无桌面（服务器 / 纯 console） | 需要 |

本机的 `SDDM` 走 `Session=plasma` → `startplasma-wayland`，所以开机后
`pgrep -x Xorg` 是 0，只有 `Xwayland :0`，卡就一直 P0 空烧。这才是本目录存在的理由。

> ⚠ 顺带纠正一个常见的印象：**Wayland 下不会自动出现 Xorg**。
> 有的机器看着「装完驱动就有个 Xorg」，是因为那台跑的是 X11 会话，
> 或者 bumblebee 的 X 还留着（`KeepUnusedXServer=true`）。本机两者都不是。

---

## 效果

没有它，P104-100 在 **0% 利用率、什么都不干的时候也一直停在 P0**，
SM 时钟顶在 1607 MHz，白烧 **22 W**，等多久都不降。
起一个最简的"无头" X server 让驱动接管显示子系统后，约 15 秒进 **P8**，
功耗掉到 **~7.8 W**。

---

## 实测（本机 P104-100 / 驱动 580.178.04 / 内核 6.12.113）

| 状态 | P-state | SM 时钟 | 显存时钟 | 功耗 | 温度 |
|---|---|---|---|---|---|
| 没有专用 X | **P0** | 1607 MHz | 405 MHz | **22.3 ~ 22.6 W** | 38 ℃ |
| 起 X 后头 15 s（初始化） | P0 | 1607 MHz | 5005 MHz | 52 ~ 59 W | 38→39 ℃ |
| 起 X 后稳定 | **P8** | 139 MHz | 405 MHz | **7.8 ~ 8.0 W** | 37 ℃ |
| 停掉 X | **P0** | 1607 MHz | 405 MHz | **22.4 W** | 37 ℃ |

净效果：**22.4 W → 7.8 W，省约 14.6 W（-65%）**，一整年按 0.6 元/度算 ≈ 77 元。

双向都验过，是可逆的：起 X 降下去，停 X 立刻弹回 22.4 W。

原始采样：

```
# 没有专用 X（只有桌面的 Xwayland，等于没有）
P0, 22.36 W, 1607 MHz, 405 MHz, 38, 0 %
P0, 22.61 W, 1607 MHz, 405 MHz, 38, 0 %
P0, 22.45 W, 1607 MHz, 405 MHz, 38, 0 %

# 起了专用 X，等过初始化
P8, 7.91 W, 139 MHz, 405 MHz, 37, 0 %
P8, 7.80 W, 139 MHz, 405 MHz, 37, 0 %
P8, 7.76 W, 139 MHz, 405 MHz, 37, 0 %
```

> 说明：这里用的是 `nvidia-smi --query-gpu=pstate,power.draw,clocks.*`，
> 这些字段是实时读取的。**但同一命令里的 PCIe 字段是驱动缓存，不能用来判链路速率**
> （见 `docs/判据` 那套规矩）。

### ⚠ 一个容易误判的点：`glxinfo` 会把卡唤醒

任何 GL 操作都会把卡从 P8 拉回 P0。实测跑一次 `glxinfo -B` 之后：

```
基线          P8,  7.81 W, 139 MHz
+1s  glxinfo  P0, 59.17 W, 1607 MHz     ← 被唤醒
+4s           P0, 53.57 W, 1075 MHz
+7s           P8,  7.57 W, 240 MHz      ← 自己回落
+10s          P8,  7.81 W, 139 MHz
```

要 **7~9 秒**才回 P8。所以：

- 看功耗用 `./xorg-p104.sh power`，**别紧接着跑 `glxinfo`**
- `status` 已经排成「先读功耗、后查 GL」，并会提示这一点
- 刚被唤醒时读到 P0 不代表没生效，等十几秒再看

---

## 用法

```bash
sudo ./xorg-p104.sh start      # 起在 :8，之后卡会自己掉进 P8
./xorg-p104.sh power           # 看 P-state / 功耗 / 时钟（只读，不用 root）
sudo ./xorg-p104.sh stop       # 停掉 —— 卡会回到 P0 继续烧 22 W
```

| 动作 | 说明 |
|---|---|
| `status` | X 是否在跑 + 它的 GL 渲染器 + 当前 P-state（只读） |
| `power` | 采样 P-state / 功耗 / 时钟，并直接告诉你进没进 P8（只读） |
| `start` | 起专用 X（默认 `:8`，需 root） |
| `stop` | 停掉（会回到 P0） |
| `restart` | `stop` + `start` |
| `gl` | 在这个 X 上跑 `glxinfo -B` |
| `offload` | 打印 PRIME offload 用法（要跑程序才看，与省电无关） |

换显示号：`P104_DISPLAY=:9 sudo ./xorg-p104.sh start`

典型输出：

```
$ sudo ./xorg-p104.sh start
── 检查前置 ──
✓ 配置 p104-xorg.conf
✓ Xorg 二进制
✓ P104-100 在总线

── 启动 Xorg :8 ──
   $ /usr/lib/xorg/Xorg :8 -config p104-xorg.conf
✓ 已启动

── 验证渲染器 ──
   OpenGL renderer string: NVIDIA P104-100/PCIe/SSE2
✓ 渲染器正确

   卡应在大约 15 秒内掉进 P8（功耗 22 W → ~7.8 W）
   确认: ./xorg-p104.sh power        停止: sudo ./xorg-p104.sh stop
```

```
$ ./xorg-p104.sh power
── P104 功耗状态 ──
   专用 X :8：运行中
   P8, 7.81 W, 139 MHz, 405 MHz, 37
   → P8，空闲降档已生效
```

---

## 为什么"起个没人看的 X"就能省电

P104-100 是 **3D controller（PCI class 0302）**，没有物理显示输出。
按常理会觉得没输出的卡无所谓显示子系统 —— 但驱动不是这么想的：
**只有接管了一个 screen，驱动的动态功耗管理才会把空闲的卡往下调**。
没有 X，卡就一直挂在 P0 满频待机。

所以要的不是"能显示"，而只是**让驱动认领这个 screen**。这就是为什么配置里要
`AllowEmptyInitialConfiguration`（允许没有 CRTC/输出的空配置）：

```conf
Option "AllowEmptyInitialConfiguration" "true"
Option "UseDisplayDevice" "none"
```

不写这两行，驱动直接报 `No devices detected / No screens found` 退出。

顺带说明：**"0302 就不能起 X" 是错的**。P106 装上驱动后本来就有 Xorg，
P104 同理，只是没有输出口而已。

---

## 为什么本目录的 conf 只是"备胎"

本机装了 **bumblebee 3.2.1**，它的 `/etc/bumblebee/xorg.conf.nvidia`
和本目录的 `p104-xorg.conf` 内容等价（`BusID "PCI:01:00:0"`、
`AllowEmptyInitialConfiguration`、`UseDisplayDevice "none"` 全都有）——
本项目这份就是从参考项目抄的，而参考项目抄的就是 bumblebee。

所以脚本**优先用系统的**：

```
$ sudo ./xorg-p104.sh start
✓ 配置 /etc/bumblebee/xorg.conf.nvidia（系统自带，装了 bumblebee）
```

只有系统没有 bumblebee 时才退回本目录那份。两条路实测都是 **P8 / 7.6~8.0 W**：

| 用的配置 | 结果 |
|---|---|
| `/etc/bumblebee/xorg.conf.nvidia` | P8, 7.61 W, 139 MHz |
| 本目录 `p104-xorg.conf` | P8, 7.8~8.0 W |

另外实测确认：**完全不指定任何 Device 也能降**（Xorg 靠 OutputClass 自动挂 nvidia，
只建 Intel 的 screen，P104 也被初始化）—— 那 60 行 Device 配置其实可以不要。
保留它只是为了在有 Intel + NVIDIA 的机器上**只碰 NVIDIA、不抢 Intel 的 DRM master**。

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

所以本目录的配置**一律用 `Xorg :N -config <file>` 显式指定**，脚本已经这么做了。

（Bumblebee 的 `/etc/bumblebee/xorg.conf.nvidia` 也是同样思路 —— 放在
`/etc/bumblebee/` 而不是 `xorg.conf.d/`，由 bumblebee 按需传给 Xorg。）

---

## 开机后要不要自动起

本仓库**不装 systemd service**（这个项目的原则是不动系统）。

开机后手动跑一次即可：

```bash
sudo ./xorg-p104.sh start
```

它是幂等的，重复跑只会提示"已在运行"。
想自己挂到别处（`~/.config/autostart`、rc.local、自己的 service）请自便，
但别把 `p104-xorg.conf` 丢进 `xorg.conf.d/`。

更"原生"的做法（如果确实想让它自动）：

- **切成 X11 会话** —— SDDM 自己起 Xorg，本目录整个都不需要了
- 或让 **bumblebee 留着 X**：`/etc/bumblebee/bumblebee.conf` 里
  `KeepUnusedXServer=true`（本机当前是 `false`，所以不常驻）

本机 `bumblebeed` 日志里那条 `No switching method available. The dedicated card
will always be on.` 说明没有 bbswitch，卡没法断电 —— 只能靠降 P8 来省电。

---

## 附：顺带说一下"让程序跑在 P104 上"

这和压功耗是**两件独立的事**，起了这个 X 也不会让程序更快或更省电。

如果确实要让程序用 P104 渲染（Intel 负责显示），用 PRIME offload，零配置：

```bash
__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia <程序>
```

`glxinfo -B` 会从 `Mesa Intel(R) HD Graphics P4600/P4700` 变成
`NVIDIA P104-100/PCIe/SSE2`（8192 MB，`direct rendering: Yes`）。

> ⚠ 在**非活动的 Wayland 会话**里（`loginctl show-session <sid> -p Active` 为 `no`，
> 典型是只通过 SSH 连过来）测帧率会得到假数字：合成器不合成，
> `SwapBuffers` 被节流 ⇒ 只有 ~2 FPS。**核显原生跑同样只有 ~2 FPS**，
> 所以这不是 P104 或 offload 的问题。

---

## 文件

| 文件 | 说明 |
|---|---|
| `p104-xorg.conf` | 备胎配置。系统有 bumblebee 时不用它（脚本会自动选系统的） |
| `xorg-p104.sh` | 起/停/看功耗的管理脚本 |

X 的日志在 `/tmp/xorg-p104-<显示号>.log`。
启动期间当前 KDE Wayland 会话不受影响（`kwin_wayland` 仍在），键鼠也不会被抢；
停止后 `Xorg` 进程数归 0。
