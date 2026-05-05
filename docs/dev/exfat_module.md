# exFAT Wave B Module 层开发流程实录（FAT / bitmap / vol_flags 写路径基础）

本文档记录 Wave B 中**底层 util 模块**的 4 个 stage：把 Linux exFAT 的 FAT 表项
写、卷脏标志维护、簇释放、簇分配 4 件套移植到 LiteOS-A。这些是后续 truncate /
write / unlink / mkdir 的共同前置——任何"修改盘面"的 stage 都先经由这一层。

沿用 `docs/dev/exfat_mount.md` / `docs/dev/exfat_read.md` 的"输入 / ask-first /
模块依赖 / 多层防御 / cmocka 验证"框架；Wave B 独有的关注点：**写路径要求
显式 vol_flags 边界 + FAT2 镜像一致性 + bitmap_lock vs inode_lock 锁序**——
读路径完全无需考虑这些。

---

## 1. 输入与目标

### 1.1 起始基线

| 项 | 值 |
|---|---|
| 上一波末态 | Wave A 结束（read 路径 mount → ls → cat → umount 全过）|
| 已就绪 helpers | `exfat_get_next_cluster` / `exfat_chain_walk` / `exfat_get_dentry*` / `exfat_calc_chksum16` / `exfat_inode_alloc/_free` / NLS UTF-16 转换 |
| 已就绪 Vnode/Fop 槽 | 9+5 个：Lookup / Reclaim / Readdir 4 件套 / Getattr / read / open / close / seek（Wave B Stage 1 已加 write）|
| 当前分支 | `feature/spec-port` |
| Linux 源 | `/Users/kissa/Codebase/linux/fs/exfat/`（同 Wave 0/A 输入）|
| 远端构建主机 | `192.168.1.15` `/mnt/work/openharmony` |
| 回归套件 | `tools/regress/run_all.sh`（Wave 1 cmocka 全套 + Wave 2 LTP smoke） |

### 1.2 Module 层出口标准

1. 4 个 helper 全部 spec_approved + code_approved，DAG 节点登记。
2. 每个 helper 至少 ≥10 cmocka testpoint，含正/负/边界三类。
3. 全套 cmocka 0 failure（含原 Wave A 套件不退化）。
4. Layer 1 cclsp + Layer 2 OHOS clang kernel build 同时通过。
5. **关键 invariant**：FAT1/FAT2 字节级一致；bitmap 与 FAT chain 双向一致；
   vol_flags 翻转必须 round-trip 持久化到 boot sector。

---

## 2. Stage 序列与依赖

Module 层共 4 个 spec stage，按拓扑序自底向上：

| # | Stage | Linux 对照 | 主要输出 |
|---|---|---|---|
| 1 | **vol_flags** | `super.c::exfat_set_vol_flags` | 静态 helper `exfat_set_vol_flags` + 公开 `exfat_set_volume_dirty` / `exfat_clear_volume_dirty` |
| 2 | **ent_set** | `fatent.c::exfat_ent_set` | `exfat_ent_set(sbi, loc, value)`（含 FAT2 镜像写） |
| 3 | **free_cluster** | `fatent.c::__exfat_free_cluster` + `balloc.c::exfat_clear_bitmap` | `exfat_free_cluster(sbi, *p_chain)` + `exfat_clear_bitmap` |
| 4 | **alloc_cluster** | `balloc.c::exfat_find_free_bitmap` + `exfat_set_bitmap` + `fatent.c::exfat_alloc_cluster` | `exfat_alloc_cluster(sbi, n, *out)` + `exfat_set_bitmap` + `exfat_find_free_bitmap` |

依赖关系：
- `ent_set` 依赖 mount 装填的 `sbi->fat_offset` / `fat2_offset` / `num_fats`。
- `free_cluster` / `alloc_cluster` 依赖 `ent_set`（写 FAT 链）+ vol_flags（边界翻转）+ mount 装填的 `sbi->vol_amap` 位图。
- 三者**不**依赖 fat_chain helper 的 chain_walk visitor——直接用 ent_set 改 FAT 表。

---

## 3. Loop A × 4：ask-first 决策与 spec 设计

### 3.1 vol_flags — Wave B Stage 2a（vol_flags 分支）

