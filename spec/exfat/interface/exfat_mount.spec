[PROMPT]
LiteOS-A 实现 exFAT MountOps.Mount 回调 `VfsExfatMount`。本规范覆盖：解析挂载选项字符串、绑定块设备、读 boot 扇区并验证、做严格 boot region CRC32 校验（11 个扇区 + 校验扇区，失败 -EINVAL）、读盘加载 upcase 表、读盘加载分配位图、初始化 inode hash 锁、用 `VnodeAlloc` 创建 root vnode 并经 `VfsHashInsert` 暴露。失败路径必须按反 LIFO 顺序释放所有已分配资源。规范遵循 LiteOS-A 风格：libsec `_s` 安全函数、`LOS_MemAlloc(m_aucSysMem0, ...)` / `LOS_MemFree`、`LosMux` 互斥（mount 路径含磁盘 IO 故选 mux 而非 spin）、磁盘 IO 经 `los_part_read`（**不是** `los_disk_read`，二者索引域不同），错误返回负 POSIX errno（VFS 边界）。v1 mount 自身仅触发只读 IO，但允许卷被以可写方式挂载（写路径在后续阶段实现）。

## First Prompt

[RELY]
```c
/* —— 公共类型来自 spec/exfat/common.header（已 frozen 第一阶段成果）——
 *   exfat_sb_info、exfat_inode_info、exfat_chain、exfat_mount_options、
 *   g_exfatVops、g_exfatFops、g_exfatMountOps。
 * —— 公共导入来自 spec/exfat/interface/interface.header ——
 *   struct MountOps { Mount, Unmount, Statfs, Sync } 真实签名。
 */

/* fs/include/fs/mount.h:44 — VFS 框架结构体（节选）*/
struct Mount {
    const struct MountOps *ops;
    struct Vnode *vnodeBeCovered;     /* 挂载点 vnode（提供权限继承）*/
    struct Vnode *vnodeCovered;       /* FS root；成功路径下由实现写入 */
    void *data;                        /* FS 私有 sbi；成功路径下由实现写入 */
    /* mountLock 由 VFS 框架管理；本回调内调用方持有 */
};

/* fs/vfs/include/vnode.h、path_cache.h */
int  VnodeAlloc(struct VnodeOps *vop, struct Vnode **newVnode);
void VnodeFree(struct Vnode *vnode);
int  VfsHashInsert(struct Vnode *vnode, uint32_t hash);

/* 块设备解析（fs/fat/os_adapt/fatfs.c:fat_bind_check 的成熟范式）*/
struct drv_data;
struct block_operations {
    int (*open)(struct Vnode *);
    int (*close)(struct Vnode *);
    /* ... */
};
typedef struct los_part {
    UINT32 part_id;        /* los_part_read/write 的索引源 */
    char  *part_name;      /* 已被某 FS 占用时非 NULL */
    /* ... */
} los_part;
los_part *los_part_find(struct Vnode *blkDriver);
INT32     SetDiskPartName(los_part *part, const char *name);
INT32     ClearDiskPartName(los_part *part);

/* 块 IO（drivers/block/disk/include/disk.h）—— 必须用 los_part_read，
 * 不可用 los_disk_read：两者索引来自不同命名空间，混用导致
 * "mutex lock failed"（CLAUDE.md 已记录的工程性陷阱）*/
INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

/* 内存与锁 */
extern UINT8 *m_aucSysMem0;
VOID  *zalloc(size_t size);                          /* 内部即 LOS_MemAlloc + memset_s 0 */
UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
UINT32 LOS_MuxInit(LosMux *mutex, const LosMuxAttr *attr);
UINT32 LOS_MuxDestroy(LosMux *mutex);

/* libsec 安全函数（华为安全编码强制）*/
int     memcmp(const void *s1, const void *s2, size_t n);
errno_t memset_s(void *dest, size_t destMax, int c, size_t count);
errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count);

/* 字节序（kernel/include/los_typedef.h 或 arch headers）*/
#define LE16_TO_HOST(x)  /* 在小端 ARM 上为 (x)，大端时需 __builtin_bswap16 */
#define LE32_TO_HOST(x)
#define LE64_TO_HOST(x)

/* —— 助手函数（规范化在后续阶段；本阶段以真实签名引入 [RELY]）—— */

/* fs/exfat/util/exfat_options.c 待生成 —— 解析 mount 选项字符串
 *   入参格式："uid=1000,gid=1000,iocharset=utf8,errors=continue,time_offset=120"
 *   v1 仅接受 utf8；其他 iocharset 返回 -EINVAL。NULL/空串视为接受全部默认。*/
int exfat_parse_options(const char *data, exfat_mount_options *opts);

/* fs/exfat/exfat_dentry.c 待生成 */
int exfat_parse_boot_sector(exfat_sb_info *sbi,
                            const struct exfat_boot_sector *bs,
                            uint32_t logical_sector_size);

/* fs/exfat/util/exfat_chksum.c 待生成 —— exFAT 专用 CRC32 多项式 0x04C11DB7 反向，
 * 与 LOS_Crc32 不兼容（IEEE 多项式不同）—— 必须自实现 */
uint32_t exfat_calc_chksum32(const void *data, uint32_t len, uint32_t chksum, int type);
#define CS_DEFAULT       2
#define CS_BOOT_SECTOR   1

/* fs/exfat/util/exfat_upcase.c 待生成 —— 从盘上 EXFAT_UPCASE 类型 dentry
 * 读取 upcase 表，分配 vol_utbl[65536]。失败时所有已分配释放。*/
int exfat_create_upcase_table(exfat_sb_info *sbi);
void exfat_free_upcase_table(exfat_sb_info *sbi);

/* fs/exfat/exfat_balloc.c 待生成 */
int exfat_load_bitmap(exfat_sb_info *sbi);
void exfat_free_bitmap(exfat_sb_info *sbi);
int exfat_count_used_clusters(exfat_sb_info *sbi, uint32_t *ret_count);

/* 错误码 */
#define EINVAL   22
#define EIO      5
#define ENOMEM   12
#define EBUSY    16
#define ENODEV   19
#define EROFS    30
```

