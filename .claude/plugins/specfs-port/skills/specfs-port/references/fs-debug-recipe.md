# FS bug 调试参考——硬约束 + 维测日志方法 + Reclaim hook 教训

补充 SKILL.md §FS 调试硬约束。本文给具体的诊断流程、典型 bug 案例与教训。

## 硬约束：不动公共层（用户明确指令）

修 FS bug 时**坚守 fs/<name>/ 内部**。`fs/vfs/`、`kernel/`、`syscall/`、
`drivers/block/disk/` 是公共层——**禁止**未经用户许可改这些路径。

理由：
- 其它 FS（fat、jffs2）能跑就证明公共层是 OK 的；
- 公共层改动影响所有 FS，回归面积大、风险不可控；
- 真正的 bug 一定在新加的 FS 自己（初始化顺序、cleanup 顺序、callback 装填）。

只有两种情况能动公共层：
1. 用户明确说"可以改 vfs"；
2. 已经 reproduce 出公共层确实是 bug 的硬证据（同样代码模式 fatfs 也崩）。

## 维测日志驱动的诊断流程

1. **加临时 PRINT_ERR**：在 fs/<name>/ 里关键路径打印字段值。例：
 ```c
 PRINT_ERR("[<NAME>_DBG] mount-end vp=%p ppc.pstNext=%p vop=%p data=%p\n",
 vp, vp->parentPathCaches.pstNext, vp->vop, vp->data);
 PRINT_ERR("[<NAME>_DBG] umount-entry root=%p useCount=%d\n",
 root, root->useCount);
 ```
2. **跑 QEMU 抓串口日志**，反推哪一步把字段写坏了。
3. **对照 fatfs**：`fs/fat/os_adapt/fatfs.c` 是 LiteOS-A 自带的最完整 FS，作为修复参照。
 常用 grep 模式：
 ```bash
 grep -n "VnodeAlloc\|VfsHashInsert\|MountOps\|Reclaim" fs/fat/os_adapt/fatfs.c
 ```
4. **定位完毕后立即删除维测日志**——临时 print 不要 commit。
5. **重 build + 重跑**确认 fix。

## 教训 #1：`g_<fs>Vops = { 0 }` 全 NULL 在 umount 时 panic

**症状**：`umount /mnt/<name>` 触发 `data_abort fsr:0x5, far:0x00000004`。
backtrace：`SysUmount → umount → VnodeFree → VnodePathCacheFree (PC at offset 0x30, NULL+4 deref)`。

**真因**：`g_<fs>Vops.Reclaim` 是 NULL → VFS 框架的 `VnodeFreeAll → VnodeFree`
在 `VnodePathCacheFree` 走 path_cache 链表时崩。

**注意**：表面看是 path_cache 的 list head 没 init，但其实 VnodeAlloc 已 init 过了。
真正的关联：vop->Reclaim 是 vnode 生命周期里 VFS 期望的"FS-private 数据释放钩子"，
没装会让 VFS 的 vnode 复用/释放路径出现一个不一致状态，最终在 path_cache 的清理
代码里崩。

**修复**：
```c
// fs/<name>/<name>_super.c
static int Vfs<Name>Reclaim(struct Vnode *vnode) {
    <name>_inode_info *ei = (<name>_inode_info *)vnode->data;
    if (ei != NULL) {
    (void)LOS_MuxDestroy(&ei->inode_lock);
    LOS_MemFree(m_aucSysMem0, ei);
    vnode->data = NULL;
    }
    return 0;
}

// In Vfs<Name>Mount, BEFORE VnodeAlloc:
if (g_<name>Vops.Reclaim == NULL) {
    g_<name>Vops.Reclaim = Vfs<Name>Reclaim;
}
```

Lazy-init 是为了不依赖 LOS_MODULE_INIT 顺序 + 不需要单独的 InitOps 函数。
幂等（NULL check）保证多 mount 实例不会重写。

## 教训 #2：FS Unmount 不可触碰 root vnode

**反面**（错的）：
```c
static int Vfs<Name>Unmount(struct Mount *m, struct Vnode **blkdriver) {
    ...
    VnodeFree(root); // ← 双重释放！VFS 后续的 VnodeFreeAll 还要再 Free 一次
    ...
}
```

