# exFAT 后续路线图：从 mount-only 到可读

完成 mount/umount/statfs，但 `g_exfatVops`/`g_exfatFops` 全 NULL，导致
`ls /mnt/exfat` 与 `cat <file>` 均返 `-ENOSYS`。本文档把 已知边界翻译为
后续 的可执行 Loop A/B 计划，按依赖序排列。

---

## 1. 后续 范围与边界

### 1.1 后续 包含

读路径全部：`lookup`、`open`、`read`、`close`、`readdir`、`getattr`、`stat`。
覆盖到 `cat /mnt/exfat/<file>` 与 `ls -la /mnt/exfat/` 在 QEMU 上正常返回内容。

### 1.2 后续 显式不包含

- 写路径（`create`/`write`/`unlink`/`rename`/`mkdir`/`rmdir`/`truncate`）→ 后续
- 锁竞争 / 多线程并发优化 → 后续
- 目录通知 / mmap → v4+
- TexFAT、bitmap 第 2 副本、FAT 第 2 副本一致性校验 → 不规划

---

## 2. stage 依赖图

```
    (mount-only) ────┐
    │
    ▼
    ┌────────────┐ fat_chain (FAT 链遍历：clu→clu_offset、读 FAT[clu]、bounded walk)
    │ 依赖 │ │
    │ helpers│ ▼
    │ │ inode_alloc (exfat_inode_info zalloc + 锁初始化)
    │ │ │
    │ │ ▼
    │ │ dentry_iter (走目录簇链 + 32B dentry 解析 + chksum16 校验)
    │ │ │ ← 依赖 dentry 的 ReadFatEntry + chksum::chksum16
    │ │ ▼
    │ │ nls_utf16 (uint16_t[] → UTF-8 转换；upcase 比较辅助)
    │ │ │ ← 依赖 upcase
    │ │ ▼
    │ │ lookup (path 单元素 → exfat_dir_entry → vnode)
    │ │ │ ← 依赖 dentry_iter + nls_utf16
    │ │ ▼
    │ │ readdir (枚举目录 dentry → struct dirent)
    │ │ │
    └────────────┘ ▼
    open (vnode → file 句柄；初始化 ei->hint_bmap)
    │
    ▼
    read (文件偏移 → 簇号 → los_part_read)
    │
    ▼
    close (file 释放，可能 invalidate hint)

最后：
    vfs_ops_stub → vfs_ops_filled (g_exfatVops/g_exfatFops 字段填充，
    通过 spec_gen_refine 不改符号身份)
```

**总计 后续 新增 9 个 stage**（fat_chain、inode_alloc、dentry_iter、nls_utf16、
lookup、readdir、open、read、close）+ 1 个 refine（vfs_ops_filled）。

---

## 3. 各 stage 详细计划

### 3.1 fat_chain（FAT 链遍历，纯 IO 工具）

**目标函数**：

```c
/* clu → next clu via FAT[clu]; bounded by sbi->num_clusters */
int exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu, uint32_t *next_clu);

/* 簇号 → 数据扇区起点 */
uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi, uint32_t clu);

/* 完整链遍历，回调 visitor 形式 */
typedef int (*exfat_chain_visitor_t)(uint32_t clu, void *ctx);
int exfat_chain_walk(const exfat_sb_info *sbi, uint32_t start_clu,
    exfat_chain_visitor_t visitor, void *ctx);
```

**预期 ask-first**：

- visitor 模式 vs 显式 chain 数组缓存？（visitor 节省内存，cache 加速反复访问）
- 大端兼容是否在本阶段做？（目前 LE-only，BE port 后续+）

**[RELY]**：dentry 的 `ReadFatEntry` 已 static——后续 提升为 public 或在
fat_chain 重写。

### 3.2 inode_alloc（inode_info 分配）

**目标函数**：

```c
int exfat_inode_alloc(exfat_inode_info **out);
void exfat_inode_free(exfat_inode_info *ei);
void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu);
```

主要为 lookup/open 复用减少 mount 中的零散初始化。

**预期 ask-first**：