[GUARANTEE]
```c
/*
 * VfsExfatMount: LiteOS-A MountOps.Mount 回调
 *
 * 调用约定：
 *   - 调用方持有 mount->mountLock（VFS 框架保证；本回调不释放、不交还）。
 *   - 调用方不持有任何 sbi 内部锁（sbi 在本函数内才被分配）。
 *   - 路径包含磁盘 IO，禁止从持自旋锁的上下文调用。
 *   - 第二参数 blk 是**块设备 vnode**，不是父目录 vnode（与某些 BSD VFS 不同）。
 *
 * 副作用与返回值：
 *   - 成功 (0)：mount->data = sbi（堆分配，sbi 内含已加载的 boot 副本、bitmap、
 *     upcase 表、已 init 的所有锁、已绑定的 part_id）；mount->vnodeCovered = root vnode
 *     （已经 VfsHashInsert，对外可见）；los_part->part_name = "exfat" 占用块设备。
 *   - 失败 (-errno)：保证不留下任何泄漏。mount->data、mount->vnodeCovered 不被赋值。
 *     具体 errno 见 [SPECIFICATION] Cases。
 *
 * 由本规范保证的全局副作用清单：
 *   - 一次 zalloc/LOS_MemAlloc 失败的 -ENOMEM 路径不会留下半初始化的锁；
 *   - 任何 LOS_MuxDestroy 调用都发生在锁已 init 且未被持有的状态；
 *   - 失败回滚时 ClearDiskPartName 仅在 SetDiskPartName 已成功后调用。
 */
int VfsExfatMount(struct Mount *mount, struct Vnode *blk, const void *data);
```

[SPECIFICATION]

**Pre-Condition**:
- `mount != NULL && blk != NULL`。
- `mount->vnodeBeCovered != NULL`，其 `uid/gid/mode` 字段在 root vnode 创建时被继承。
- `blk->data` 是有效 `struct drv_data`，`bops->open(blk)` 成功后块设备进入"已打开"状态。
- 当前未持有任何 `sbi->*_lock`（sbi 尚未分配）。
- `data` 可为 NULL（视作空选项串）或 `\0` 结尾的 C 字符串。

