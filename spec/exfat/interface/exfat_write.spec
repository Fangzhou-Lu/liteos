[PROMPT]
Wave B Stage 1：file_operations_vfs.write 回调 `VfsExfatWrite`，仅实现现有文件
**原地覆盖写**（in-place overwrite）。范围严格 clamp 到 [filep->f_pos,
ei->size)；越过 ei->size 的追加 / 分配新簇 / 扩大文件 / 写 dentry 持久化全部
明确**留 Wave B 后续 stage**（B2 truncate / B6 fsync）。

Linux 对照：`.write_iter = generic_file_write_iter`（exfat 不覆盖默认实现）。
Linux 上层 vfs_write 自动持 inode->i_rwsem 独占；底层 `exfat_get_block(create=0)`
在 in-place overwrite 路径不取 sbi->s_lock。LiteOS-A 端把上层 i_rwsem 折到本
函数：入口 `LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER)`，单点释放。
**不取 sbi->s_lock**（与 Linux 一致；B3 create / B4 mkdir 才会取）。
**不取 sbi->bitmap_lock**（B1 不分配簇；B2 truncate-extend 才会取）。

数据 IO 走 read-modify-write：
1. 读现有簇到 cluster_buf（los_part_read）
2. memcpy_s 把用户数据贴到目标 byte 范围
3. los_part_write 写回整簇

理由：los_part_write 以扇区为单位，write 边界很可能不对齐扇区；最简单对齐
方案是 cluster-level RMW（一次 IO 单元）。性能不是 B1 关注点；Wave B 后续
可优化为 sector-level RMW（仅触碰受影响扇区）。

簇定位复用 ExfatGetClusterAt 的逻辑（fat 链 / 线性段两种 flags）。该函数当前
在 exfat_file.c 内部 static——B1 不为复用做跨 TU 升级，**直接拷贝一份到
exfat_write.c**（标识符相同，static 不冲突）。后续 B2 truncate 也需要时再
做"提到 fat_chain util 公共导出"的 evolve（标 invariant exfat-write-helper-
duplication-temp）。

输出阶段必填：扩展 g_exfatFops：
```c
g_exfatFops = {
    .open  = VfsExfatOpen,
    .close = VfsExfatClose,
    .read  = VfsExfatRead,
    .seek  = VfsExfatSeek,
    .write = VfsExfatWrite,        /* + */
};
```

Out of scope（B1 显式不做）：
- 越过 ei->size 的追加（→ Wave B2 truncate-extend）
- 簇分配（→ Wave B2 truncate-extend / Wave B3 create）
- 写回 dentry 的 ValidDataLength / FileSize 字段（→ Wave B6 fsync）
- 翻转 sbi->vol_flags 的 VOLUME_DIRTY 位（→ Wave B6 fsync）
- O_APPEND flag 的特殊处理（→ Wave B2 一并处理）
- 标记 ei dirty / writeback（LiteOS-A 无 page cache writeback，简化为同步写）

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
extern int  exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu, uint32_t *next_clu);

/* exfat_clu_to_sector — fs/exfat/include/exfat.h 静态内联 */
/* uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi, uint32_t clu); */

/* exfat 簇链 flag 与 sentinel */
#define ALLOC_FAT_CHAIN     0x01u
#define ALLOC_NO_FAT_CHAIN  0x03u
#define EXFAT_EOF_CLUSTER   0xFFFFFFFFu
#define EXFAT_FREE_CLUSTER  0u
#define EXFAT_FIRST_CLUSTER 2u

#define TYPE_FILE  0x011Fu

/* LiteOS-A VFS 类型 */
struct file;
struct Vnode;

/* 内核原语 */
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern VOID  *m_aucSysMem0;
extern errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count);
extern int  los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);
extern int  los_part_write(INT32 pt, const VOID *buf, UINT64 sector, UINT32 count);
extern void PRINT_ERR(const char *fmt, ...);
#define LOS_WAIT_FOREVER 0xFFFFFFFFu

