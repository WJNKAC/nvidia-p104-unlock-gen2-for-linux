# patches/ —— 按驱动版本组织

```
patches/
└── 580.178.04/                 一套完整验证过的驱动版本
    ├── 00-whitelist.sh         前置（可选）：解除受限 SKU（改 o_binary 的设备表）
    ├── 01-gen2-patch.sh        必做：改 o_binary 的 mode 白名单（2 字节）
    ├── 02-gen2-push.sh         必做：编译 gen2kr → insmod → unbind/bind 推链路
    ├── 03-gen2-push-light.sh   可选：轻量推送（不重编，只重新 probe）
    ├── 04-build-nvidia.sh      必做：重编 nvidia.ko + 安装 + 同步 DKMS
    ├── patch-gen2.py           2 字节补丁的实现（被 01 调用）
    ├── offsets.sh              偏移与原始字节（可 source）
    ├── sync-dkms.sh            DKMS 三处 .ko 同步
    ├── README.md               本版本简介 + 偏移表 + 验证数据
    ├── src/kernel/             拦截模块源码（gen2kr.c + Makefile + .service）
    └── output/                 编译产物（gen2kr.ko / nvidia.ko；不入库）
```

## 两类模块，两种编法

| 模块 | 绑定什么 | 谁编译 | 产出 |
|---|---|---|---|
| **nvidia.ko**（最终驱动） | **驱动版本** | `04-build-nvidia.sh` | `output/nvidia.ko` → 装载位 |
| **gen2kr.ko**（拦截模块） | **内核版本** | `02-gen2-push.sh` 里自动编 | `output/gen2kr.ko` |

拦截模块跟驱动版本无关、只跟内核 ABI 有关，所以放在"升 Gen2"的脚本里顺手编掉；
nvidia.ko 要跟着 `o_binary` 的改动手动重编，单独一个脚本。

## 为什么改 o_binary 而不是 .ko

`nvidia.ko` 是编译产物。直接改它，任何一次 `make` / `dkms install` / 装新内核
都会覆盖掉——这是过去"重启后 19fps 依旧"的根因之一。

改成改**编译输入**（`nv-kernel.o_binary`），再重编安装，产物从源头就是对的。

## 新增一个驱动版本

```
patches/<新版本>/
```

偏移跟驱动版本绑定，换版本时**新建同级目录**，不要原地改名 —— 老目录是历史验证记录。
`utils/lib_v1.sh` 里 `DRIVER_VER` 环境变量可切换，默认 580.178.04。