**关键 ask-first 题**：
- **Q1：vol_flags 的回写策略？**
  选项：(a) 仅改内存 / (b) **改内存 + 写 boot sector** ← 用户选 / (c) 改内存 +
  defer 到 sync 时统一回写
  - 答 (b) 的代价：每次 set_volume_dirty/clear_volume_dirty 都会触发一次 sector
    write。优势：crash recovery 一致——重启 mount 看到的 dirty bit 反映了崩溃前
    的真实状态。这是 Linux 上游同款选择。

- **Q2：合并保留位 (`VOLUME_DIRTY | MEDIA_FAILURE`)？**
  选项：(a) 保留 / (b) 覆盖
  - 答 (a)：要把 `sbi->vol_flags_persistent` 中保留位 OR 进 new_flags 再写。
    防止 set_volume_dirty 把别的进程刚标的 MEDIA_FAILURE 抹掉。

**关键 invariant**：
- `exfat-vol-flags-persistent-merge`：每次写 boot sector 必须先合并保留位。
- `exfat-vol-flags-no-write-on-noop`：内存值已经是 new_flags 时**不**触发 IO（避免无意义的 boot sector 写带来寿命与冲突）。
- `exfat-vol-flags-bracketed`：`set_volume_dirty` 与 `clear_volume_dirty` 必须成对出现，且围着真正的盘面修改窗口。caller 责任。

**spec/code 数据**：spec 138 行 / code ~80 行（含 boot_buf 直写 + part_write
1 sector）。比例 1.7（高于平均，因为 vol_flags 分支需要描述合并保留位的位运算）。

### 3.2 ent_set — Wave B Stage 2a（ent_set 分支）

**关键 ask-first 题**：
- **Q1：FAT2 镜像 byte-exact 复用 buffer 还是重新编码？**
  选项：(a) **复用 FAT1 写入的 buffer 写 FAT2** ← 用户选 / (b) 独立 LE32
  encode 两次
  - 答 (a)：FAT1 和 FAT2 必须**逐字节一致**——这是 Linux exfat_mirror_bh 的
    本质约束。两次独立编码若 LE32 转换出现差异（理论上不会，但属于无谓复杂度）
    会破坏镜像不变量。Invariant `exfat-ent-set-mirror-byte-exact`。

- **Q2：value 范围校验是否含 EXFAT_BAD_CLUSTER？**
  选项：(a) 接受 / (b) **拒绝（Wave B 不引入坏块标记）** ← 用户选
  - 答 (b)：Wave B 暂不引入坏块标记路径，避免误用。Invariant
    `exfat-ent-set-rejects-bad-cluster`。后续如需引入，独立 stage evolve。

**关键 invariant**：
- `exfat-ent-set-no-locks`：函数自身不取锁；caller 持 inode_lock 或 bitmap_lock。
- `exfat-ent-set-sector-aligned-io`：FAT 表项 4 字节、扇区 ≥ 512、blocksize 是 2^k → 表项不会跨扇区，每次 ≤ 2 sector IO。
- `exfat-ent-set-validates-value-range`：value ∈ {EOF, FREE} ∪ [FIRST, num_clusters)，其它一律 -EINVAL。

**踩坑**：Linux `__exfat_ent_get` 把范围校验放在**读端**，写端不做。本 spec
显式把校验**对称到写端**——理由：读到脏值能在 chain_walk 处发现，但若
脏值是 ent_set 自己写的，就来不及拦截了。这是规范化把 Linux 习惯改造成
LiteOS 行为的一例。

**spec/code 数据**：spec 248 行 / code ~95 行。比例 2.6（spec 大量篇幅在
锁矩阵 + 7 个 Case + Refine Prompt 的两节）。

### 3.3 free_cluster — Wave B Stage 2b

**关键 ask-first 题**：
- **Q1：FAT 链遍历策略——chain walk 还是逐 FAT 读？**
  选项：(a) **逐 FAT 读（在 ent_set 之上循环）** ← 用户选 / (b) 用
  exfat_chain_walk visitor
  - 答 (a)：visitor 的 invariant 禁止内嵌 chain_walk 递归调用；free 路径
    要 ent_set 写入新值，会破坏 visitor 的"读端纯函数"语义。简化：自己开
    while 循环，每步先 ent_get 取下一个，再 ent_set 把当前置 FREE，最后
    清 bitmap 位。