**正面**（对的，照抄 fatfs_umount）：
```c
static int Vfs<Name>Unmount(struct Mount *mount, struct Vnode **blkdriver) {
    /* Step 1: capture block device vnode BEFORE any frees */
    los_part *part = get_part(sbi->part_id);
    struct Vnode *device = (part != NULL) ? part->dev : NULL;

    /* Step 2: close block device via *blkdriver */
    if (blkdriver != NULL && *blkdriver != NULL) {
    struct drv_data *dd = (*blkdriver)->data;
    if (dd && dd->ops && dd->ops->close) {
    dd->ops->close(*blkdriver);
    }
    }

    /* Step 3: do NOT touch root vnode — VFS reaps it via VnodeFreeAll → Reclaim */

    /* Step 4: release sbi resources (boot_buf, vol_amap, vol_utbl, locks) */
    ...

    /* Step 5: free part_name to allow remount */
    if (part != NULL && part->part_name != NULL) {
    free(part->part_name);
    part->part_name = NULL;
    }

    /* Step 6: free sbi, clear mount fields */
    LOS_MemFree(m_aucSysMem0, sbi);
    mount->data = NULL;
    mount->vnodeCovered = NULL;

    /* Step 7: hand block device vnode back */
    if (blkdriver != NULL) *blkdriver = device;
    return 0;
}
```

关键点：FS Unmount 只释放 FS-private 资源（sbi、boot_buf、vol_amap 等），
**不**碰任何 vnode 对象——VFS 框架自己负责 vnode 生命周期。

## 教训 #3：bootargs 必须是真 NUL 字节

`printf '%s\0'`（单引号）在 bash/zsh 都正确写出末尾 NUL。
**不要**用 `printf '\\0'`（双反斜杠）—— printf 把 `\\` 解为字面反斜杠，`0` 当字面 0，
最终落盘是两字节 `\0`（反斜杠 + 数字 0），内核解析 bootargs 失败。

不放心就用 `perl -e 'print chr(0)'` 写 NUL。

## 验证：4 轮 mount/umount 测试模板

修完 umount 路径，必须跑 4 轮验证（mount → umount → 重 mount → 重 umount）：
```bash
mount -t <name> /dev/mmcblk0p3 /mnt/<name>; echo MOUNT1_RC=$?
umount /mnt/<name>; echo UMOUNT1_RC=$?
mount -t <name> /dev/mmcblk0p3 /mnt/<name>; echo MOUNT2_RC=$?
umount /mnt/<name>; echo UMOUNT2_RC=$?
```

四个 RC 全 0 + 0 panic 才算修好。第 2 轮重 mount 检验 part_name 是否在第 1 轮 umount
里清掉了——清掉才能复用。

## 把无符号 panic dump 还原成有函数符号的调用栈

QEMU 串口日志里的 panic 长这样（出自 `arch/arm/arm/src/los_exc.c` 的
`PrintExcInfo`）：

```
[ERR] excType: data_abort
task kernel stack = 0x40012000 -> 0x40014000
pc = 0x401234ac
klr = 0x40129b04
ksp = 0x40013ee0
fp = 0x40013ec0
traceback 1 -- lr = 0x40129b04 fp = 0x40013ee0
traceback 2 -- lr = 0x4011a8d4 fp = 0x40013ed0
traceback 3 -- lr = 0x4010014c
```

光看地址定位不到函数。`tools/regress/symbolicate_panic.py` 解决这件事：

```bash
# 远程构建机器上的 OH 工具链路径
TC=/mnt/work/openharmony/prebuilts/clang/ohos/linux-x86_64/llvm/bin
SRC=/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo

# 推荐：始终带 --symfile，避免 ELF rebuild 后地址漂移
python3 tools/regress/symbolicate_panic.py \
    --log /tmp/qemu_serial.log \
    --image $SRC/OHOS_Image \
    --addr2line $TC/llvm-addr2line \
    --objdump $TC/llvm-objdump \
    --symfile $SRC/OHOS_Image.sym.sorted
```

> Arch Linux 远程机器上 distro 包不带 `llvm-addr2line`，必须用 OH 内置工具链
> （路径如上），或 `pacman -S llvm` 装一遍。

真实输出（截自实测的 umount-Reclaim panic，`/tmp/qemu_serial.log`）：

```
# kernel .text VMA from ELF: 0x40001000
# using --symfile OHOS_Image.sym.sorted for symbol resolution (3335 text symbols loaded)

==============================================================================
Symbolicated callstack (6 kernel frame(s))
==============================================================================
#01 0x400293ec (pc)
    → SysPread64 at ??:0 ← addr2line 给的是错的（ELF 不匹配）
    [sym] VnodePathCacheFree+0x30 ← .sym.sorted 给的是对的
#02 0x4002be54 (klr)
    → LOS_DoExecveFile at ??:0
    [sym] VnodeFree+0x3c
#03 0x4002f7b8 (lr)
    [sym] umount+0xec
#04 0x40097de8 (lr)
    [sym] SysUmount+0x68
#05 0x4009d9d0 (lr)
    [sym] OsArmA32SyscallHandle+0xa4
#06 0x400013bc (lr)
    [sym] _osExceptDispatch
```