/* errno */
#define EINVAL  22
#define ENOMEM  12
#define EIO     5
#define EISDIR  21
#define EROFS   30
```

[GUARANTEE]
```c
/* === Public exports (declared in fs/exfat/include/exfat.h) =========== */

/*
 * VfsExfatWrite — file_operations_vfs.write callback (Wave B Stage 1).
 *
 * 调用约定：
 *   - 由 sys_write(2) 经 vfs_writev 派发。
 *   - 调用方未持任何 exfat 锁；本函数自取 ei->inode_lock，全程持有，单点释放。
 *   - 不取 sbi->s_lock（Linux exfat in-place overwrite 路径同样不取）。
 *   - 不取 sbi->bitmap_lock（B1 不分配簇）。
 *   - 锁序：ei->inode_lock 永远在最外层。
 *
 * 返回值：
 *   - >= 0 ：成功写入字节数 N（0 <= N <= len）。可能小于 len（本实现：
 *           filep->f_pos + len 越过 ei->size 时，写到 ei->size 截断）。
 *           filep->f_pos 已就地推进 N。
 *   - -EINVAL  ：filep / f_vnode / vp->data / sbi 为 NULL；或 buf == NULL && len > 0。
 *   - -EISDIR  ：vnode 是目录。
 *   - -ENOMEM  ：cluster_buf 分配失败。
 *   - -EIO     ：los_part_read / los_part_write / memcpy_s / cluster 链不连续 失败
 *               且 copied == 0；若已 copied > 0 → 短写返 N 而非 -errno
 *               （mirror Linux generic_file_write_iter）。
 *
 * 副作用：
 *   - 成功：filep->f_pos += N；目标簇的字节内容被替换；ei->size / valid_size
 *           / start_clu / flags / 任何 dentry 字段 一律不变。
 *   - 失败：filep->f_pos 不变；不修改 ei / sbi 任何字段；用户 buf 不变。
 *   - 全路径：sbi->vol_flags 不翻 VOLUME_DIRTY 位（Wave B6 fsync 接管）。
 *
 * Pre：filep / f_vnode / vp->data / sbi 非 NULL；ei->type == TYPE_FILE；
 *      ei->inode_lock 已 init；ei->{start_clu,flags,size} 已就绪（lookup 阶段安装）。
 *      len > 0 时 buf 非 NULL；len == 0 直接返回 0 不解引用 buf。
 */
extern ssize_t VfsExfatWrite(struct file *filep, const char *buf, size_t len);

/* === Static helpers (internal to exfat_write.c) ====================== */

/*
 * ExfatWriteGetClusterAt —— 与 exfat_file.c::ExfatGetClusterAt 同语义复制。
 * 仅 VfsExfatWrite 内部调用。Wave B 后续阶段（B2 truncate）也需要簇定位时
 * 再做"统一提到 fat_chain util 公共导出"的 evolve（参 invariant
 * exfat-write-helper-duplication-temp）。
 */
static int ExfatWriteGetClusterAt(const exfat_sb_info *sbi, uint32_t start_clu,
                                  uint8_t flags, uint32_t idx, uint32_t *out_clu);