- **Q2：部分释放失败的回滚？**
  选项：(a) 回滚 / (b) **不回滚，置 vol_flags VOLUME_DIRTY 让 mount fsck 修** ← 用户选
  - 答 (b)：v1 不引入事务/回滚——这要求 pre-image 缓冲，复杂度高。
    Linux 也没回滚，依赖 fsck.exfat。Invariant `exfat-free-cluster-no-rollback`。

**关键 invariant**：
- `exfat-free-cluster-fat-then-bitmap`：先 ent_set FAT 表项 = FREE，再清 bitmap 位。**绝不**反过来。
  - 如果先清 bitmap，crash 后 FAT 链仍连着但 bitmap 已 free → 下次 alloc
    会拿到这个簇，造成 cross-link（两个文件的 chain 指向同一个簇）。最严重的
    数据损坏类型。
  - 先清 FAT 链，crash 后 bitmap 还占着但链已断 → orphan cluster，磁盘空间
    "丢失"但无数据损坏。fsck 可以修。

- `exfat-free-cluster-bounded`：簇链长度 ≤ sbi->num_clusters，循环上界由该值
  保护，防御损坏链导致死循环。

**spec/code 数据**：spec 182 行 / code ~120 行。比例 1.5。

### 3.4 alloc_cluster — Wave B Stage 2c

**关键 ask-first 题**：
- **Q1：分配策略——last_alloc rotate 还是 from-zero scan？**
  选项：(a) **last_alloc rotate（Linux 同款）** ← 用户选 / (b) from-zero
  - 答 (a)：Linux 用 `sbi->last_alloc + 1` 起点扫描 bitmap，命中即返回。
    优点：减少 bitmap fragmentation 的局部性。缺点：第一次 scan miss 后要
    回到 0 重头扫，有"两段扫"的复杂度。本 spec 显式用 invariant
    `exfat-alloc-cluster-two-pass-scan` 钉住该模式。

- **Q2：分配 N 簇是否要求物理连续？**
  选项：(a) 必须连续 → 失败立即 -ENOSPC / (b) **尽量连续，连续不到时回退到非连续 (FAT chain)** ← 用户选
  - 答 (b)：默认非连续，省得空间稍碎就 -ENOSPC。Invariant
    `exfat-alloc-cluster-prefer-contiguous-then-fallback`：先尝试找连续 N，
    找不到就放宽到任意 FAT chain。flags 字段反映哪种。

**关键 invariant**：
- `exfat-alloc-cluster-bitmap-then-fat`：先在 bitmap 标记 in-use，再写 FAT 链
  把新分配的簇接到既有 chain 末端。**与 free 反向**。理由对称：bitmap 占了但
  FAT 没接 → orphan（fsck 修）；FAT 接了但 bitmap 没占 → 下次 alloc 还能
  挑到同一簇 → cross-link。

- `exfat-alloc-cluster-vol-dirty-bracketed`：分配本身被 caller 的 set_volume_dirty
  / clear_volume_dirty 包围（Stage 2a 已建立的边界），保证 crash 后 mount
  fsck 看到的状态是一致的。

**spec/code 数据**：spec 199 行 / code ~140 行。比例 1.4。

---

## 4. 跨 Stage invariant：写路径锁矩阵

Wave B 三类锁，必须严守层级（一致继承自 Linux 上游的同款层级）：

```
sbi->s_lock     ← rename / lookup / 跨目录大操作（粒度最大）
ei->inode_lock  ← truncate / write / fsync 同 inode 内串行
sbi->bitmap_lock ← alloc_cluster / free_cluster bitmap 修改同步
```

**禁忌组合**：
- 持 spinlock 调任何 helper：所有 helper 都含 LOS_MemAlloc 或 los_part_*。
  Invariant `*-no-spinlock-callsite` 在每个 spec 显式重申。
- bitmap_lock 上面套 inode_lock（逆序）：要同时改 FAT chain 和 bitmap 时，
  正确顺序是先 inode_lock 再 bitmap_lock，**不**是反过来。
- 在 ent_set 的 FAT1/FAT2 之间释锁：会让另一个线程在 FAT1 已写但 FAT2 未写
  的窗口看到镜像不一致。Refine Prompt 显式禁止。

caller 持锁映射（Refine Prompt 表格）：

| 调用方路径 | 持锁 |
|---|---|
| truncate-shrink / fsync | inode_lock |
| alloc_cluster / free_cluster 内部 | bitmap_lock |
| Wave B 后续 rename | s_lock |
| 直接 ent_set / vol_flags 测试 | 无（host LosMux nop） |

