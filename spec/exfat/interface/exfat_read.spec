[PROMPT]
将 Linux `fs/exfat/file.c` 的 `.read_iter = generic_file_read_iter` 路径移植为
LiteOS-A 端的 `.read = VfsExfatRead` 实现。Linux 通过 page cache + bmap
(`exfat_get_block`) 完成数据读；LiteOS-A 无 page cache，本路径直接走簇链，
按簇分段调用 `los_part_read` 装载到中转缓冲区，再 `memcpy_s` 到用户 buf。

锁模型严格对齐 Linux：

- Linux 的 `generic_file_read_iter` 内部不取任何 exfat 自定义锁；
  `exfat_get_block` 在 read（create=0）路径下也不取 sbi->s_lock。
  Linux 仅依赖 VFS 上层在 read 系统调用前自动持有 `inode->i_rwsem` (shared)，
  对同一 inode 串行化 read/write/truncate。
- LiteOS-A VFS 不自动持 inode 锁。本实现把 Linux 上层的 i_rwsem 折到本函数内
  显式获取一次：入口 `LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER)`，返回前
  在所有 goto 标签后单点 `LOS_MuxUnlock`。
- **不取 sbi->s_lock**——这是与 lookup/readdir 的关键差异。Linux exfat 在 read
  路径不取，本路径也不取，避免单文件读阻塞跨文件并发 lookup。
- **不取 sbi->bitmap_lock**——read 不分配/释放簇，不读 vol_amap。

辅以一个 static 私有助手 `ExfatGetClusterAt(sbi, start_clu, flags, idx, *out)`
在 .c 内部使用：从 start_clu 起按簇链或线性方式走 idx 步得到目标簇。
不上 common.header（仅本 TU 私用）。

输出阶段必填：把 `g_exfatFops` 由 `{ 0 }` 改为静态初始化 `{ .read = VfsExfatRead }`
（其他 slot 仍为 NULL，待 open/close/seek 阶段逐个填上）。这与 Wave A 既定的
"Linux 风格预先定义 ops" 方针一致。

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;
extern int  exfat_get_next_cluster(const exfat_sb_info *sbi, uint32_t cur_clu, uint32_t *next_clu);

/* 由 fat_chain stage 在 fs/exfat/include/exfat.h 提供的 static inline */
/* uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi, uint32_t clu); */
/* 计算：sbi->clu_offset + ((clu - 2u) << sbi->sect_per_clus_bits)，partition-relative */

/* exfat 簇链 flag 常量（与 exfat_raw.h / inode_alloc invariant 对齐）*/
#define ALLOC_FAT_CHAIN     0x01u
#define ALLOC_NO_FAT_CHAIN  0x03u
#define EXFAT_EOF_CLUSTER   0xFFFFFFFFu
#define EXFAT_FREE_CLUSTER  0u

/* 文件类型 */
#define TYPE_FILE  0x101u

/* LiteOS-A VFS 类型（来自 fs/include/fs/file.h、fs/vfs/include/vnode.h）*/
struct file;        /* 包含 f_vnode、f_pos、f_oflags、ops、f_priv */
struct Vnode;       /* 包含 type、data、originMount、vop、fop */
typedef struct Vnode Vnode;

/* LiteOS-A 内核原语 */
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern VOID  *m_aucSysMem0;
extern errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count);
extern int  los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);
extern void PRINT_ERR(const char *fmt, ...);
#define LOS_WAIT_FOREVER 0xFFFFFFFFu

/* errno（POSIX 负值边界返回）*/
#define EINVAL  22
#define ENOMEM  12
#define EIO     5
#define EISDIR  21
```

[GUARANTEE]
```c
/* === Public exports (declared in fs/exfat/include/exfat.h) =========== */