**Post-Condition**:

**Case 1（成功，返回 0）**：
1. `opts = (exfat_mount_options){...defaults...}`：fs_uid/fs_gid 继承 `mount->vnodeBeCovered`；
   `fs_fmask = fs_dmask = 0022`；`utf8 = 1`；`errors = EXFAT_ERRORS_RO`；其余 0。
2. `exfat_parse_options(data, &opts)` 返回 0；data 中显式给出的字段覆盖默认。
3. `bops->open(blk)` 成功；`part = los_part_find(blk)` 返回非 NULL；
   `part->part_name == NULL`（未被占用）；`SetDiskPartName(part, "exfat")` 返回成功。
4. `sbi = zalloc(sizeof(exfat_sb_info))` 非 NULL；`sbi->options = opts`；
   `sbi->part_id = part->part_id`。
5. `LOS_MuxInit(&sbi->s_lock)`、`LOS_MuxInit(&sbi->bitmap_lock)`、
   `LOS_MuxInit(&sbi->inode_hash_lock)` 都成功；锁均处**未锁定**状态。
6. `sbi->boot_buf = LOS_MemAlloc(m_aucSysMem0, EXFAT_DEFAULT_BLOCKSIZE)` 非 NULL；
   `los_part_read(part->part_id, sbi->boot_buf, 0, 1, TRUE)` 返回 ≥ 0。
7. `exfat_parse_boot_sector(sbi, (struct exfat_boot_sector *)sbi->boot_buf,
   1U << ((struct exfat_boot_sector *)sbi->boot_buf)->sect_size_bits)` 返回 0；
   sbi 几何字段（`sect_size_bits, sect_per_clus_bits, num_fats, partition_offset,
   vol_length, fat_offset, fat_length, fat2_offset, clu_offset, num_clusters,
   root_dir, cluster_size, cluster_size_bits, blocksize, blocksize_bits,
   dentries_per_clu, vol_flags, vol_flags_persistent, s_maxbytes`) 已字节序转换填入。
8. **严格 boot region 校验**：依次读 `sn ∈ {0..10}` 共 11 个扇区，调用
   `exfat_calc_chksum32(buf, blocksize, chksum, sn ? CS_DEFAULT : CS_BOOT_SECTOR)`
   累计 chksum；再读 `sn=11`（校验扇区），其每 4 字节 LE32 必须等于累计 chksum，
   否则失败（见 Case 4）。所有读使用 `los_part_read`。
9. `exfat_create_upcase_table(sbi)` 返回 0；`sbi->vol_utbl` 非 NULL，包含从盘上
   `EXFAT_UPCASE` 类型 dentry 指定起始簇/大小读取的 upcase 表。
10. `exfat_load_bitmap(sbi)` 返回 0；`sbi->vol_amap` 非 NULL，
    大小 `sbi->map_sectors * sbi->blocksize`，内容来自盘上 `EXFAT_BITMAP` dentry。
11. `exfat_count_used_clusters(sbi, &sbi->used_clusters)` 返回 0。
12. 新分配 root vnode：
    - `vp = NULL; VnodeAlloc(&g_exfatVops, &vp)` 返回 0，`vp != NULL`；
    - `inode_info = zalloc(sizeof(exfat_inode_info))` 非 NULL；
    - `LOS_MuxInit(&inode_info->inode_lock)` 成功；
    - `inode_info->dir = (exfat_chain){sbi->root_dir, 0, ALLOC_FAT_CHAIN}`；
    - `inode_info->entry = -1`；`inode_info->type = TYPE_DIR`；
    - `inode_info->start_clu = sbi->root_dir`；`inode_info->flags = ALLOC_FAT_CHAIN`；
    - `inode_info->i_pos = ((uint64_t)sbi->root_dir << 32) | 0xFFFFFFFFu`；
    - `vp->fop = &g_exfatFops`；`vp->data = inode_info`；
    - `vp->originMount = mount`；`vp->parent = mount->vnodeBeCovered`；
    - `vp->type = VNODE_TYPE_DIR`；`vp->uid = mount->vnodeBeCovered->uid`；
      `vp->gid = mount->vnodeBeCovered->gid`；`vp->mode = mount->vnodeBeCovered->mode`；
    - `VfsHashInsert(vp, sbi->root_dir)` 返回成功——**这是 vnode 首次对外可见的时刻**，
      在此之前所有锁、所有 sbi 字段、所有 inode_info 字段必须已就绪。
