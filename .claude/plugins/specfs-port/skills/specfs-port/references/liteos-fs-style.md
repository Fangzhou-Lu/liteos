# LiteOS-A 文件系统代码风格与构建接线模板

把规范翻成 C 代码时，必须与现有 FS（`fs/fat/`、`fs/jffs2/`）的风格保持一致。本文档给出可直接套用的模板。

## 版权头模板

每个 `.c` / `.h` 文件首部使用华为 Device 的 BSD-3-Clause 头：

```c
/*
    * Copyright (c) 2024-2026 Huawei Device Co., Ltd. All rights reserved.
    *
    * Redistribution and use in source and binary forms, with or without modification,
    * are permitted provided that the following conditions are met:
    *
    * 1. Redistributions of source code must retain the above copyright notice, this list of
    * conditions and the following disclaimer.
    *
    * 2. Redistributions in binary form must reproduce the above copyright notice, this list
    * of conditions and the following disclaimer in the documentation and/or other materials
    * provided with the distribution.
    *
    * 3. Neither the name of the copyright holder nor the names of its contributors may be used
    * to endorse or promote products derived from this software without specific prior written
    * permission.
    *
    * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
    * OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
    * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    * COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
    * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
    * GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
    * AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
    * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
    * OF THE POSSIBILITY OF SUCH DAMAGE.
    */
```

如果上游 Linux 文件是 GPL，**不允许**直接复用其源码或注释；必须重写实现并使用上面的 BSD-3-Clause 头。

## 命名约定

| 实体 | 命名 | 示例 |
|---|---|---|
| 公开 API（其它子系统调用） | `LOS_<Module><Verb>` | `LOS_ExfatFlush` |
| VFS 回调（挂在 VnodeOps / Fops 上） | `Vfs<Name><Op>` | `VfsExfatLookup` |
| 模块初始化入口 | `Os<Name><Verb>` | `OsExfatInit`、`OsExfatMount` |
| FS 私有辅助函数 | `<name>_<verb>_<noun>`（小写蛇形） | `exfat_alloc_cluster`、`exfat_load_bitmap` |
| 全局符号 | `g_<name><Suffix>` | `g_exfatVops`、`g_exfatFops` |
| 私有结构体 | 沿用上游 + `_t` 不强制 | `struct exfat_chain` |
| 盘上结构体 | 与上游 `_raw.h` 一致 | `struct exfat_dentry` |
| 公开宏 | `<NAME>_<NOUN>` 全大写 | `EXFAT_EOF_CLUSTER` |
| Kconfig 符号 | `LOSCFG_FS_<NAME>` 全大写 | `LOSCFG_FS_EXFAT` |
| 头文件保护宏 | `_<RELATIVE_PATH>_H` | `_FS_EXFAT_EXFAT_H` |

## 头文件结构

公开头 `fs/<name>/include/<name>.h`：

```c
#ifndef _FS_<NAME>_<NAME>_H
#define _FS_<NAME>_<NAME>_H

#include "vnode.h"
#include "fs/file.h"
#include "fs/mount.h"
#include "los_typedef.h"

#ifdef LOSCFG_FS_<NAME>

/* 盘上常量 */
#define <NAME>_BLOCK_SIZE 512
/* ... */

/* 盘上结构体 */
struct <name>_super_block { /* ... */ } __attribute__((packed));

/* 公开 API */
int Os<Name>Init(void);
int Os<Name>Mount(struct Mount *mnt, struct Vnode *parentVnode,
    struct Vnode **rootVnode, const char *target,
    const void *data);

#endif /* LOSCFG_FS_<NAME> */

#endif /* _FS_<NAME>_<NAME>_H */
```

私有头 `fs/<name>/<name>_pri.h`（如有）按相同结构，但只被 FS 内部源文件 include。

## 源文件结构

```c
/* 版权头 */
#include "<name>.h"
#ifdef LOSCFG_FS_<NAME>

#include <errno.h>
#include <string.h>
#include "securec.h" /* libsec —— 必含 */
#include "los_memory.h"
#include "los_mux.h"
#include "los_printf.h"
#include "vnode.h"
#include "path_cache.h"
#include "disk.h"

/* 仅本文件内部用的静态变量 / 静态辅助 */
static int <name>_internal_helper(...) { ... }

/* 实现公开声明 */
int Vfs<Name>Lookup(struct Vnode *parent, const char *name, int len,
    struct Vnode **vpp)
{
    /* ... */
}

#endif /* LOSCFG_FS_<NAME> */
```

要点：
- 第一个 `#include` 永远是本 FS 的公开头，紧跟着 `#ifdef LOSCFG_FS_<NAME>`，整个文件实现包在这个 `#ifdef` 内。
- 系统头按字母序，自家头紧随其后。
- 所有静态辅助函数加 `static`，以免污染符号表。

## 注册胶水模板（`vfs_<name>.c`）

参考 `fs/jffs2/src/vfs_jffs2.c` 末尾的注册段：

