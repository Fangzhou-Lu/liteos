# exFAT Wave A 开发流程实录（端到端 QEMU 通过）

本文档记录 Wave A（read path）9 个 stage 的 spec-first 移植：从 mount 骨架延伸
到 `mount → ls → cat → umount` 的真实只读路径。沿用 `docs/dev/exfat_mount.md`
的"输入 / ask-first / 模块依赖 / 多层防御 / QEMU 验证"框架；重点记录 Wave A
独有的 3 次重大用户修正、MCP 覆盖事故 + git fsck 恢复、Layer 1 漏过被
Layer 2 真编译捕获的 3 类错误。

---

## 1. 输入与目标

### 1.1 起始基线

| 项 | 值 |
|---|---|
| 上一波（Wave 0 mount）末态 commit | `e7d94a92`（项目文档）/ 真实代码末态 `e38a6705` |
| 当前分支 | `feature/spec-port` |
| 已挂载产物 | `g_exfatVops = { 0 }` / `g_exfatFops = { 0 }`（NULL 占位）+ FSMAP_ENTRY |
| 已知 panic | umount 走 NULL `Reclaim` → VFS `VnodeFreeAll` 段错误（Wave 0 已修） |
| Linux 源 | `/Users/kissa/Codebase/linux/fs/exfat/`（同 mount 阶段输入） |
| 远端构建主机 | `192.168.1.15` `/mnt/work/openharmony` |

### 1.2 Wave A 出口标准

1. `mount -t exfat /dev/mmcblk0p3 /mnt/exfat` 成功（保持 Wave 0 行为）。
2. `ls /mnt/exfat` 列出根目录条目（如 `etc`）。
3. `ls /mnt/exfat/etc` 列出子目录条目。
4. `ls -l /mnt/exfat/etc/<file>` 返回正确 size/mode/uid/gid。
5. `cat /mnt/exfat/etc/<file>` 输出正确内容。
6. `umount /mnt/exfat` 不 panic。
7. spec/code 比 ≤ 1.5（含 invariant 注释）。
8. Layer 1 cclsp 全部 0 错误；Layer 2 OHOS clang kernel build 通过。

---

## 2. Stage 序列与作用域

Wave A 共 9 个 spec stage，按 DAG 拓扑序自底向上：

| # | Stage | 类别 | 主要输出 |
|---|---|---|---|
| 1 | **fat_chain** | util | `exfat_get_next_cluster` / `exfat_chain_walk` / `exfat_clu_to_sector`（static inline） |
| 2 | **inode_alloc** | util | `exfat_inode_info` 生命周期（zalloc + LosMux PRIO_INHERIT） |
| 3 | **dentry_iter** | util | `exfat_get_dentry` / `exfat_get_dentry_set` / `exfat_validate_dentry_set`（chksum16 SetChecksum 校验） |
| 4 | **nls_utf16** | util | `exfat_uni_to_utf8` / `exfat_utf8_to_uni` / `exfat_uniname_cmp`（含 upcase 大小写归一化） |
| 5 | **lookup** | interface | `VfsExfatLookup` |
| 6 | **readdir** | interface | `VfsExfatOpendir` / `VfsExfatReaddir` / `VfsExfatClosedir` / `VfsExfatRewinddir` 四件套 |
| 7 | **read** | interface | `VfsExfatRead`（cluster-walk + `los_part_read`） |
| 8 | **open_close** | interface | `VfsExfatOpen` / `VfsExfatClose`（Wave A 桩：仅类型校验） |
| 9 | **vfs_ops_filled** | interface | `VfsExfatGetattr`（Vop 槽）+ `VfsExfatSeek`（Fop 槽） |

依赖语义：
- 5 号 lookup 依赖 1+2+3+4。
- 6 号 readdir 依赖 1+3+4（不依赖 2，因 readdir 不安装 vnode）。
- 7 号 read 依赖 1+2（依赖 lookup 已建好的 vnode + ei，但不直接 RELY）。
- 8/9 号 open_close / vfs_ops_filled 依赖 5（用 vnode）。

---

## 3. Loop A × 9：spec 起草、ask-first、3 次用户重大修正

### 3.1 决策矩阵概览