- `LOS_MuxAttr` 用默认 NULL 还是开启 `LOS_MUX_PRIO_INHERIT`？

### 3.3 dentry_iter（最复杂的中间层）

**目标函数**：

```c
/* 在目录簇链中按索引取 dentry（多扇区跨越透明）*/
int exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
    int entry, struct exfat_dentry *out, uint64_t *bh_sector);

/* 获取一组 dentry（file primary + stream + name1..nameK）*/
int exfat_get_dentry_set(const exfat_sb_info *sbi, const exfat_chain *dir,
    int entry, struct exfat_dentry *set, int max_entries,
    int *num_entries);

/* dentry-set chksum16 校验 */
int exfat_validate_dentry_set(const struct exfat_dentry *set, int num_entries);
```

**预期 ask-first**：

- dentry buffer cache？后续 不引（每次重读，用 bcache 复用 IO）。
- 校验失败如何处理？严格 -EINVAL（与 upcase 严格策略一致）。

**[RELY]**：fat_chain、chksum::chksum16。

### 3.4 nls_utf16（字符集转换）

**目标函数**：

```c
/* exfat_dentry name 段（uint16_t LE）→ UTF-8 缓冲 */
int exfat_uni_to_utf8(const uint16_t *uni, int uni_len,
    char *out, int out_max);

/* 比较两个 UTF-16 串（不区分大小写，用 sbi->vol_utbl 做 upcase）*/
int exfat_uniname_cmp(const exfat_sb_info *sbi,
    const uint16_t *a, int a_len,
    const uint16_t *b, int b_len);
```

**预期 ask-first**：

- 后续 仅 UTF-8 输出？（与 mount options 的 `iocharset=utf8` 约束一致 → 是）
- 输入路径 string 转 UTF-16 在哪做？（lookup 入口，`exfat_nls_to_utf16`）

### 3.5 lookup（路径单元素解析）

**目标函数**：

```c
int VfsExfatLookup(struct Vnode *parent, const char *path,
    int len, struct Vnode **vpp);
```

**[GUARANTEE] 调用约定**（节选）：

```c
/*
    * VfsExfatLookup: 在 parent 目录中查找 path[0..len) 名称，返回新 vnode。
    *
    * 调用约定：
    * - 调用方持有 parent->vnodeLock。
    * - path 是 UTF-8 编码，单元素（不含 '/' 分隔符）。
    * - 找到 → vpp 写入新 vnode，类型由 dentry attr 决定（DIR/FILE）。
    * - 未找到 → 返回 -ENOENT，vpp 不写。
    * - 错误 → 返回负 POSIX errno。
    */
```

**[RELY]**：dentry_iter、nls_utf16、`VnodeAlloc`、`VfsHashInsert`。

**预期 ask-first**：

- 大小写不敏感比较？exfat 默认 case-preserving + case-insensitive 查找 → 是。
- hint_femp / hint_stat 加速结构？后续 不引，后续 优化。

### 3.6 readdir

**目标函数**：

```c
int VfsExfatReaddir(struct Vnode *vp, struct dirent_buf *buf, int buf_size);
```

枚举目录簇链中所有 EXFAT_FILE 类型 dentry-set，逐个 emit 一个 `struct dirent`。

**[RELY]**：dentry_iter、nls_utf16。

**预期 ask-first**：

- offset 语义？（exfat 用 dentry 索引作 offset，linux 同此 → 是）

### 3.7 open / close

**目标函数**：

```c
int VfsExfatOpen(struct Vnode *vp, struct File *file);
int VfsExfatClose(struct Vnode *vp, struct File *file);
```

主要初始化 / 释放 `exfat_inode_info` 内的 `hint_bmap`（簇映射缓存）。

**预期 ask-first**：

- O_TRUNC 在 open 时拒绝？read-only mount 可拒绝 open 时 W 标志 → 是。

### 3.8 read（后续 收尾）

**目标函数**：

```c
ssize_t VfsExfatRead(struct File *file, char *buf, size_t len);
```

文件偏移 → 簇号 → 通过 fat_chain 找物理簇 → `los_part_read` 读字节。