---

## 5. 多层防御反馈

### 5.1 Layer 1 cclsp（编辑期）

每个 stage 提交前 `clangd --check fs/exfat/exfat_*.c → 0 errors`。
**Wave B 新加要点**：

- 卷 boot_buf 直接 splice 写要求 `sizeof(uint16_t)` 对 `*((uint16_t *)(buf + 106))`
  的 unaligned access。clang ARM 默认对 `__attribute__((packed))` 生成 bytewise
  load；非 packed 路径要 memcpy_s 显式做。Layer 1 不抓，但 Layer 2 ARM build
  会触发 `-Wcast-align`——已加 `-Wno-cast-align` 否则 boot sector 直 splice 会噪。

### 5.2 Layer 2 OHOS clang kernel build

- ent_set 的 FAT2 镜像写：在 Linux 用 `sb_bread`/`exfat_mirror_bh`，本端用
  `los_part_write` 直接写另一个 sector_lba。**编译时**确认 `sbi->fat2_offset`
  在 num_fats==1 时被 mount 期填成 `fat_offset` 同值——避免 Wave B 跑 num_fats==1
  卷时第二次 part_write 写到错误扇区。
- alloc_cluster 含 `last_alloc` 字段读写——它是 sbi 的可写状态，多 alloc
  并发会 race。bitmap_lock 已覆盖；但 spec 显式 reaffirm，避免后续 stage
  误以为它是只读字段。

### 5.3 Layer 5 cmocka

| Stage | testpoints | 关键覆盖 |
|---|---|---|
| vol_flags | 18 | persistent-merge / no-write-on-noop / boot_buf round-trip |
| ent_set | 22 | FAT1+FAT2 byte-exact / EOF / FREE / out-of-range value rejected |
| free_cluster | 26 | fat-then-bitmap 顺序 / partial failure / chain bounds |
| alloc_cluster | 24 | last_alloc rotate / two-pass scan / prefer-contiguous fallback |

全套 cmocka 跑完合计 **190+ testpoints**（Wave 0/A 老套件 + Wave B 4 新套件），
0 failure。

---

## 6. 公共层桥接：kernel sysconf 补丁（独立 commit）

Wave B 同期发现 LiteOS-A musl `sysconf` 路由是 `return syscall(SYS_sysconf, name)`
（third_party/musl/porting/liteos_a/user/src/conf/sysconf.c），整个 constant→value
映射在 kernel `compat/posix/src/misc.c::sysconf` 的 switch 里。该 switch **缺
`_SC_NPROCESSORS_CONF` (=83) 与 `_SC_NPROCESSORS_ONLN` (=84) 两个 case**，
默认走 `set_errno(EINVAL); return -1`。

LTP 用户态测试框架 `tst_cpu.c:31` 调 `sysconf(_SC_NPROCESSORS_ONLN)` → EINVAL → TBROK。

补丁极小（commit `47f603b9`，7 行新增）：

```c
#ifdef LOSCFG_KERNEL_SMP
        CONF_CASE_RETURN(_SC_NPROCESSORS_CONF, LOSCFG_KERNEL_SMP_CORE_NUM);
        CONF_CASE_RETURN(_SC_NPROCESSORS_ONLN, LOSCFG_KERNEL_SMP_CORE_NUM);
#else
        CONF_CASE_RETURN(_SC_NPROCESSORS_CONF, 1);
        CONF_CASE_RETURN(_SC_NPROCESSORS_ONLN, 1);
#endif
```

QEMU LTP smoke 验证：sysconf(84) 不再 EINVAL。但 LTP framework 还有
`/proc/meminfo`、mksh `[`、root capability 三个独立障碍——这些超出 exFAT 范围。

**项目记忆触发**：该补丁碰到 `feature/no-common-layer-edits` 红线（公共层
修改要显式用户授权）。用户已显式 OK 后落下。

---

## 7. 落地清单

| Commit | 内容 |
|---|---|
| `df15df7a` | Stage 2a ent_set — FAT 表项写 + FAT2 镜像 |
| `d326bc46` | Stage 2a vol_flags — 卷脏标志置/清 |
| `f450e5d3` | Stage 2b free_cluster — 释簇 + 清 bitmap |
| `3e5af29c` | Stage 2c alloc_cluster — 分配簇 + 置 bitmap |
| `47f603b9` | kernel sysconf 补 `_SC_NPROCESSORS_*`（公共层补丁） |