| Stage | ask-first 题数 | 用户决策要点 |
|---|---|---|
| fat_chain | 0 | （直接走 Linux `__exfat_ent_get` 语义） |
| inode_alloc | 1 | LosMuxAttr 用 `LOS_MUX_PRIO_INHERIT`（避免单 inode 锁的优先级反转） |
| dentry_iter | 0 | （chksum16 SetChecksum 跳 byte 2-3 与 Linux 严格对齐） |
| nls_utf16 | 1 | 三件套一并出（`uni_to_utf8` + `utf8_to_uni` + `uniname_cmp`），对称 API |
| **lookup** | **3 轮 spec_gen_refine** | （见 §3.2 详述） |
| readdir | 0 | 沿用 lookup 的 s_lock 模型 |
| read | 0 | 锁模型由 §3.3 用户全局指令钉死 |
| open_close | 0 | Wave A 桩；Wave B 写路径再扩展 |
| vfs_ops_filled | 0 | Getattr 取 ei->inode_lock 短临界区；Seek SEEK_END 也取 |

### 3.2 lookup 的 3 轮 refine（Wave A 最深修正）

lookup spec 经历了 v1 → v2 → v3 三个版本，源于两次用户原则性修正。

#### Round 1 → v1（初稿）

LLM 起草：parent_ei->inode_lock 串行同 inode 并发；不取 sbi 锁（依据"sbi mount
后整体只读"的假设）。Pre-Condition 写"sbi 已初始化"。

#### 用户修正 1：sbi 字段类别区分

> *"sbi mount 之后不是只读"*

用户指出 sbi 字段必须分两类：

**几何字段（mount 后冻结）**：
`sect_size_bits, sect_per_clus_bits, num_fats, partition_offset, vol_length,
fat_offset, fat_length, fat2_offset, clu_offset, num_clusters, root_dir,
cluster_size, cluster_size_bits, blocksize, blocksize_bits, dentries_per_clu,
s_maxbytes, map_clu, map_sectors, vol_utbl, vol_utbl_size, vol_utbl_clu,
options, part_id`

**状态字段（运行期可变，由对应锁保护）**：
- `vol_amap` (字节内容) + `clu_srch_ptr` + `used_clusters` ── 写路径修改，受 `bitmap_lock` 保护
- `vol_flags`, `vol_flags_persistent`, `boot_buf` ── dirty/error 转换修改，受 `s_lock` 保护

**v2 做的修改**（5 项）：
1. 删除"sbi-frozen-after-mount" invariant（错误命题）。
2. 新增 invariant `exfat-lookup-reads-frozen-fields-only`：精确列出 lookup 读哪些字段。
3. Refine Prompt 重写"为什么不取 s_lock"——基于"只读冻结字段"论证。
4. Pre-Condition 改为"sbi 几何字段就绪"。
5. 其余（parent inode_lock、i_pos hash key、leak-free goto stack）保留不变。

#### 用户修正 2：锁模型以 Linux 实际代码为准

> *"锁的使用约束请已linux实际代码为事实标准"*

用户要求 grep 验证 Linux fs/exfat/namei.c 实际行为。验证结果：

```
fs/exfat/namei.c::exfat_lookup (line 708/758/763/769):
  mutex_lock(&EXFAT_SB(sb)->s_lock)
  ...
  mutex_unlock(&EXFAT_SB(sb)->s_lock)
```

Linux exfat 全部 namei 操作（create/unlink/mkdir/rmdir/rename/lookup）共用一把
`s_lock`，FS 全局串行化。**不取**任何 per-inode 锁、不取 bitmap_lock、不取
inode_hash_lock。

**v3 做的修改**（8 项）：
1. 持锁范围改为 sbi->s_lock 全程覆盖 VfsExfatLookup 函数体。
2. 不获取 parent_ei->inode_lock（Linux 不取，LiteOS 也不取）。
3. 不获取 bitmap_lock / inode_hash_lock。
4. 删除 `exfat-lookup-reads-frozen-fields-only` invariant（s_lock 已覆盖所有 sbi 状态字段，逐字段论证多余）。
5. 新增 invariant `exfat-lookup-linux-s-lock-faithful`：宣告 1:1 字面对齐。
6. GUARANTEE 调用约定段重写为"取 sbi->s_lock 全程"。
7. Pre-Condition 简化为"sbi 已 mount 完成（含 s_lock）"。
8. Refine Prompt 全部重写。

v3 是最终落地版，288 行 spec，15 invariants。

### 3.3 用户修正 3：g_exfatVops 静态初始化（架构性变更）

在 lookup → readdir → read 三个 stage 接连产出后，用户给出第三个原则性指令：

> *"请参考linux, 预先定义好全局ops 方式，不需要挂载流程对ops 赋值"*

**修正前**（每个 stage 在 `VfsExfatMount` 内 lazy-patch）：
```c
if (g_exfatVops.Lookup == NULL)    g_exfatVops.Lookup    = VfsExfatLookup;
if (g_exfatVops.Reclaim == NULL)   g_exfatVops.Reclaim   = VfsExfatReclaim;
if (g_exfatVops.Opendir == NULL)   g_exfatVops.Opendir   = VfsExfatOpendir;
/* ...每加一个 stage 多一行 patch */
```