**[RELY]**：fat_chain、nls_utf16 不需要、`los_part_read`、`memcpy_s`。

**预期 ask-first**：

- 是否引入 bcache？后续 不引（每次直接 part_read），后续 优化。
- 跨簇读怎么处理？分段读 + 拼接缓冲（hint_bmap 加速可后续）。

### 3.9 vfs_ops_filled（最后 refine）

通过 `spec_gen_refine` 修改 `vfs_ops_stub` spec，**不改符号身份**：

```c
struct VnodeOps g_exfatVops = {
    .Lookup = VfsExfatLookup,
    .Open = VfsExfatOpen,
    .Close = VfsExfatClose,
    .Read = VfsExfatRead,
    .Readdir = VfsExfatReaddir,
    /* 写路径仍 NULL，在后续版本 */
};
struct file_operations_vfs g_exfatFops = {
    .open = VfsExfatOpen,
    .close = VfsExfatClose,
    .read = VfsExfatRead,
    /* ... */
};
```

由 invariant `exfat-vfs-stub-symbol-stable`（ 钉死）保护——不允许重命名、
不允许拆分、不允许新增编译期常量。

---

## 4. 后续 估算

| 维度 | 实测 | 后续 估算 |
|---|---|---|
| stage 数 | 7 | 9 + 1 refine = 10 |
| spec 行数 | 1,344 | ~2,000（更多 [SPECIFICATION] case，dentry_iter 复杂）|
| code 行数 | 1,476 | ~2,500（lookup/read 涉及更多 IO + 字符集转换）|
| spec/code 比 | 0.91 | 估 0.85（read 相对机械，spec 短）|
| ask-first 题数 | 10 | 估 ~15 |
| 跨阶段 reconcile 事件 | 2 | 估 4-6（lookup 修 mount.spec [RELY] 添 lookup 签名等）|
| Loop A/B wall-clock | 3 hr | 估 5-6 hr |
| QEMU 验收 | mount RC=0 | `cat /mnt/exfat/EXFAT_HELLO.txt` 输出预期内容 |

---

## 5. 后续 启动检查清单

开始 后续 之前确认：

- [ ] `git checkout -b feature/exfat`
- [ ] 所有 stage DAG 已 approved（`commit_node.py show exfat`）
- [ ] mount [Invariant] 列表已沉淀进 `docs/exfat_mount_invariants.md`（待写）
- [x] umount NULL deref 已修复（2026-05-01）—— root cause: `g_exfatVops.Reclaim`
 为 NULL 让 VFS `VnodeFreeAll` 在 path_cache 走查时 NULL+4 崩溃。修法：
 lazy-init `g_exfatVops.Reclaim = VfsExfatReclaim` 于 `VfsExfatMount`，
 Reclaim 释放 inode_info + 销毁 inode_lock + 清 `vnode->data`，VFS 框架
 在 VnodeFree 流程内自然回收 root vnode。QEMU mount→umount→remount→umount
 4 轮全 RC=0、0 panic，验证通过。
- [ ] cclsp + clangd 在工作机就位
- [ ] 远端 192.168.1.15 SSH + sudo 可用（QEMU 验证）

---

## 6. 风险

### 6.1 dentry_iter 复杂度

dentry_iter 是 后续 最难的 stage——涉及目录簇链遍历 + dentry-set 拆解 +
chksum16 校验三层逻辑。Linux exfat dir.c 1203 行有 30+ 个 helper，需谨慎
范围控制。

**缓解**：spec 拆成 3 个子 stage（chain_walk_with_dentry、dentry_set_extract、
dentry_set_validate），各自独立 ask-first + approve。

### 6.2 字符集转换体积

UTF-16 → UTF-8 转换表（surrogate pair 处理）+ upcase 比较两条路径都要走。
 已嵌入 65536-entry upcase（128KB）。

**缓解**：UTF-8 转换用算法（不查表）；upcase 比较通过 `vol_utbl` 间接，
不引入新表。

### 6.3 锁竞争首次出现