```

[SPECIFICATION]
**Pre-Condition**：
1. `filep != NULL` 且 `filep->f_vnode != NULL` 且 `filep->f_vnode->originMount != NULL`
   且 `filep->f_vnode->data != NULL`。
2. `ei = vp->data` 是 lookup 阶段安装的有效 exfat_inode_info；`ei->type ==
   TYPE_FILE`；`ei->inode_lock` 已 init。
3. `sbi = vp->originMount->data` 是 mount 完成的 exfat_sb_info;几何字段
   `cluster_size / cluster_size_bits / blocksize / blocksize_bits / clu_offset
   / sect_per_clus_bits / num_clusters / part_id` 均已就绪。
4. `len > 0` 时 `buf != NULL`；`len == 0` 不解引用 buf。
5. 调用方未持 `ei->inode_lock`。

**Post-Condition (Case 1: ZeroLen)**：
- `len == 0`：直接返回 0；不取锁，不分配，不修改任何状态。

**Post-Condition (Case 2: 非常规 vnode)**：
- `ei->type != TYPE_FILE`：返回 `-EISDIR`，不取锁，不分配。

**Post-Condition (Case 3: BeyondEOF — 全越过 ei->size)**：
- 取 `ei->inode_lock`；若 `(uint64_t)filep->f_pos >= ei->size`：释放锁后
  返回 0；filep->f_pos 不变；不分配 cluster_buf。
- B1 显式不分配新簇；越过 size 的写在 Wave B2 引入。

**Post-Condition (Case 4: Success — in-place overwrite)**：
- 取 `ei->inode_lock` 后：
  - `to_write = min(len, ei->size - filep->f_pos)`；必有 `to_write > 0`。
  - 分配 cluster_buf（`sbi->cluster_size` 字节）。
  - `cur_off = filep->f_pos`；`written = 0`。
  - 循环（直到 `written == to_write`）：
    a. `clu_idx = cur_off >> sbi->cluster_size_bits`；
       `byte_in_clu = cur_off & (sbi->cluster_size - 1u)`。
    b. `ExfatWriteGetClusterAt(sbi, ei->start_clu, ei->flags, clu_idx, &cur_clu)`；
       失败 → goto IO_ERR。
    c. `sect = exfat_clu_to_sector(sbi, cur_clu)`；
       `nr_sect = sbi->cluster_size >> sbi->blocksize_bits`。
    d. `los_part_read(sbi->part_id, cluster_buf, sect, nr_sect, TRUE)`；
       失败 → goto IO_ERR。【RMW 第 1 步：读现有簇】
    e. `chunk = min(to_write - written, sbi->cluster_size - byte_in_clu)`。
    f. `memcpy_s(cluster_buf + byte_in_clu, sbi->cluster_size - byte_in_clu,
                buf + written, chunk)`；失败 → goto IO_ERR。【RMW 第 2 步：改】
    g. `los_part_write(sbi->part_id, cluster_buf, sect, nr_sect)`；失败 →
       goto IO_ERR。【RMW 第 3 步：写回整簇】
    h. `written += chunk`；`cur_off += chunk`。
  - 释放 cluster_buf；`filep->f_pos += written`；释放 `ei->inode_lock`；
    返回 `written`。

**Post-Condition (Case 5: 中途 IO 失败 + 已 copied > 0)**：
- IO_ERR 标签：若 `written > 0`：释放 cluster_buf 与锁；
  `filep->f_pos += written`；返回 `written`（短写，mirror Linux）。

**Post-Condition (Case 6: 中途 IO 失败 + 0 字节)**：
- IO_ERR 标签：`written == 0`：释放 cluster_buf 与锁；
  filep->f_pos 不变；返回 -EIO（或透传 ExfatWriteGetClusterAt 的 -EINVAL）。

**Post-Condition (Case 7: 分配失败)**：
- cluster_buf 分配失败：释放锁；返回 `-ENOMEM`；filep->f_pos 不变。

**Invariant** (id=exfat-write-linux-i-rwsem-faithful)：
VfsExfatWrite 全程持 `ei->inode_lock`，单点释放。该锁折映 Linux 上层
`vfs_write` 路径自动持有的 `inode->i_rwsem`（独占模式）。锁序：ei->inode_lock
永远在最外层。Wave B 后续 stage 引入 sbi->s_lock / bitmap_lock 时，本 invariant
仍要求 inode_lock 是最外层。

**Invariant** (id=exfat-write-no-s-lock)：
不取 `sbi->s_lock`。Linux `generic_file_write_iter` → `exfat_get_block(create=0)`
在 in-place overwrite 路径不取 s_lock；本路径严格对齐。这与 lookup / readdir
（取 s_lock）和 Wave B3 create（也取 s_lock）形成对照。

**Invariant** (id=exfat-write-no-bitmap-lock)：
不取 `sbi->bitmap_lock`。B1 in-place overwrite 不分配/释放簇，不修改 vol_amap。
Wave B2 truncate-extend 引入分配时再取该锁。

**Invariant** (id=exfat-write-clamp-by-size)：
写入字节数 `to_write = min(len, ei->size - filep->f_pos)`，绝不写越过 ei->size。
B1 不扩大文件；越过 size 的 byte 不分配、不归零、不写盘——直接以短写
（return N < len）反馈调用方。Wave B2 truncate-extend 解除本约束。

**Invariant** (id=exfat-write-no-extend)：
filep->f_pos >= ei->size 时返回 0（短写 / EOF-like）。绝不分配新簇、不修改
ei->start_clu / ei->flags / ei->size / ei->valid_size 任一字段。

**Invariant** (id=exfat-write-rmw-cluster-granularity)：
每次 IO 以一整簇为单位：读整簇 → memcpy_s 改目标 byte 范围 → 写整簇。
不做 sector-level RMW（性能优化留 Wave B 后续）。每轮 los_part_read /
los_part_write 都是 `sbi->cluster_size >> sbi->blocksize_bits` 个扇区。

**Invariant** (id=exfat-write-fat-chain-walk)：
`ALLOC_FAT_CHAIN`：从 `ei->start_clu` 起循环 `exfat_get_next_cluster` 走
clu_idx 步；途中 EOF/FREE/越界 → -EINVAL。
`ALLOC_NO_FAT_CHAIN`：`cur_clu = ei->start_clu + clu_idx`（线性段）。
B1 不修改簇链 flag（exfat-write-no-extend 保证）。

**Invariant** (id=exfat-write-cluster-buf-life)：
cluster_buf 在持 ei->inode_lock 期间分配，释放发生在释放锁之前；任意 goto
路径均经过 cluster_buf 释放点。无内存泄漏。

**Invariant** (id=exfat-write-pos-advance-exact)：
返回值 N 与 `filep->f_pos` 推进字节数严格相等（含 short-write 的 IO 失败
路径）；返回 -errno 时 filep->f_pos 不变。

**Invariant** (id=exfat-write-no-mutate-ei-on-success)：
**关键**：成功路径下 ei 任何字段都不变（含 size / valid_size / start_clu /
flags / dir / entry / i_pos / version / num_subdirs / type / attr /
i_size_ondisk）。Wave B6 fsync 接管 ei → dentry 的持久化；B1 仅改盘上数据
块，不改任何元数据。

**Invariant** (id=exfat-write-no-mutate-on-failure)：
全错误路径不修改 sbi / ei / vp / 用户 buf 任一字段；只允许的合法写入是
filep->f_pos（仅 N >= 0 路径）。

**Invariant** (id=exfat-write-no-vol-flags-touch)：
不翻转 sbi->vol_flags 的 VOLUME_DIRTY 位、不写 boot_buf。该字段持久化
留 Wave B6 fsync。这也意味着：B1 期间断电会丢失"磁盘曾被写"的 dirty
标记，下次 mount 不会自动 fsck——这是 B1 的已知 trade-off，Wave B6
解决。

**Invariant** (id=exfat-write-bounded-per-iter)：
循环每轮 `chunk = min(to_write - written, sbi->cluster_size - byte_in_clu)`
<= sbi->cluster_size；每轮恰好一个 RMW 的 los_part_read + los_part_write
对，不跨簇。

**Invariant** (id=exfat-write-le-host-only)：
ARMv7-A LE host；FAT 表项字节序由 exfat_get_next_cluster 内部转换；本路径
仅做字节复制 + 整簇 IO，无字节序敏感字段。

**Invariant** (id=exfat-write-uses-frozen-sbi-fields)：
本路径只读 sbi 的几何字段（cluster_size / cluster_size_bits / blocksize /
blocksize_bits / clu_offset / sect_per_clus_bits / num_clusters / part_id）。
不读 / 不写 vol_amap / vol_flags / used_clusters / clu_srch_ptr / boot_buf。

**Invariant** (id=exfat-write-uses-frozen-ei-fields)：
本路径只读 ei 的不变字段（start_clu / flags / size / type / inode_lock）。
**绝不修改**任何 ei 字段（含 valid_size——B1 不维护，Wave B2 写路径接管）。

**Invariant** (id=exfat-write-no-spinlock-callsite)：
本函数体内含 LOS_MemAlloc 与 los_part_read / los_part_write（都可能睡眠）；
调用方不允许在持自旋锁时进入。VFS 上层默认不持自旋锁，约束自然成立。

**Invariant** (id=exfat-write-zero-len-fast-path)：
`len == 0` 直接返回 0：不取锁、不分配、不解引用 buf。POSIX write(fd, buf, 0)
zero-byte short-circuit 与 Linux 一致。

**Invariant** (id=exfat-write-isdir-rejected)：
`ei->type != TYPE_FILE` 直接返回 `-EISDIR`，不取锁、不分配。

**Invariant** (id=exfat-write-rmw-read-failure-aborts)：
RMW 流程的 los_part_read 失败必须中止当前轮并 goto IO_ERR；**不可**直接
los_part_write（防止把未初始化的 cluster_buf 字节写回，腐化磁盘其它字节）。

**Invariant** (id=exfat-write-helper-duplication-temp)：
`ExfatWriteGetClusterAt` 与 `exfat_file.c::ExfatGetClusterAt` 是字面同语义副本。
B1 接受短期重复以避免跨 stage 的接口暴露。Wave B2 引入 truncate 时一并
重构为 `exfat_pos_to_cluster` 公共导出（位于 fat_chain util），由 read /
write / truncate 三处共用，并删除两处 static 副本。

**Invariant** (id=exfat-write-short-write-on-mid-failure)：
RMW 流程中途任何步骤失败（cluster walk / part_read / memcpy_s / part_write），
若 `written > 0` 则返回 written（短写），否则返回 -errno。filep->f_pos 与
written 同步推进——保证调用方透过短写计数能判断"哪几字节已落盘"。

## Refine Prompt

锁约定（与 read 一致，B1 不引入新锁）：

1. **取 `ei->inode_lock` 全程持有**：入口
   `LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER)`，所有 goto 标签后单点
   `LOS_MuxUnlock` 再 return。Mirror Linux 上层 VFS 在 vfs_write 路径
   自动持 inode->i_rwsem（独占）的语义。LosMux 不区分独占 / 共享，对 B1
   足够正确（写时无并发读——同 fd 的 read+write 由 VFS 上层串行）。

2. **不取 sbi->s_lock**：Linux generic_file_write_iter 不取；
   exfat_get_block(create=0) 不取。本路径严格对齐。Wave B3 create / B4 mkdir /
   B5 rename 引入 s_lock（与 Linux exfat namei 一致）。

3. **不取 sbi->bitmap_lock**：B1 不分配 / 释放簇。Wave B2 truncate-extend
   引入。

4. **锁序约束**：ei->inode_lock 永远最外层。Wave B 后续 stage 若引入 s_lock，
   必须先释放 inode_lock 再按 sbi->s_lock → bitmap_lock 顺序取，避免与
   create/balloc 路径死锁。

5. **spinlock 禁区**：本函数体内 LOS_MemAlloc / los_part_read / los_part_write
   / memcpy_s 均可能睡眠；调用方不允许在持自旋锁时进入。

6. **可重入安全**：同一 filep 的并发 write 由 ei->inode_lock 串行化；不同
   file 的并发 write 走不同 ei，互不阻塞——这是不取 s_lock 的核心收益。

7. **read / write 互斥粒度**：read / write 都取同一个 ei->inode_lock，因此
   同一文件的 read 与 write 完全串行（POSIX 强一致）。Wave B6 fsync 也将取
   该锁，与 read/write 串行。