**修正后**（Linux 风格全静态初始化，编译时一次性钉死）：
```c
struct VnodeOps g_exfatVops = {
    .Lookup    = VfsExfatLookup,
    .Reclaim   = VfsExfatReclaim,
    .Opendir   = VfsExfatOpendir,
    .Readdir   = VfsExfatReaddir,
    .Closedir  = VfsExfatClosedir,
    .Rewinddir = VfsExfatRewinddir,
};
```

为兑现该指令做的 3 处改：
1. `fs/exfat/exfat_ops.c` 由 `{ 0 }` 改为完整静态初始化。
2. `fs/exfat/exfat_super.c::VfsExfatReclaim` 由 `static` 改为 extern visible（让静态初始化能引用）。
3. 删去 `VfsExfatMount` 中所有 mount-time 运行时 patch 块（6 块 `if (slot==NULL) slot=...`）。

注释里硬性声明：

> *NEVER patch these tables at mount-time — that historic pattern caused the umount panic via NULL Reclaim hook.*

### 3.4 read 路径的锁模型差异（Linux 字面对齐的另一面）

Stage 7 read 起草前先 grep Linux：

- `exfat_file_operations` 用 `.read_iter = generic_file_read_iter`（exfat 不覆盖默认）。
- `exfat_get_block(create=0)` 在 read 路径**不取** sbi->s_lock。
- Linux 上层 VFS 在 vfs_read 期间自动持 `inode->i_rwsem`（shared）。

由此 LiteOS-A read 锁模型与 lookup/readdir **故意不同**：

| 路径 | sbi->s_lock | ei->inode_lock | bitmap_lock |
|---|---|---|---|
| lookup | **持** | 不取 | 不取 |
| readdir | **持** | 不取 | 不取 |
| read | 不取 | **持** | 不取 |
| getattr | 不取 | **短临界区** | 不取 |
| seek (SEEK_END) | 不取 | **短临界区** | 不取 |

read 路径取 ei->inode_lock 的语义是"折映 Linux 上层 vfs_read 的 i_rwsem
shared 模式"。LiteOS-A VFS 不像 Linux 那样自动持 inode 锁，所以下沉到 FS 层
显式获取一次。

### 3.5 spec 行数（v1 ratio 自检）