mount 的 sbi 暴露后，lookup / read 是首批并发访问 sbi 的路径。
锁序需要在 lookup spec 的 `## Refine Prompt` 段严格记录。

**缓解**：先实现单线程 lookup（path mux 串行），后引入并发 后续.5。

### 6.4 跨阶段 reconcile 频率上升

stage 间依赖比 复杂（fat_chain → dentry_iter → lookup 三层），
跨阶段 [RELY] 漂移更频繁。

**缓解**：尽快实现 `reconcile_spec` MCP 工具（ 已知边界 L-2）。

---

## 7. 后续+ 展望（非 后续 范围）

| Stage 类 | 目标 |
|---|---|
| 写路径核心 | bitmap 修改（exfat_set_bitmap）、FAT 写（exfat_ent_set）、create/write/unlink |
| journal-like | vol_flags VOLUME_DIRTY 持久化（mount 时设、umount 时清）|
| 性能优化 | bcache 集成、hint_bmap、dentry_set 缓存 |
| 锁路径 | lookup/read 加 sbi-level mux；read 加 inode_lock；rename 跨级锁 |
| 上游对齐 | TexFAT 检测、BACKUP_BOOT_SECTOR 校验 |

---

## 8. v2 写路径完成回填（2026-05-11）

§7 列出的 "后续+ 展望" 写路径核心在 v2 已经全部落地。下表把每条计划与
实际交付的 stage / 测试 / commit 对齐：

| v1.0 计划 | v2 实际 stage | 主要 commits | testpoints |
|---|---|---|---|
| bitmap 修改 (exfat_set_bitmap) | alloc_cluster / free_cluster | (Wave B Stage 2b/2c) | 已含 |
| FAT 写 (exfat_ent_set) | ent_set | (Wave B Stage 2a) | 13 |
| create / unlink | create + unlink | `581e2111`, `0e43a7c3` | 5 + 15 |
| journal-like vol_flags | vol_flags | (Wave B Stage 2a) | 已含 |
| 锁路径 sbi-level mux | 所有 Wave B inode/interface stage | s_lock + bitmap_lock + inode_lock 三层 | 已含 |

v2 还额外交付了：

| 额外完成 | stage | commit |
|---|---|---|
| 完整 inode 元数据时间编/解码 + nlink | inode_metadata_model | (Wave B Stage 5) |
| mkdir + cmocka 测试固化 | mkdir | `d11fa145` |
| dentry_set_write + alloc_dentry_slot DAG 闭环 | dentry_set_write, alloc_dentry_slot | `39722191` |
| rmdir 全流程 | rmdir | `e08c2358` (8 invariants, 22 testpoints) |
| rename 全流程 (完整 Linux 语义) | rename | `96fd9f8e` (8 invariants, 16 testpoints) |
| MountOps 收口 (statfs / sync) | mount_ops_rest | `3d29e7a7` (5 invariants, 10 testpoints) |
| vfs_ops_stub DAG 对齐 | vfs_ops_stub | `f18ee50d` (8 testpoints) |

**当前状态**：31/31 stage 全部 fully_complete (spec + code + tests)。
VFS 写表面五件套 (mkdir/rmdir/create/unlink/rename) 全到位；MountOps 四
件套 (mount/unmount/statfs/sync) 全到位。

cmocka host 套件 350+ testpoints `=== exfat TOTAL FAILURES: 0 ===`。

---

## 9. v3 路线图（非当前范围）

v2 推迟到 v3 的项目，列在这里方便接手者：

### 9.1 parent metadata sync

mkdir / create / unlink / rmdir / rename 都在 in-memory 增减
`parent_ei->num_subdirs`，但**不**写父目录 on-disk dentry。
导致 umount → remount 后父目录的统计数据从盘读回，不反映 v1 期间变更。

**v3 stage**：`parent_metadata_sync`
- 新 helper `exfat_sync_parent_dentry(sbi, parent_ei)`，回填 num_subdirs / mtime
- 每个写路径 VOP 在 phase1 unlock 前调用一次
- 影响 spec：mkdir.spec / unlink.spec / rmdir.spec / rename.spec 都需 SpecFine
  添加 "on-disk parent dentry now reflects new num_subdirs" 条款