/*
 * 调用约定：
 *   - VFS file_operations_vfs.read 回调，由 read(2) 经过 read_proxy 派发。
 *   - 调用方未持任何 exfat 锁；本函数自取 ei->inode_lock，全程持有，单点释放。
 *   - 不取 sbi->s_lock（与 Linux exfat read 路径一致）。
 *   - 不取 sbi->bitmap_lock（read 不接触 vol_amap）。
 *   - 锁序：ei->inode_lock 永远在最外层。允许在持锁期间调用 los_part_read（睡眠 OK）。
 *
 * 返回值：
 *   - >= 0 ：成功读到的字节数 N（0 <= N <= len）。N 可小于 len（EOF 截断或中途
 *           IO 失败但已拷贝部分数据）。filep->f_pos 已就地推进 N。
 *   -   <0 ：负 POSIX errno；filep->f_pos 不变；用户 buf 内容未定义但允许已被
 *           部分写入（VFS 层契约不要求未失败前的数据回滚）。本实现的语义是：
 *           只有在「尚未拷贝任何字节」时才返回 -errno；一旦拷过哪怕一个字节，
 *           失败就以 N 形式返回（mirror Linux generic_file_read_iter）。
 *
 * 副作用：
 *   - 修改 filep->f_pos（成功路径）。
 *   - 不修改 sbi 任何字段。不修改 ei 任何字段（含 i_pos / size / valid_size /
 *     start_clu / flags 全部只读）。
 *
 * Pre：filep != NULL && filep->f_vnode != NULL && filep->f_vnode->type ==
 *      VNODE_TYPE_REG && filep->f_vnode->data 指向有效 exfat_inode_info（type ==
 *      TYPE_FILE）&& filep->f_vnode->originMount->data 指向已 mount 完成的
 *      exfat_sb_info。len > 0 时 buf != NULL；len == 0 直接返回 0 不解引用 buf。
 */
extern ssize_t VfsExfatRead(struct file *filep, char *buf, size_t len);

/* === Static helpers (internal to exfat_read.c) ======================== */

/*
 * 调用约定：
 *   - 仅 VfsExfatRead 内部调用，已持 ei->inode_lock；本函数不再取锁。
 *   - 从 start_clu 起按 flags（ALLOC_FAT_CHAIN / ALLOC_NO_FAT_CHAIN）走 idx 步，
 *     idx == 0 时 *out_clu = start_clu。
 *   - ALLOC_NO_FAT_CHAIN 时直接 *out_clu = start_clu + idx（线性段）。
 *   - ALLOC_FAT_CHAIN 时循环调用 exfat_get_next_cluster；途中遇到 EOF/FREE/越界
 *     簇号返回 -EINVAL（spec 不允许 idx 落在簇链外）。
 *
 * 返回值：0 / -EINVAL / -EIO（透传 exfat_get_next_cluster 错误）。
 */
static int ExfatGetClusterAt(const exfat_sb_info *sbi, uint32_t start_clu,
                             uint8_t flags, uint32_t idx, uint32_t *out_clu);