13. `mount->data = sbi`；`mount->vnodeCovered = vp`。
14. `vol_flags & VOLUME_DIRTY` 时仅 `PRINT_WARN("Volume was not properly unmounted")`，
    不返回错误；`MEDIA_FAILURE` 同处理。

**Case 2（选项解析失败，返回 `-EINVAL`）**：
- `exfat_parse_options(data, &opts)` 返回非 0：未知 key、超界 time_offset、非 utf8 iocharset。
- 此时未分配 sbi，无回滚动作。

**Case 3（boot sector 内容非法，返回 `-EINVAL`）**：
满足下列任一：
- `LE16(bs->signature) != BOOT_SIGNATURE (0xAA55)`；
- `memcmp(bs->fs_name, "EXFAT   ", 8) != 0`（8 字节带尾空格）；
- `bs->must_be_zero[i]` 任一字节非零（防 FAT 卷误挂）；
- `bs->sect_size_bits ∉ [9, 12]`；
- `bs->num_fats ∉ {1, 2}`；
- `bs->sect_per_clus_bits > 25 - bs->sect_size_bits`；
- `(num_FAT_sectors << sect_size_bits) < num_clusters * 4`（FAT 太小放不下簇表）；
- `data_start_sector < FAT1_start_sector + num_FAT_sectors * num_fats`（数据区与 FAT 重叠）。

**Case 4（boot region CRC32 不匹配，返回 `-EINVAL`）**：
- 严格模式：累计 chksum 与扇区 11 中任一 4 字节 LE32 不匹配即失败。
- 已分配的 sbi、boot_buf、锁等按 Invariant `exfat-mount-rollback-lifo` 全部释放。

**Case 5（IO 错误，返回 `-EIO`）**：
- `los_part_read` 任一调用返回负值；
- 或 `exfat_create_upcase_table` / `exfat_load_bitmap` / `exfat_count_used_clusters`
  内部 IO 失败。

**Case 6（内存不足，返回 `-ENOMEM`）**：
- `zalloc` / `LOS_MemAlloc` / `VnodeAlloc` 任一返回 NULL 或非 0。
- 已分配资源严格按反 LIFO 顺序释放。

**Case 7（设备绑定失败）**：
- `bops->open(blk)` 返回非 0：返回其错误码（典型为 `-EIO` 或 `-ENXIO`）。
- `los_part_find(blk) == NULL`：返回 `-ENODEV`。
- `part->part_name != NULL`（已被其它 FS 挂载）：返回 `-EBUSY`。

**Invariant** (id=exfat-mount-readonly-during-mount):
v1 mount 路径自身**永不调用** `los_part_write` / `los_disk_write`。即使卷被以可写
方式挂载，写路径在 Mount 之外的回调（如 file write、unlink）中触发。本约束保护
mount 失败回滚时不留下脏数据；后续阶段若新增 mount-time 写入需开新阶段重审。

**Invariant** (id=exfat-mount-rollback-lifo):
所有失败分支都按反 LIFO 顺序释放：
```
ERROR_HASH:    VnodeFree(vp);
ERROR_VNODE:   LOS_MuxDestroy(&inode_info->inode_lock);
               LOS_MemFree(m_aucSysMem0, inode_info);
ERROR_INODE:   exfat_count_used_clusters 已运行（无资源持有）；
ERROR_BITMAP:  exfat_free_bitmap(sbi);
ERROR_UPCASE:  exfat_free_upcase_table(sbi);
ERROR_BOOT:    LOS_MemFree(m_aucSysMem0, sbi->boot_buf);
ERROR_LOCKS:   LOS_MuxDestroy(&sbi->inode_hash_lock);
               LOS_MuxDestroy(&sbi->bitmap_lock);
               LOS_MuxDestroy(&sbi->s_lock);
ERROR_SBI:     LOS_MemFree(m_aucSysMem0, sbi);
ERROR_PARTNAME:ClearDiskPartName(part);
ERROR_OPEN:    /* bops->open 已失败或未调用，无需 close */
ERROR_OPTS:    return -ret;
```
内部使用正值 errno、最终 `return -ret` —— 与 fatfs_mount 一致的 goto-stack 风格。