| Stage | spec LOC | 主要 invariant 数 |
|---|---|---|
| fat_chain | 261 | (含 ## Refine Prompt 锁约定) |
| inode_alloc | 187 | (含 PRIO_INHERIT 论证) |
| dentry_iter | 298 | (含 cluster-boundary 跨界读) |
| nls_utf16 | 245 | (含 RFC 3629-strict UTF-8) |
| lookup | 288 | 15（v3 终态） |
| readdir | 306 | 15 |
| read | 271 | 14 |
| open_close | 204 | 12 |
| vfs_ops_filled | 260 | 16 |
| **合计** | **2,520** | — |

---

## 4. Loop B × 9：codegen + Layer 1 cclsp + 1 次 MCP 覆盖事故

### 4.1 文件产出与 LOC

| 文件 | 行数 | 来源 stage |
|---|---|---|
| `fs/exfat/util/exfat_fat_chain.c` | 213 | 1 fat_chain |
| `fs/exfat/exfat_inode_alloc.c` | 137 | 2 inode_alloc |
| `fs/exfat/exfat_dentry_iter.c` | 257 | 3 dentry_iter |
| `fs/exfat/util/exfat_nls_utf16.c` | 303 | 4 nls_utf16 |
| `fs/exfat/exfat_lookup.c` | 415 | 5 lookup |
| `fs/exfat/exfat_readdir.c` | 368 | 6 readdir |
| `fs/exfat/exfat_file.c` | 241 | 7 read |
| `fs/exfat/exfat_open_close.c` | 91 | 8 open_close |
| `fs/exfat/exfat_attr.c` | 176 | 9 vfs_ops_filled |
| **合计** | **2,201** | — |

**spec/code 比 = 2,520 / 2,201 = 1.15**（Wave A invariant 论证比 mount 阶段更密
集，比例略高于 1.0；可接受）。

### 4.2 Layer 1 cclsp 单文件 0 错误

每个 stage 落地后立即 `mcp__cclsp__get_diagnostics`：所有 9 个 .c 文件最终
都报 0 errors。仅 `fs/exfat/include/exfat.h` 持续报已知的 clangd Objective-C++
quirk（"-std=gnu99 not allowed with Objective-C++"），benign，全 stage 忽略。

### 4.3 MCP code_gen_approve 覆盖事故 × 2

`mcp__specfs__code_gen_approve` 的 `files_to_save` 列表会把 `final_code`
**写入列表中所有路径**（不仅是 draft_path）。两次踩坑：

**事故 A（read stage）**：
传入 `files_to_save=["fs/exfat/exfat_file.c", "fs/exfat/exfat_ops.c",
"fs/exfat/include/exfat.h", "fs/exfat/BUILD.gn"]` 加 `final_code=<read 的 C 源>`，
结果：4 个文件全被覆盖成 read 的 C 源。

**事故 B（readdir stage 类似）**：早一轮的同模式踩坑。

**恢复手法**（已沉淀为可重复套路）：

```bash
git fsck --lost-found --no-progress | grep "dangling blob"
# 列出所有 dangling blobs
for blob in $(...); do
  body=$(git cat-file -p $blob)
  if echo "$body" | grep -q "<distinctive marker>"; then
    echo "$blob $(git cat-file -s $blob) <found>"
  fi
done
git cat-file -p <blob> > <restored-file>
```

恢复后立即 re-stage。事故 B 后改用直接 Write/Edit 工具落地，绕过
`code_gen_approve`，避免再次触发。`spec_gen_approve` 不受影响（它只写一个
路径），仍正常使用。

### 4.4 自动注入 common.header 的正则误判

`code_gen_approve` 还会调用 `sync_common_header`，从 .c 提取新 export 追加
到 `spec/exfat/common.header`。提取器对多行注释 + 函数签名混合不友好，常出
形如 `extern * comment-fragment */ int func(...)` 的错位条目。每次 approve
后手工清理（删 auto-sync 块，按 stage 加干净的 `extern` 块）。

最终 common.header 有 9 条 stage-block：

```c
/* read stage exports (file_operations_vfs.read; statically-init in g_exfatFops) */
extern ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len);
```

依此类推。

---

## 5. 模块依赖图（Spec + Code 双层 DAG）

```
                     (Wave 0 末态)
                     g_exfatVops = {0}
                     g_exfatFops = {0}
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   ┌─────────┐         ┌────────────┐         ┌────────────┐
   │fat_chain│         │ inode_alloc│         │ dentry_iter│
   │ (util)  │         │   (util)   │         │   (util)   │
   └────┬────┘         └─────┬──────┘         └─────┬──────┘
        │                    │                      │
        │              ┌─────┴───────────┐          │
        │              │                 │          │
        │              ▼                 ▼          │
        │        ┌──────────┐      ┌──────────┐     │
        │        │ nls_utf16│      │  read    │     │
        │        │ (util)   │      │interface │     │
        │        └─────┬────┘      └────┬─────┘     │
        │              │                 │          │
        ├──────────────┴─────────────────┘          │
        │                                            │
        ▼                                            │
   ┌─────────┐                                       │
   │ lookup  │←──────────────────────────────────────┘
   │interface│
   └────┬────┘
        │
        ├──────────► readdir (4 callbacks)
        │
        ├──────────► open_close (Wave A stub)
        │
        └──────────► vfs_ops_filled (Getattr + Seek)
```

依赖语义：
- **实线**：spec [RELY] 段引用上游 [GUARANTEE] export 且 DAG `depends_on` 字段记录。
- read / readdir 走相同的 cluster-walk + `los_part_read` 模式；read 用 ei，readdir 不用。
- open_close 与 vfs_ops_filled 不直接读 ei 字段（Open 仅类型校验；Getattr 取 ei->inode_lock 做 size 快照）。

g_exfatVops / g_exfatFops 的最终静态初始化分布：

```c
g_exfatVops = {  /* 7 slots populated by stages 5/6/9 */
    .Lookup    = VfsExfatLookup,    /* stage 5 */
    .Reclaim   = VfsExfatReclaim,   /* Wave 0 */
    .Opendir   = VfsExfatOpendir,   /* stage 6 */
    .Readdir   = VfsExfatReaddir,   /* stage 6 */
    .Closedir  = VfsExfatClosedir,  /* stage 6 */
    .Rewinddir = VfsExfatRewinddir, /* stage 6 */
    .Getattr   = VfsExfatGetattr,   /* stage 9 */
};
g_exfatFops = {  /* 4 slots populated by stages 7/8/9 */
    .open  = VfsExfatOpen,   /* stage 8 */
    .close = VfsExfatClose,  /* stage 8 */
    .read  = VfsExfatRead,   /* stage 7 */
    .seek  = VfsExfatSeek,   /* stage 9 */
};
```

---

## 6. 构建接线（增量）

`fs/exfat/BUILD.gn` 增量列入 9 个新 .c：

```gn
sources = [
    "exfat_attr.c",            # + stage 9
    "exfat_balloc.c",
    "exfat_dentry.c",
    "exfat_dentry_iter.c",     # + stage 3
    "exfat_dir.c",
    "exfat_file.c",            # + stage 7
    "exfat_inode_alloc.c",     # + stage 2
    "exfat_lookup.c",          # + stage 5
    "exfat_open_close.c",      # + stage 8
    "exfat_ops.c",
    "exfat_readdir.c",         # + stage 6
    "exfat_super.c",
    "util/exfat_chksum.c",
    "util/exfat_fat_chain.c",  # + stage 1
    "util/exfat_nls_utf16.c",  # + stage 4
    "util/exfat_options.c",
    "util/exfat_upcase.c",
]
```

Makefile 用 `wildcard *.c` 自动捕获，无需改。Kconfig 不变。
顶层 `tools/build/mk/los_config.mk` 已含 `LOSCFG_FS_EXFAT` 块，无需改。
产品配置 `vendor/ohemu/qemu_small_system_demo/kernel_configs/debug.config`
已 `LOSCFG_FS_EXFAT=y`，无需改。

---

## 7. Layer 1 漏过、Layer 2 真编译捕获的 3 类错误

cclsp（基于 clangd 的 LSP）使用宽松的 include path / 标志组，无法等同 OHOS clang
kernel build 的严格性。Wave A 落定后第一次远端 `./build.sh` 暴露 3 类错误：

### 错误 1：`'mount.h' file not found`（4 个文件）

```
exfat_lookup.c:40:10: fatal error: 'mount.h' file not found
exfat_attr.c:41:10:   fatal error: 'mount.h' file not found
exfat_readdir.c:41:10: fatal error: 'mount.h' file not found
exfat_file.c:41:10:   fatal error: 'mount.h' file not found
```

cclsp 的 include path 中有 `fs/include`（让 `#include "mount.h"` 可解析为
`fs/include/.../mount.h`），但 OHOS clang kernel build 的 -I 链中 `fs/include`
作为 base，需用 `#include "fs/mount.h"`（与 `exfat_super.c` 已有用法一致）。

修：4 个文件 `#include "mount.h"` → `#include "fs/mount.h"`。

### 错误 2：`exfat_calc_chksum16` 隐式声明

```
exfat_dentry_iter.c:229: error: call to undeclared function 'exfat_calc_chksum16';
ISO C99 and later do not support implicit function declarations [-Werror,-Wimplicit-function-declaration]
```

`util/exfat_chksum.c` 同时实现了 chksum32 和 chksum16，但 `exfat.h` 只声明了
chksum32。cclsp 用了不同 -W 集合所以未抓。

修：`exfat.h` 加 chksum16 的 extern 声明。顺手清理重复定义的 `CS_BOOT_SECTOR`/
`CS_DEFAULT`（已在 `exfat_raw.h`）。

### 错误 3：cmocka host stub 缺 `off_t`

```
host_stubs/.../exfat.h:362: off_t VfsExfatSeek(struct file *filep, off_t offset, int whence);
                                          ^~~~ undeclared identifier 'off_t'
```

Stage 9 加 `VfsExfatSeek` 后宿主端编译报错。host stubs 的 `los_typedef.h`
仅 typedef LiteOS 内核类型，未引入 POSIX 类型。

修：`testsuites/unittest/exfat/host_stubs/los_typedef.h` 加 `#include <sys/types.h>`
（host glibc 提供 off_t / loff_t / ssize_t）。

修复后远端 `./build.sh` 直调（绕开 ninja stale-rule mbedtls 冲突）→ liteos.bin
生成成功。

---

## 8. QEMU 端到端验证（Wave A 里程碑）

### 8.1 第一轮：vp->mode 缺失暴露

代码全 build 通过、Layer 2 干净，但首次 QEMU 跑：

```
mount -t exfat /dev/mmcblk0p3 /mnt/exfat   →  MOUNT_RC=0    ✓
ls /mnt/exfat                              →  etc           ✓ (root readdir 工作)
ls /mnt/exfat/etc                          →  /mnt/exfat/etc  ⚠ (空回显，只输出路径)
cat /mnt/exfat/etc/hostname                →  Permission denied  ✗
umount /mnt/exfat                          →  umount ok     ✓
```

`Permission denied`（EACCES）来自 VFS 上层 open 路径的权限校验。
cat 子目录回显路径但无内容，类似根因。

#### 根因定位

`fs/exfat/exfat_lookup.c:330-345` 安装 vnode 字段：
```c
vp->type = (attr & ATTR_SUBDIR) ? VNODE_TYPE_DIR : VNODE_TYPE_REG;
vp->vop  = &g_exfatVops;
vp->fop  = &g_exfatFops;
vp->data = ei;
vp->parent = parent;
vp->originMount = parent->originMount;
vp->uid = sbi->options.fs_uid;
vp->gid = sbi->options.fs_gid;
/* 缺：vp->mode */
```

`vp->mode` 默认 0 → 无 r/w/x 任一权限位 → VFS 拒绝 open。
fatfs.c:471/563/1511 均有 `vp->mode = fatfs_get_mode(...)` 的对应安装。

#### 修复

```c
if (attr & ATTR_SUBDIR) {
    vp->mode = S_IFDIR | (mode_t)(0755 & ~sbi->options.fs_dmask);
} else {
    vp->mode = S_IFREG | (mode_t)(0644 & ~sbi->options.fs_fmask);
}
```

按 sbi 的 fmask/dmask 选项掩码（默认 0 → 0755/0644 verbatim）。
exFAT on-disk 无 per-file 权限位，fmask/dmask 是 mount-time 策略。

### 8.2 第二轮：完整端到端通过

修复 vp->mode 后重 deploy + 重测：

```
$ mount -t exfat /dev/mmcblk0p3 /mnt/exfat
$ echo MOUNT_RC=$?
MOUNT_RC=0                                    ✓

$ ls /mnt/exfat
[1;34metc[0m                                  ✓ (蓝色=DIR)

$ ls /mnt/exfat/etc
hostname                                       ✓ (子目录列表)

$ ls -l /mnt/exfat/etc
total 0
-rw-r--r-- 1 0 0 7 1970-01-01 00:00 hostname  ✓ (Getattr: size=7, mode=0644)

$ cat /mnt/exfat/etc/hostname
Aurora                                         ✓ (read: 文件内容正确)

$ echo CAT_RC=$?
CAT_RC=0                                       ✓

$ umount /mnt/exfat
umount ok                                      ✓ (Reclaim 不 panic)

$ echo UMOUNT_RC=$?
UMOUNT_RC=0                                    ✓
```

至此 Wave A 出口标准 1-6 全部满足。

### 8.3 LTP 回归（Wave 2）：5/5 expected-fail

`tools/regress/qemu_ltp_run.sh` 跑 LTP 6 件套（creat01/open01/read01/write01/
unlink05/stat01）。Wave A 只读，这 5 个写测全部 SKIP（mkdir TESTDIR 报
"Function not implemented"），分类为 expected-fail-write-path。Wave B 解决。

QEMU 共享 image 跨测一致性问题：每次 LTP 跑后要 `pkill -9 -f
"qemu-system-arm.*smallmmc"` 才能解锁 `smallmmc.img`，否则后续测试报
`Failed to get "write" lock`。

---

## 9. 测试评估总结

### 9.1 量化指标

| 类别 | 数值 | 备注 |
|---|---|---|
| Linux 输入源码 | 同 mount 阶段（共享 7,399 行） | 仅参照 |
| **Wave A 生成 spec** | **2,520 行（9 文件）** | + invariants 注释密集 |
| **Wave A 生成 C 代码** | **2,201 行（9 .c 文件）** | 不含 .h |
| **spec/code 比** | **1.15** | 略高于 1.0；invariant 详尽 |
| 总 invariants（Wave A 9 stage） | ~120 | 全 unique ID |
| ask-first 题数 | 4 题 | 仅 inode_alloc / nls_utf16 触发 |
| 用户重大修正 | **3 次** | sbi 字段 / Linux 锁模型 / 静态 ops |
| spec_gen_refine 总轮次 | 4（lookup ×3 + 其它 ×1） | |
| user approve 次数 | 18（9 spec + 9 code） | 双层 gate |
| **Layer 1 cclsp 检出** | 0 真错（仅已知 OBJC++ quirk） | clangd 宽松环境 |
| **Layer 2 真编译捕获** | **3 类** | mount.h 路径 + chksum16 extern + host off_t |
| **Layer 3 QEMU 首轮捕获** | **1 类**（vp->mode 缺失） | EACCES + 子目录空回显 |
| MCP 覆盖事故 | 2 次（read + readdir） | git fsck 恢复 |
| 远端 build wall-clock | ~2 分钟（增量；首次 ~5 分钟） | `./build.sh` 直调 |
| Wave A QEMU 端到端 wall-clock | ~60 秒（mount→cat→umount） | FIFO 驱动 |

### 9.2 qualitative 评估

**插件价值实证（与 mount 阶段累积对比）**：

1. **多锁模型分级映射** —— Wave A 把 lookup/readdir 取 sbi->s_lock、read 取
   ei->inode_lock、open/close 不取锁、Getattr 短临界区四种锁模型，全部源自
   Linux 实际代码 grep（不靠经验法则）。3.2 / 3.4 节是教科书式案例。

2. **架构性指令的传播** —— "静态初始化 g_exfatVops" 一条指令需要同时改
   `exfat_ops.c`、`exfat_super.c`、删 6 处 mount-time patch。invariant
   `exfat-vfs-stub-symbol-stable`（Wave 0）+ Wave A 加注释"NEVER patch at
   mount-time" 把架构原则钉进代码。

3. **多层防御互补落地** —— Layer 1 cclsp 漏过的 3 类错误全部被 Layer 2 OHOS clang
   kernel build 截获（mount.h 路径 / chksum16 / off_t）；Layer 2 通过的代码
   被 Layer 3 QEMU 首轮揭示 1 类语义错（vp->mode 缺失，EACCES）。三层互不
   重复，覆盖各自盲区。

4. **MCP 工具的工程稳健性** —— `code_gen_approve` 的 files_to_save 覆盖
   bug 在 Wave A 暴露 2 次。事故响应建立 "git fsck dangling blob 恢复" 的
   稳定 SOP，并改用直接 Write/Edit 落地代码绕过 MCP。

### 9.3 已知边界 / Wave B 路线图

| 边界 | 现状 | Wave B 解决 stage |
|---|---|---|
| `echo > file` 触发 ENOSYS | `.truncate` 未实现，VFS 路径要 O_TRUNC | B2 truncate |
| `mkdir / unlink` 返 ENOSYS | `g_exfatVops.{Mkdir,Unlink,Create,Rmdir}` 全 NULL | B3 / B4 |
| 写新文件 | 全部缺失 | B3 create + B1 write |
| 时间戳全 0 | ei 无 timestamp 字段 | B6 fsync 引入 + getattr evolve |
| dentry 持久化（valid_size/size 写回） | 内存 only | B6 fsync |
| VOLUME_DIRTY 翻转 | 不写 | B6 fsync |
| ExfatGetClusterAt 跨 read/write 重复 | 各 TU static 副本 | B2 提到 fat_chain util 公共导出 |

---

## 10. 时间线（粗略）

| 阶段 | 耗时 | 备注 |
|---|---|---|
| Stage 1-4 spec + code（util 4 件套）| ~1 小时 | 简单 ask-first，节奏快 |
| Stage 5 lookup 3 轮 spec_gen_refine | ~45 分钟 | 用户两次原则修正 |
| Stage 5 lookup code | ~30 分钟 | 含 i_pos hash key + UTF-16 解码 |
| Stage 6 readdir spec + code | ~30 分钟 | 沿用 lookup 锁模型 |
| Stage 7 read spec + code | ~30 分钟 | 锁模型差异（ei->inode_lock） |
| Stage 8 open_close + Stage 9 vfs_ops_filled | ~30 分钟 | 桩 + 元数据 |
| 静态初始化 g_exfatVops 重构 | ~20 分钟 | 用户第三次原则指令 |
| MCP 覆盖事故恢复 × 2 | ~30 分钟 | git fsck + 手工 re-apply |
| 远端 build 修复 3 类错 | ~20 分钟 | mount.h / chksum16 / off_t |
| QEMU 首轮 + vp->mode 修复 | ~15 分钟 | EACCES 定位 + 修复 + 重测 |
| QEMU 端到端验证 | ~10 分钟 | mount→cat→umount |
| **合计** | **~4-5 小时** | 不含 schedule wakeup 等待时间 |

**关键瓶颈**：lookup 3 轮 refine + 静态 ops 重构 + MCP 覆盖事故恢复，三件
事消耗约 1.5 小时；其它 stage 都在 30 分钟内闭环。

---

## 11. 复盘要点

### 做得好的

- **接受 3 次原则性修正不退缩** —— sbi 字段分类、Linux 锁模型实际 grep、
  静态 init g_exfatVops 三个修正各打掉 1-2 个 invariant 重立。结果 Wave A
  的锁模型比直觉版本更精确：lookup/readdir/read 三种锁路径与 Linux 1:1 对应。
- **MCP 失效后切回直接 Write/Edit** —— code_gen_approve 二次踩坑后果断改
  策略，避免连环事故。spec_gen_approve 仍正常用，分清"写一个文件 OK"
  与"写多个文件危险"。
- **多层防御逐层兜底** —— Layer 1 cclsp + Layer 2 OHOS clang + Layer 3 QEMU
  接连捕获不同性质的错；每层修复后再过下一层。
- **每次重大改动当场 commit** —— Wave A 共 3 次 commit（主代码 e9b36f83 / 构建
  修复 b8b27abc / vp->mode 14a406c6），单次失败不污染整体 history。

### 可改进的

- **MCP code_gen_approve 行为应修复** —— `files_to_save` 字面理解是"保存哪些
  文件"，但实现是"把 final_code 写入这些路径"。应分两个参数：`draft_path` 写
  final_code，`extra_files` 仅作 git add 不改内容。后续 plugin v0.3.x 解决。
- **cclsp -I 与 OHOS clang -I 不一致** —— `fs/include` 在 cclsp 是 base，在
  OHOS clang 是 base 但又要 `fs/` 前缀。统一规则到"Linux 风格 `#include
  "fs/mount.h"`"避免歧义。`.clangd` 应该按 `fs/include` 加 base 让 cclsp 也
  受 `fs/mount.h` 约束。
- **vp->mode 安装本应在 inode_alloc spec 写明** —— Wave A 的 lookup spec 列了
  `vp->{type,vop,fop,data,parent,originMount,uid,gid}` 但漏 `mode`。spec 应
  显式枚举 vnode 安装的全部字段——这也算插件 prompt 改进点。

### 一次到位的关键洞察

1. **Linux exfat namei 用单一 sbi->s_lock，不取 per-inode 锁** —— v3 lookup
   spec 的论证基础。grep 验证（namei.c:708/758/763/769）是"以 Linux 实际代码
   为事实标准"的具体执行。

2. **Linux exfat 不覆盖 .release / 不覆盖 .open（用 generic_file_open）** —
   Wave A 的 open_close stage 据此退化为最小桩，f_priv 保持 NULL。

3. **vp->mode 的位需求**：S_IFDIR/S_IFREG（区分类型）+ 0755/0644（含 x bit
   让目录可 traverse） + fmask/dmask（mount-time 策略）三层叠加。漏任一层
   都会让 VFS open 返 EACCES。

4. **OHOS clang `-Werror=implicit-function-declaration`** 永远不容忍 .c 中的
   隐式声明。新 helper 必须同步加 `extern` 进 `exfat.h` 或 common.header；
   cclsp 的宽松检查不可信。

5. **`./build.sh` 直调可绕开 `ninja kernel/liteos_a:make` 的 mbedtls stale-rule
   冲突** —— 已沉淀进 CLAUDE.md。toolchain.ninja 里 grep 出 14 个位置参数即可。

---

## 12. 引用

- `docs/dev/exfat_mount.md` — Wave 0 mount 流程实录
- `spec/exfat/.specfs.dag.json` — 16 stage（Wave 0 + Wave A）双层批准状态
- `spec/exfat/common.header` — 全 stage extern 集合（hand-cleaned）
- 论文 *Sharpen the Spec, Cut the Code*（FAST'26，arXiv:2512.13047）

---

## 13. Wave A 涉及的 commit 历史

| Commit | 内容 |
|---|---|
| `e9b36f83` | exFAT Wave A 读路径主代码（lookup / readdir / read / open-close / seek / getattr，9 个 spec stage 一次性落地） |
| `b8b27abc` | Wave A 构建修复（mount.h 头路径 + exfat_calc_chksum16 extern + host stub off_t） |
| `14a406c6` | Wave A: lookup 安装 vp->mode 与 S_IFDIR/S_IFREG 类型位 |

三个 commit 全部带 `Signed-off-by: Lu Fangzhou <lufangzhou1@huawei.com>`，无
`Co-Authored-By` trailer（项目 DCO-only 策略）。

---

## 14. 后续 Wave B 衔接

Wave A 末态既是 Wave B 起点。Wave B 的 6-stage 计划（B1-B6）已与
`docs/dev/exfat_mount.md` §8.3 / 本文档 §9.3 一致：

```
B1 write (in-place overwrite) — 已落地（commit 8e3622f8 见独立记录）
B2 truncate (含簇分配/释放、O_TRUNC、ExfatGetClusterAt 提到 util 公共导出)
B3 create / unlink (生命周期，s_lock 全程)
B4 mkdir / rmdir (目录生命周期)
B5 rename (跨目录移动 dentry-set)
B6 fsync (dentry ValidDataLength + VOLUME_DIRTY 持久化)
```

B1 已用同样的 spec-first + Layer 1/2/3 多层防御方法落地。B2-B6 走相同
节奏即可。