```

[SPECIFICATION]
**Pre-Condition**：
1. `filep != NULL` 且 `filep->f_vnode != NULL` 且 `filep->f_vnode->originMount != NULL`。
2. `vp->type == VNODE_TYPE_REG`（VFS 上层确保；本函数额外断言 `ei->type == TYPE_FILE`）。
3. `ei = vp->data` 是 lookup 阶段安装的 exfat_inode_info；其字段
   `start_clu`、`flags`、`size`、`type`、`inode_lock` 已就绪且 `inode_lock` 已 init。
4. `sbi = mount->data` 是 mount 完成时初始化的 exfat_sb_info；几何字段
   `cluster_size`、`cluster_size_bits`、`blocksize`、`blocksize_bits`、`clu_offset`、
   `sect_per_clus_bits`、`num_clusters`、`part_id` 均已就绪。
5. `len > 0` 时 `buf != NULL`；`len == 0` 不解引用 buf。
6. 调用方未持 `ei->inode_lock`。

**Post-Condition (Case 1: ZeroLen)**：
- 若 `len == 0`：直接返回 0；不取锁，不分配，不修改任何状态。

**Post-Condition (Case 2: BeyondEOF)**：
- 取 `ei->inode_lock`；若 `filep->f_pos >= ei->size`：释放锁后返回 0；
  `filep->f_pos` 不变；不分配 cluster_buf。

**Post-Condition (Case 3: Success)**：
- 取 `ei->inode_lock` 后：
  - 计算 `to_read = min(len, ei->size - filep->f_pos)`，必有 `to_read > 0`。
  - 分配 cluster_buf（cluster_size 字节，从 m_aucSysMem0）。
  - 设 `cur_off = filep->f_pos`，`copied = 0`。
  - 循环（直到 `copied == to_read`）：
    a. `clu_idx = cur_off / cluster_size`；`byte_in_clu = cur_off % cluster_size`。
    b. 调用 `ExfatGetClusterAt(sbi, ei->start_clu, ei->flags, clu_idx, &cur_clu)`；
       若失败（含中途簇链断裂、簇号越界）→ goto IO_ERR。
    c. `chunk = min(to_read - copied, cluster_size - byte_in_clu)`。
    d. `sect = exfat_clu_to_sector(sbi, cur_clu)`；
       `nr_sect = sbi->cluster_size >> sbi->blocksize_bits`。
    e. `los_part_read(sbi->part_id, cluster_buf, sect, nr_sect, TRUE)`；
       失败 → goto IO_ERR。
    f. `memcpy_s(buf + copied, to_read - copied, cluster_buf + byte_in_clu, chunk)`；
       失败 → goto IO_ERR（理论不会发生，destMax 已正确传入）。
    g. `copied += chunk`；`cur_off += chunk`。
  - 释放 cluster_buf；`filep->f_pos += copied`；释放 `ei->inode_lock`；返回 `copied`。

**Post-Condition (Case 4: IO 中途失败但已拷字节)**：
- IO_ERR 路径：若 `copied > 0`，释放 cluster_buf 与锁，
  `filep->f_pos += copied`，返回 `copied`（mirror Linux short-read 语义）。
- IO_ERR 路径：若 `copied == 0`，释放 cluster_buf 与锁，
  `filep->f_pos` 不变，返回相应负 errno（`-EIO` 居多；ExfatGetClusterAt 失败可能
  `-EINVAL`）。

**Post-Condition (Case 5: 分配失败)**：
- cluster_buf 分配失败：释放锁，返回 `-ENOMEM`；`filep->f_pos` 不变。

**Post-Condition (Case 6: 非常规 vnode)**：
- `ei->type != TYPE_FILE`（被错当文件读的目录 vnode）：返回 `-EISDIR`，不取锁，不分配。

**Invariant** (id=exfat-read-linux-i-rwsem-faithful)：
VfsExfatRead 全程持 `ei->inode_lock`，单点释放。该锁对应 Linux 上层 VFS 在
`vfs_read` 路径自动持有的 `inode->i_rwsem`（shared 模式），LiteOS-A VFS 不
自动持有，故本函数显式 fold。锁序：ei->inode_lock 永远在最外层。

**Invariant** (id=exfat-read-no-s-lock)：
不取 `sbi->s_lock`。Linux `generic_file_read_iter` → `exfat_get_block(create=0)`
路径不取 s_lock；本路径严格对齐。这是与 lookup/readdir 的关键差异（后者
对应 Linux exfat 在 namei 内显式 `mutex_lock(&sbi->s_lock)`，故 LiteOS 也取）。

**Invariant** (id=exfat-read-no-bitmap-lock)：
不取 `sbi->bitmap_lock`。read 路径不分配/释放簇，不修改 vol_amap，无需该锁。

**Invariant** (id=exfat-read-eof-returns-zero)：
`filep->f_pos >= ei->size` 必返回 0（非负、非错）。POSIX read 在文件末尾的标
准行为。

**Invariant** (id=exfat-read-clamp-by-size)：
读取字节数 `to_read = min(len, ei->size - filep->f_pos)`，绝不读越过 ei->size。
Wave A 不区分 size / valid_size（exfat 上 valid_size <= size，二者由 mount 时
StreamExtension 解析填入；Wave A 简化为以 size 为读上限）。

**Invariant** (id=exfat-read-fat-chain-walk)：
对 `ALLOC_FAT_CHAIN`：从 `ei->start_clu` 起循环调用 `exfat_get_next_cluster`
clu_idx 次得到目标簇；途中遇到 EOF/FREE/越界（大于 num_clusters+1）→ -EINVAL。
对 `ALLOC_NO_FAT_CHAIN`：`cur_clu = ei->start_clu + clu_idx`（线性段）。

**Invariant** (id=exfat-read-cluster-buf-life)：
cluster_buf 在持 ei->inode_lock 期间分配，释放发生在释放锁之前；任意 goto
路径均经过 cluster_buf 释放点。无内存泄漏。

**Invariant** (id=exfat-read-pos-advance-exact)：
返回值 N 与 `filep->f_pos` 推进字节数严格相等（包括 short-read 的 IO 失败路径）；
返回 -errno 时 `filep->f_pos` 不变。

**Invariant** (id=exfat-read-no-mutate-on-failure)：
全错误路径不修改 sbi 任何字段、不修改 ei 任何字段（含 size / valid_size /
start_clu / flags / dir / entry / i_pos / version / num_subdirs / type / attr /
i_size_ondisk）。可能修改 filep->f_pos 当且仅当返回 N >= 0。

**Invariant** (id=exfat-read-bounded-per-iter)：
循环每轮拷贝字节数 chunk = min(to_read - copied, cluster_size - byte_in_clu) <=
cluster_size；每轮 los_part_read 读取 cluster_size / blocksize 个扇区，不跨簇。

**Invariant** (id=exfat-read-le-host-only)：
Wave A 假设 ARMv7-A LE host；FAT 表项字节序由 exfat_get_next_cluster 内部转换；
本路径无需额外 le32 转换。

**Invariant** (id=exfat-read-uses-frozen-sbi-fields)：
本路径只读 sbi 的几何字段：`cluster_size`、`cluster_size_bits`、`blocksize`、
`blocksize_bits`、`clu_offset`、`sect_per_clus_bits`、`num_clusters`、`part_id`。
不读 `vol_amap`、`vol_flags`、`used_clusters`、`clu_srch_ptr`、`boot_buf`。

**Invariant** (id=exfat-read-uses-frozen-ei-fields)：
本路径只读 ei 的不变字段：`start_clu`、`flags`、`size`、`type`、`inode_lock`。
不读 `dir`、`entry`、`i_pos`、`valid_size`、`version`、`num_subdirs`、`attr`、
`i_size_ondisk`（Wave A 简化；valid_size 留 Wave B 处理 sparse / preallocated）。

**Invariant** (id=exfat-read-no-spinlock-callsite)：
本函数体内含 LOS_MemAlloc 与 los_part_read（均可能睡眠），调用方不允许在持
任何 spinlock 时进入本函数。VFS 上层默认不持 spinlock，约束自然成立。

**Invariant** (id=exfat-read-zero-len-fast-path)：
`len == 0` 直接返回 0，不取锁、不分配、不解引用 buf。POSIX read(fd, buf, 0)
的 zero-byte short-circuit 与 Linux 一致。

**Invariant** (id=exfat-read-isdir-rejected)：
`ei->type != TYPE_FILE` 直接返回 `-EISDIR`，不取锁、不分配。VFS 上层通常会先
按 vnode 类型分流（目录走 readdir 而非 read），但本路径作为最后一道断言。

## Refine Prompt

锁约定（Linux exfat read 路径字面对齐）：

1. **取 `ei->inode_lock` 全程持有**：入口
   `LOS_MuxLock(&ei->inode_lock, LOS_WAIT_FOREVER)`，所有 goto 标签后单点
   `LOS_MuxUnlock(&ei->inode_lock)` 再 return。Mirror Linux 上层 VFS 自动持
   inode->i_rwsem（shared 模式）的语义。LiteOS-A 的 LosMux 无 read/write 区分，
   Wave A 用普通 mux 即可（性能损失换正确性，与 Linux 移植惯例一致）。

2. **不取 `sbi->s_lock`**：Linux `generic_file_read_iter` 内部不取，
   `exfat_get_block(create=0)` 也不取。本路径严格对齐。这是与
   `VfsExfatLookup`、`VfsExfatReaddir`（二者均取 sbi->s_lock）的关键差异。
   理由：read 路径不修改 sbi 任何状态字段；并发 read 不同文件不应被全局锁串行化。

3. **不取 `sbi->bitmap_lock`**：read 不分配/释放簇，不读 vol_amap。

4. **不取 `sbi->inode_hash_lock`**：read 不操作 inode hash 表（vp 已由 lookup
   阶段建好并 hash）。

5. **锁序约束**：ei->inode_lock 在最外层；禁止在持 ei->inode_lock 期间反向取
   sbi->s_lock 或 bitmap_lock（本路径不需要这些锁，但若未来扩展需要：必须
   先释放 inode_lock，再按 sbi->s_lock → bitmap_lock 顺序取，避免与
   lookup/balloc 路径死锁）。

6. **spinlock 禁区**：本函数体内 LOS_MemAlloc、los_part_read、memcpy_s 调用
   均可能睡眠或长时占 CPU。调用方不允许在持任何 spinlock 时进入。

7. **可重入安全**：同一 filep 的并发 read（多线程持有同 fd）由 ei->inode_lock
   串行化；不同 file 的并发 read 走不同 ei，互不阻塞——这是不取 s_lock 的
   核心收益。