**Invariant** (id=exfat-mount-mount-data-self-consistent):
成功路径下 `mount->data == sbi && mount->vnodeCovered != NULL && mount->vnodeCovered->originMount == mount`。本双向自洽是 umount 路径的前提。

**Invariant** (id=exfat-mount-vnode-visible-after-init):
vnode 经 `VfsHashInsert` 暴露之前，其 `vp->fop`、`vp->data`、`vp->data->inode_lock`、
sbi 的所有锁均已 `LOS_MuxInit`。读 vnode 的并发线程不会观察到半初始化状态。

**Invariant** (id=exfat-mount-fsmap-entry-name):
本 spec 要求生成的 `exfat_super.c` 在文件作用域包含
`FSMAP_ENTRY(exfat_fsmap, "exfat", g_exfatMountOps, FALSE, TRUE);`，使 mount 命名空间
将字符串 `"exfat"` 映射到 `g_exfatMountOps`。零运行时注册——链接器表机制（CLAUDE.md "Link-time tables" 一节）。

## Refine Prompt

[RELY]
```c
UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
UINT32 LOS_MuxUnlock(LosMux *mutex);
#define LOS_WAIT_FOREVER 0xFFFFFFFFu
```

[SPECIFICATION of VfsExfatMount —— 锁与并发约束]

**Pre-Condition (lock state)**:
- 调用方持有 `mount->mountLock`（VFS 框架保证；本回调既不释放也不重新获取它）。
- 不持有任何 `sbi` 内部锁（这些锁尚不存在）。
- 不持有任何 vnode 锁（root vnode 尚不存在）。

**Post-Condition (lock state)**:
- 返回时调用方继续持有 `mount->mountLock`，与调用前严格一致。
- 成功路径：`sbi->s_lock`、`sbi->bitmap_lock`、`sbi->inode_hash_lock`、
  `inode_info->inode_lock` 均已 `LOS_MuxInit` 且处于**未锁定**状态。
- 失败路径：所有 `LOS_MuxDestroy` 调用都发生在锁未被持有的窗口；不允许销毁仍持锁的 mux。

**初始化顺序约束（避免可见性窗口）**:
1. 先 `LOS_MuxInit(&sbi->s_lock)`、`bitmap_lock`、`inode_hash_lock`，
2. 再读 boot sector（IO 期间任何并发线程都还看不到 sbi/vnode），
3. 再 `LOS_MuxInit(&inode_info->inode_lock)`，
4. 之后才 `mount->data = sbi`，
5. 之后才 `vp->data = inode_info`、`vp->fop = &g_exfatFops`，
6. 最后 `VfsHashInsert(vp, sbi->root_dir)` —— **vnode 首次对外可见的时刻**。

**死锁分析**:
mount 路径不持任何已对外暴露 vnode 的锁，与现有锁图无环路。`exfat_create_upcase_table`、
`exfat_load_bitmap` 内部各自使用 `sbi->bitmap_lock` 时已是 sbi 已分配阶段，但因
sbi 尚未通过 `mount->data` 暴露，唯一持锁者就是当前 mount 线程，故不可能与外部线程
形成 AA 死锁。

**回滚的锁前提**:
任一 `LOS_MuxDestroy(&sbi->X_lock)` 调用必须满足：
（a）该锁已 `LOS_MuxInit` 成功；
（b）当前线程不持该锁；
（c）任何其它线程都不可能在持该锁——由 Invariant `exfat-mount-vnode-visible-after-init`
    保证：vnode 未 VfsHashInsert 之前外部不可见；外部线程无法获取 sbi 引用，因此
    无法持有 sbi 内任何锁。

满足以上三条，goto-stack 回滚即安全。