```c
/* fs/<name>/vfs_<name>.c */
#include "<name>.h"
#ifdef LOSCFG_FS_<NAME>

#include "los_init.h"
#include "los_tables.h"
#include "vnode.h"
#include "fs/mount.h"

static struct MountOps g_<name>MountOps = {
    .Mount = Os<Name>Mount,
    .Unmount = Os<Name>Umount,
    .Statfs = Os<Name>Statfs,
};

static struct VnodeOps g_<name>Vops = {
    .Lookup = Vfs<Name>Lookup,
    .Create = Vfs<Name>Create,
    .Mkdir = Vfs<Name>Mkdir,
    .Rmdir = Vfs<Name>Rmdir,
    .Unlink = Vfs<Name>Unlink,
    .Rename = Vfs<Name>Rename,
    .Getattr = Vfs<Name>Getattr,
    .Chattr = Vfs<Name>Chattr,
    .Truncate = Vfs<Name>Truncate,
    .Reclaim = Vfs<Name>Reclaim,
    /* ... */
};

static struct file_operations_vfs g_<name>Fops = {
    .open = Vfs<Name>Open,
    .close = Vfs<Name>Close,
    .read = Vfs<Name>Read,
    .write = Vfs<Name>Write,
    .seek = Vfs<Name>Lseek,
    .ioctl = Vfs<Name>Ioctl,
    .fsync = Vfs<Name>Fsync,
    .readdir = Vfs<Name>Readdir,
    .mmap = Vfs<Name>Mmap,
};

int Os<Name>Init(void)
{
    return VnodeRegisterFs("<name>", &g_<name>MountOps,
    &g_<name>Vops, &g_<name>Fops);
}

LOS_MODULE_INIT(Os<Name>Init, LOS_INIT_LEVEL_KMOD_EXTENDED);

#endif /* LOSCFG_FS_<NAME> */
```

`LOS_INIT_LEVEL_KMOD_EXTENDED` 是常用级别——晚于内核基础设施初始化、早于用户空间。

## 构建文件模板

### `fs/<name>/Kconfig`

```
config FS_<NAME>
    bool "Enable <NAME> filesystem"
    default n
    depends on FS_VFS
    help
    Support for the <NAME> filesystem.

if FS_<NAME>

config FS_<NAME>_FEATURE_X
    bool "Enable <NAME> feature X"
    default n
    help
    Optional sub-feature.

endif
```

### `fs/<name>/BUILD.gn`

参考 `fs/fat/BUILD.gn`：

```gn
import("//kernel/liteos_a/liteos.gni")

module_switch = defined(LOSCFG_FS_<NAME>)
module_name = "<name>"
kernel_module(module_name) {
    sources = [
    "vfs_<name>.c",
    "<name>_super.c",
    "<name>_inode.c",
    "<name>_file.c",
    "<name>_dir.c",
    "<name>_balloc.c",
    "<name>_fatent.c",
    "util/<name>_nls.c",
    "util/<name>_misc.c",
    ]
    include_dirs = [
    "include",
    "$LITEOSTOPDIR/fs/include",
    "$LITEOSTOPDIR/fs/vfs/include",
    "$LITEOSTOPDIR/drivers/block/disk/include",
    ]
}
```

### `fs/<name>/Makefile`

参考 `fs/fat/Makefile`：

```make
include $(LITEOSTOPDIR)/config.mk

MODULE_NAME := $(notdir $(CURDIR))

LOCAL_SRCS := $(wildcard *.c) $(wildcard util/*.c)

LOCAL_INCLUDE := \
    -I$(LITEOSTOPDIR)/fs/<name>/include \
    -I$(LITEOSTOPDIR)/fs/include \
    -I$(LITEOSTOPDIR)/fs/vfs/include \
    -I$(LITEOSTOPDIR)/drivers/block/disk/include

LOCAL_FLAGS := $(LOCAL_INCLUDE)

include $(MODULE)
```

### 顶层接入

`fs/BUILD.gn` 追加：

```gn
if (defined(LOSCFG_FS_<NAME>)) {
    module_group_modules += [ "<name>:<name>" ]
}
```

`fs/Kconfig` 追加：

```
source "fs/<name>/Kconfig"
```

## 错误返回约定

VFS 回调必须返回**负 POSIX errno**：

```c
int VfsExfatLookup(struct Vnode *parent, const char *name, int len,
    struct Vnode **vpp)
{
    if (parent == NULL || name == NULL || vpp == NULL) {
    return -EINVAL;
    }
    /* ... */
    if (!found) {
    return -ENOENT;
    }
    return LOS_OK; /* 0 */
}
```

内部辅助函数允许返回 `LOS_ERRNO_FS_*` 或自定义负数；但向 VFS 边界返回前要翻译：

```c
ret = exfat_alloc_cluster(vn, n, &chain, true);
if (ret != LOS_OK) {
    return -ENOSPC; /* 翻译 */
}
```

## 单元测试位置

新 FS 应在 `testsuites/unittest/fs/` 下追加一个子目录（参照现有 `vfat/`、`jffs/` 子目录的结构）：

```
testsuites/unittest/fs/<name>/
├── BUILD.gn
├── It_<name>_smoke_001.cpp mount + create + write + read + umount
├── It_<name>_smoke_002.cpp mkdir + ls + rmdir
└── ...
```

测试框架是仓库自有的 `It_*` 体系（不是 gtest）。每个文件一个测试，断言宏 `ICUNIT_GOTO_EQUAL` / `ICUNIT_ASSERT_EQUAL`。

## 提交前检查清单

- [ ] 每个翻译单元都包在 `#ifdef LOSCFG_FS_<NAME>` 内。
- [ ] 没有 `strcpy` / `strncpy` / `memcpy` / `memmove` / `memset` 直接调用——全部 `_s` 版本，且 `dstSize` 写正确。
- [ ] 没有 `kmalloc` / `kfree` / `mutex_lock` / `spin_lock` 等 Linux API 残留。
- [ ] 没有 GPL 头文件残留；版权头是华为 Device BSD-3-Clause。
- [ ] `BUILD.gn` 与 `Makefile` 同时更新；新源文件两边都列出。
- [ ] `fs/BUILD.gn` 与 `fs/Kconfig` 顶层接入。
- [ ] `make build` 在 `LOSCFG_FS_<NAME>=y` 下成功。
- [ ] 至少一个冒烟用例。