DAG 节点 9-11 + 18-20（vol_flags / free_cluster / alloc_cluster + ent_set，按
approval 时间排序），全部 spec_approved + code_approved 状态。

下一波（dentry/inode 写路径）见 `docs/dev/exfat_node.md`；
下一波（文件 IO 与截断）见 `docs/dev/exfat_file.md`。

---

## 8. 物理代码组织：7 文件 Linux 风格 (2026-05-05 重构)

Wave B 全部 stage 落地后，把原本散在 `fs/exfat/{util/, ./}` 的 25 个翻译单元
按 Linux fs/exfat/ 风格收敛到 7 个 .c 文件——单元测试需要编译完整产线代码，
小颗粒度组织在 cmocka harness 中维护成本太高。

| LiteOS-A 文件 | 收敛内容 | Linux 对应 |
|---|---|---|
| `exfat_super.c` | `VfsExfatMount/Unmount/Statfs/Sync` + `g_exfatMountOps` + `FSMAP_ENTRY` | super.c |
| `exfat_module.c` | chksum / options / upcase / **vol_flags** / boot_parser | misc.c + balloc.c 部分 |
| `exfat_cluster.c` | fat_chain / **ent_set** / **free_cluster** / **alloc_cluster** / balloc | fatent.c + balloc.c |
| `exfat_inode.c` | inode_alloc / lookup / open_close / attr | inode.c + namei.c |
| `exfat_file.c` | dentry_iter / dentry_set_write / alloc_dentry_slot / readdir / file IO / truncate | file.c + dir.c |
| `exfat_nls.c` | UTF-16 ↔ UTF-8 转换 | nls.c |
| `exfat_ops.c` | `g_exfatVops` / `g_exfatFops` 静态表 | (无单独文件，Linux 各 .c 里散) |

**本文档涉及 helper 当前所在文件**：
- `exfat_set_volume_dirty` / `exfat_clear_volume_dirty` → `exfat_module.c`（粗体行）
- `exfat_ent_set` / `exfat_free_cluster` / `exfat_alloc_cluster` → `exfat_cluster.c`（粗体行）

### 8.1 配套清理

- 移除全部 7 个 .c + `include/exfat.h` 内部冗余 `#ifdef LOSCFG_FS_EXFAT` 包装
  （`BUILD.gn` 的 `module_switch` + cmocka Makefile 无条件 `-DLOSCFG_FS_EXFAT`
  已经把开关收口在编译入口，源文件内重复包是死代码）。
- 各文件统一 file format：`#include` / `#define` / 文件作用域 `static g_*` 一律
  合并到文件头部，去重；helper 实现按 sub-narrative 分组保留 `/* ----- merged
  from <orig>.c ----- */` 锚点便于追溯。
- 旧 25 个 .c 物件移至 `backup/exfat-pre-consolidation/`（仓内 gitignored）。

### 8.2 cmocka 适配

- 全部 7 个 production .c 进入 `testsuites/unittest/exfat/Makefile::PROD_SRCS`，
  再无"测哪几个就编哪几个"的部分编译。
- 新增 4 份 host_stub：`fs/fs.h`、`disk_pri.h`、`los_tables.h`、`path_cache.h`
  让 `exfat_super.c` + `exfat_ops.c`（VFS 边界文件）可在主机编译。
- `host_stubs/{fs/mount.h, fs/file.h, vnode.h}` 内 `MountOps` /
  `file_operations_vfs` / `VnodeOps` 由原先的 `{int _opaque;}` 占位升级为
  真实成员形状——VFS 静态初始化 `g_exfatMountOps = { .Mount = ..., }` 才能
  type-check（cmocka 不调，仅类型可达）。
- `mock_disk.c` 移除原本占位用的 `g_exfatVops` / `g_exfatFops` 临时定义——
  现在产线表已纳入编译，重复定义会触发 ld multiple definition。

验证：`make` in `testsuites/unittest/exfat/` 跑 23 套件 / 395 testpoint，
0 failure。OHOS clang kernel build 走 `kernel/liteos_a/build.sh` 直接调用
（绕开 ninja stale rule），`liteos.map` 含全部 exfat 符号。