### 9.2 rename rollback semantics

rename Case 11/12 (新 set 写入成功后 src 端 tombstone 写入失败) 在 v2 是
**no rollback** —— disk 上同名两份，PRINT_ERR 后留给 future fsck。

**v3 stage**：`rename_rollback`
- Case 11/12 触发时，反向把新 set tombstone (top-bit clear)，恢复 disk 到 rename
  前状态
- 新 invariant `exfat-rename-atomic-rollback`
- 复杂度：需要在每个 init_dir_entry / init_ext_entry / set_dentry_set 都保留
  足够的回滚状态。预计 +150 LOC。

### 9.3 inode hash key 优化

当前 `i_pos = (start_clu << 32) | entry` 作为 VfsHashInsert 的 key。
- 零簇文件 (start_clu == EXFAT_EOF_CLUSTER) 全部碰撞到同一 bucket
- 大文件 (start_clu > 0xFFFFFFFF / cluster_size) 上半段也碰撞

**v3 stage**：`inode_hash_key_v2`
- 改为 `XXH3(parent_inode_no, entry) ^ start_clu`
- 评估对 lookup 性能影响（应该减半 hash 冲突率）

### 9.4 mount-time eager exfat_count_used_clusters

当前 `mount.spec` 不调 exfat_count_used_clusters，sbi->used_clusters 保持
EXFAT_CLUSTERS_UNTRACKED 直到 alloc_cluster 第一次被调用。
- statfs 在此期间返回 f_bfree=0（v2 通过 mount_ops_rest 显式声明）
- 用户态 df 命令初次执行就误报"满"

**v3 stage**：`mount_count_eager`
- mount.spec SpecFine：Step 11 (root vnode init) 之后 / Step 12 (FsmapEntry
  insert) 之前调 exfat_count_used_clusters
- 失败路径：返回 0 但 PRINT_WARN——这是 best-effort 优化，不影响 mount 成功

### 9.5 Setattr / Chattr / Link / Symlink 等剩余 VOP

v2 后 g_exfatVops 仍有 8 个 NULL slot (Setattr, Chattr, Link, Symlink,
Readlink, ReadPage, WritePage, Fscheck)。其中：
- **Setattr / Chattr**: exFAT 不支持 POSIX 文件 mode/owner（mount 选项已覆盖
  全局 uid/gid）。Linux 实现是 `exfat_setattr` 仅处理 size 变更 ——
  和 truncate 重叠。v3 可考虑实现一个最小版本支持 utime。
- **Link / Symlink / Readlink**: exFAT 文件系统**结构上**不支持 hard link 或
  symbolic link。Linux 也不实现。这 3 个 slot 应永远保持 NULL；可在 v3 加
  invariant `exfat-vfs-no-link-symlink` 锚定。
- **ReadPage / WritePage**: LiteOS-A v1 无 page cache。当 v2+ 引入 bcache /
  page cache 时填充。
- **Fscheck**: Linux 也没实现（fsck 是用户态工具）。永久 NULL。

---

## 10. 引用

- `docs/specfs_plugin_design.md` —— 插件设计与 8 项论文差异
- `docs/dev/exfat_mount.md` —— 开发流程实录（本文档的前传）
- Linux 源码：`/Users/kissa/Codebase/linux/fs/exfat/dir.c, file.c, inode.c, namei.c`
- 论文 *Sharpen the Spec, Cut the Code*（FAST'26，arXiv:2512.13047）的 §evolvefs
 小节描述阶段化演化策略——后续 即按此模式。
- `spec/exfat/.specfs.dag.json` —— DAG 状态 (31 stages, 全部 fully_complete)
- v2 commits: `0e43a7c3`(unlink) → `d11fa145`(mkdir) → `39722191`(dsw+ads) →
  `e08c2358`(rmdir) → `96fd9f8e`(rename) → `3d29e7a7`(mount_ops_rest) →
  `f18ee50d`(vfs_ops_stub) —— 完整 v2 写路径交付时间线