可读性立刻拉满：本次 panic 是 `SysUmount → umount → VnodeFree(+0x3c) →
VnodePathCacheFree(+0x30)`，PC=0x400293ec NULL+4 解引用——正是
`g_<fs>Vops.Reclaim == NULL` 这个 bug 的栈轨迹（教训 #1 的真实出处）。

### **重要：日志与 ELF 必须同源**

addr2line / nm 都依赖 ELF 里的地址→符号映射，**rebuild 一次就漂一次**。
本节示例里 ELF 已经在 panic 之后被重新构建过，所以纯 addr2line 输出
（`SysPread64`、`LOS_DoExecveFile` 等）完全是噪音；只有走 `--symfile` 指向
panic 时刻保存下来的 `.sym.sorted` 才得到正确的栈。

**实践建议**：

1. 每次 QEMU 跑出 panic 立刻用 `cp` 把 `OHOS_Image{,.bin,.map,.sym.sorted}`
 存档到一个时间戳目录（如 `/tmp/dump_<ts>/`）。
2. `tools/regress/qemu_ltp_run.sh` 在检测到 `panic | Oops | Kernel BUG` 后应在
 kill QEMU 的同时归档当前 ELF 与 sym 表（roadmap 改进项，详见 `../../CHANGELOG.md`）。
3. 后期符号化时优先使用 `--symfile <archived sym.sorted>`；只有当确认 ELF 没动
 过、`stat -c %y OHOS_Image` 比 panic 时间戳早，才直接吃 ELF。

### 工作机制

1. 用 `objdump -h` 读 ELF 的 `.text` VMA（LiteOS-A arm_virt 一般是 `0x40000000`）
 作为合理性下限，过滤掉用户态地址（< 0x40000000）。
2. 抽取所有 `pc/klr/ulr/lr = 0x...` 与 `traceback N -- lr = 0x...` 命中。
 `fp/sp/ksp/usp` **不**抽取——它们是数据指针不是代码指针，addr2line 上去给的
 是噪声。
3. 一行只取一个地址，避免把 `traceback N -- lr = 0x... fp = 0x...` 同一行里的
 `fp` 当成额外帧。
4. 批量喂 `llvm-addr2line -C -f -i -p -e <ELF> 0xADDR ...`，inline 帧会以 ` (inlined by) ...`
 形式跟在主帧后面。
5. 输出按"出现顺序"编号（从 `pc` 起，到栈顶最远的 `lr` 为止）。

### 前置条件

- **kernel ELF 必须带 DWARF**——LiteOS-A debug build 默认带 (`-g`)，release 别用
 这工具。
- **toolchain**——脚本默认找 `llvm-addr2line`，找不到再降级到 `addr2line` /
 `arm-linux-gnueabi-addr2line`。强制用 GCC 系：`--toolchain gcc`；显式指定路径：
 `--addr2line /path/to/llvm-addr2line`。
- **load address**——LiteOS-A 不做 KASLR，链接地址即运行地址，绝大多数情况下
 不需要 `--load-addr`。仅当你确实做了重定位时传 `--load-addr 0xRUNTIME_BASE`。

### 与 QEMU 测试 loop 的衔接

把脚本接到 `qemu_ltp_run.sh` 的 panic 分支里：检测到 `panic | Oops | Kernel BUG`
立刻 `kill QEMU` 并自动 symbolicate，把符号化后的栈写到 `docs/exfat_regression_<ts>.md`。
这样 fail 报告里直接是函数名而不是裸地址，无需事后人肉 addr2line。

### 与上游 parse_excinfo.py 的差别

`tools/scripts/parse_exc/parse_excinfo.py` 是上游的 Python 2 工具，依赖已废弃的
`commands` 模块，且只解析 ExcInfo 段（不识别 OsBackTrace / 自定义 PRINT_ERR 里的
裸地址）。本脚本 Python 3、零依赖、识别面更广，专门给 FS 移植期间的快速诊断用。

## 内核-only 快速重 build

只改了 `kernel/liteos_a/**` 时不要跑全量 `hb build`（耗时 2-4 min）。直接调
`kernel/liteos_a/build.sh` + GN ACTION 的 14 参数。完整调用见
`~/.claude/projects/.../memory/feedback_kernel_only_build.md`，典型耗时 30-60 秒。

build 完后 cp `obj/kernel/liteos_a/make_out/liteos{,.bin,.map}` 到
`out/arm_virt/qemu_small_system_demo/OHOS_Image{,.bin,.map}`。
