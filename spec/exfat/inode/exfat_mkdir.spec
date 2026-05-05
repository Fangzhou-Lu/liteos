[PROMPT]
Wave B Stage 4e：在 LiteOS-A 端把 Linux exFAT 的 `mkdir` 路径完整移植成
**单 stage** 输出——一次性给齐 5 个内部 helper（calc_num_entries /
zeroed_cluster / alloc_new_dir / init_dir_entry / init_ext_entry）+ 1 个
组合器（add_entry）+ 1 个 VOP 入口（VfsExfatMkdir），由 user 决策矩阵
Q1=a 锁定。Linux 上游对应：`fs/exfat/namei.c::exfat_mkdir` 行 837-880、
`exfat_add_entry` 行 471-542、`fs/exfat/dir.c::exfat_alloc_new_dir` 行
319-330、`exfat_calc_num_entries` 行 332-342、`exfat_init_dir_entry`
行 441-491、`exfat_init_ext_entry` 行 529-570、`exfat_update_dir_chksum`
行 493-527、`fs/exfat/fatent.c::exfat_zeroed_cluster` 行 275-319。

Linux/LiteOS-A 主要差异：
- Linux 用 buffer_head + `sb_getblk` + `exfat_update_bh` + `brelse` 串接每次
  IO；本端没有 buffer cache，统一用已批准的 `exfat_set_dentry` /
  `exfat_set_dentry_set`（Stage 4a）+ `los_part_write`（zeroed_cluster
  专用）做 read-modify-write，与 Wave B 既有写路径同范式。
- Linux `exfat_resolve_path` 把 `dentry->d_name.name` 解析为 uniname；
  LiteOS-A VFS 在 `VnodeOps.Mkdir` 时已经把 `parent_vp` 与 leaf `name`
  分开传入，本 stage 只做 `exfat_utf8_to_uni` 一步即可，**不**移植
  resolve_path 整套（namespace/upcase 在 lookup 阶段已就绪）。
- Linux 在 `exfat_alloc_cluster` 调用前必须先 `exfat_find_empty_entry`
  以防 alloc 后无 slot 写 dentry；LiteOS-A 沿用相同顺序：先 alloc_dentry_slot
  再 alloc_new_dir，与 Linux 一致；alloc_new_dir 失败时**不**回滚已找
  到的 slot（Q3，下文展开）。
- Linux 通过 `inode_inc_iversion` / `current_time` / `mark_inode_dirty`
  把 parent inode 的 mtime/ctime + nlink 持久化；本 stage 仅在内存层
  更新（parent_ei->num_subdirs++），**不**写盘——与 Wave B 既有
  truncate VOP "不持久化 dentry" 的 v1 限制一致，留给后续 sync helper。
- Linux 失败路径（init_dir_entry / init_ext_entry 出错）保留已分配的
  目录簇与已写的 dentry slot 不回滚——本端**严格沿用**该策略
  （Q3=a），由 `exfat_set_volume_dirty` / `exfat_clear_volume_dirty`
  在 VOP 顶层 bracket 整段 add_entry，让下次 mount fsck 修复孤儿。

Out of scope（明确不做）：
- `Create` VOP（Stage 4d）：本 stage 的 `exfat_add_entry` 形参里保留
  `type` 入口，但 `TYPE_FILE` 分支 v1 直接 `return -ENOSYS`；4d 单独
  evolve 引入 `start_clu = EXFAT_EOF_CLUSTER / size = 0` 的文件分支。
- `Unlink` / `Rmdir` / `Rename`（4f / 4g / 4h+）：与本 stage 互斥的
  写路径，由后续阶段独立交付。
- `exfat_extend_dir`：父目录 dentry 区满 → -ENOSPC 即返回（继承
  Stage 4b `exfat-alloc-slot-no-grow` invariant）。
- 持久化 parent dir 的 mtime/ctime/num_subdirs：caller 在后续 sync 路径
  补齐；本 stage 仅 in-memory 更新 ei->num_subdirs。
- `IS_DIRSYNC` 同步策略：v1 写路径**始终同步**（los_part_write 自身
  即同步），无需细分 sync/async。

[RELY]
```c
/* common.header 已声明的类型与常量；本节只列本 stage 新增 / 必须显式
 * 引用的 RELY，避免重列已有 64 个 export。 */

typedef struct exfat_sb_info       exfat_sb_info;
typedef struct exfat_chain         exfat_chain;
typedef struct exfat_inode_info    exfat_inode_info;
struct exfat_dentry;               /* 32B 联合，定义在 exfat_raw.h */
struct Vnode;
struct Mount;

/* —— 本 stage 新引入的两类 in-memory 结构（待 code_gen_approve
 * 阶段写入 common.header；本 spec 仅做声明锚点）—— */

/* Linux struct exfat_uni_name 移植：UTF-16 leaf name + hash + length。
 * 由 add_entry 内 exfat_utf8_to_uni 装填，转手喂 init_ext_entry。 */
struct exfat_uni_name {
    uint16_t name[EXFAT_MAX_NAME_LEN + 1];   /* UTF-16 + NUL */
    uint16_t name_hash;                       /* upcase chksum16(CS_DEFAULT) */
    uint8_t  name_len;                        /* code-unit count，不含 NUL */
};

/* Linux struct exfat_dir_entry 的 LiteOS-A 简化版：
 * 由 exfat_add_entry 装填，转手喂 VfsExfatMkdir 用以填充新 ei。 */
struct exfat_dir_entry {
    exfat_chain dir;            /* 父目录簇链 */
    int32_t     entry;          /* 父目录中本 file dentry 的线性索引 */
    uint32_t    type;           /* TYPE_DIR / TYPE_FILE */
    uint16_t    attr;           /* ATTR_SUBDIR / ATTR_ARCHIVE */
    uint32_t    start_clu;      /* TYPE_DIR: 新目录簇；TYPE_FILE: EOF */
    uint8_t     flags;          /* ALLOC_NO_FAT_CHAIN（dir 单簇） */
    uint64_t    size;           /* TYPE_DIR: cluster_size；TYPE_FILE: 0 */
    uint32_t    num_subdirs;    /* TYPE_DIR: EXFAT_MIN_SUBDIR(=2)，TYPE_FILE: 0 */
};

/* exFAT raw 常量（来自 exfat_raw.h，本 stage 高频使用）*/
#define DENTRY_SIZE             32u
#define EXFAT_FIRST_CLUSTER     2u
#define EXFAT_EOF_CLUSTER       0xFFFFFFFFu
#define EXFAT_FILE              0x85u    /* primary file dentry type */
#define EXFAT_STREAM            0xC0u    /* secondary stream extension */
#define EXFAT_NAME              0xC1u    /* secondary name extension */
#define EXFAT_FILE_NAME_LEN     15       /* UTF-16 units per name dentry */
#define EXFAT_MIN_SUBDIR        2        /* "." + ".." 占位 */
#define ATTR_SUBDIR             0x0010u
#define ATTR_ARCHIVE            0x0020u
#define ALLOC_FAT_CHAIN         0x01u
#define ALLOC_NO_FAT_CHAIN      0x03u
#define TYPE_DIR                0x0104u
#define TYPE_FILE               0x011Fu
#define CS_DIR_ENTRY            0
#define CS_DEFAULT              2

/* 已批准 stage 公共 API（来自 common.header；引用而不重复签名）*/
extern int  exfat_alloc_cluster(exfat_sb_info *sbi, uint32_t num_alloc,
                                exfat_chain *p_chain);
extern int  exfat_clear_bitmap(exfat_sb_info *sbi, uint32_t clu);
extern int  exfat_set_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                             int entry_idx, const struct exfat_dentry *in);
extern int  exfat_set_dentry_set(const exfat_sb_info *sbi,
                                 const exfat_chain *dir, int start_entry,
                                 const struct exfat_dentry *set,
                                 int num_entries);
extern int  exfat_get_dentry(const exfat_sb_info *sbi, const exfat_chain *dir,
                             int entry_idx, struct exfat_dentry *out,
                             uint64_t *out_sector);
extern int  exfat_alloc_dentry_slot(const exfat_sb_info *sbi,
                                    const exfat_chain *dir, int n_entries,
                                    int *slot_idx_out);
extern uint16_t exfat_calc_chksum16(const void *data, int len, uint16_t chksum,
                                    int type);
extern int  exfat_set_volume_dirty(exfat_sb_info *sbi);
extern int  exfat_clear_volume_dirty(exfat_sb_info *sbi);
extern int  exfat_utf8_to_uni(const char *utf8, int utf8_len, uint16_t *uni,
                              int uni_max, int *uni_len);
extern int  exfat_inode_alloc(exfat_inode_info **out);
extern void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu);

/* 内核原语 */
extern VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern VOID  *m_aucSysMem0;
extern UINT32 LOS_MuxLock(LosMux *mutex, UINT32 timeout);
extern UINT32 LOS_MuxUnlock(LosMux *mutex);
extern errno_t memset_s(void *dest, size_t destMax, int c, size_t count);
extern errno_t memcpy_s(void *dest, size_t destMax, const void *src,
                         size_t count);
extern void   PRINT_ERR(const char *fmt, ...);
extern INT32  los_part_write(INT32 part_id, const VOID *buf, UINT64 sector,
                             UINT32 count);

/* VFS 钩子（lookup / readdir 已批准 stage 已存在；本 stage 复用）*/
extern int  VnodeAlloc(struct VnodeOps *vop, struct Vnode **newVnode);
extern int  VfsHashInsert(struct Vnode *vnode, uint32_t hash);
extern struct VnodeOps g_exfatVops;

#define LOS_WAIT_FOREVER  0xFFFFFFFFu
#define EOK               0
```

[GUARANTEE]
```c
/* ============================================================================
 * Helper 1: exfat_calc_num_entries
 *
 * 调用约定：
 *   - 纯函数；不取锁；不发起 IO；不分配内存。
 *   - 入参 p_uniname != NULL 且 name_len > 0。
 *   - Linux 公式：1 file + 1 stream + ceil(name_len / 15) name 项
 *     即 `((name_len - 1) / EXFAT_FILE_NAME_LEN) + 3`。
 *   - 返回值：
 *       >= 3      ：dentry-set 总条数（必落入 [3, EXFAT_DENTRY_SET_MAX]）。
 *       -EINVAL   ：p_uniname == NULL 或 name_len == 0 或 name_len > 255。
 * ========================================================================== */
extern int exfat_calc_num_entries(const struct exfat_uni_name *p_uniname);

/* ============================================================================
 * Helper 2: exfat_zeroed_cluster
 *
 * 调用约定：
 *   - 调用方进入时**不**持任何锁；helper 自身 lock-free（只对 sbi 只读字段
 *     和 part_id 一次性 IO）。
 *   - 把簇号 clu 对应的 sbi->sect_per_clus 个连续扇区**逐扇区**清零写盘
 *     （Linux 的 `exfat_zeroed_cluster` 在 buffer_head 层批量写；本端按
 *     单扇区 los_part_write 循环以与 Wave B 既有写路径同范式）。
 *   - blocksize 缓冲：在栈上分配 EXFAT_DEFAULT_BLOCKSIZE（512）字节有上限风险；
 *     **必须**使用 `LOS_MemAlloc(m_aucSysMem0, sbi->blocksize)` 取堆缓冲，一次
 *     性 memset_s 清零后循环复用。
 *   - 返回值：
 *       0          ：所有 sect_per_clus 个扇区落盘成功。
 *       -EINVAL    ：sbi == NULL；clu < FIRST_CLUSTER；clu >= num_clusters；
 *                    sbi->blocksize == 0；sbi->sect_per_clus_bits 非法。
 *       -ENOMEM    ：堆缓冲分配失败。
 *       -EIO       ：任一扇区 los_part_write 失败 / memset_s 失败；
 *                    部分提交：已写入的扇区保留为 0，不回滚（caller
 *                    在 alloc_new_dir 内会清 bitmap 释放该簇）。
 * ========================================================================== */
extern int exfat_zeroed_cluster(exfat_sb_info *sbi, uint32_t clu);

/* ============================================================================
 * Helper 3: exfat_alloc_new_dir
 *
 * 调用约定：
 *   - 调用方持 sbi->s_lock（即 mkdir VOP 顶层锁）；自身不取额外锁
 *     （exfat_alloc_cluster 内部自取 bitmap_lock）。
 *   - clu_out->flags 必须由本 helper 自行设为 ALLOC_NO_FAT_CHAIN（v1
 *     单簇目录）；caller 不需要预填。
 *   - 步骤：
 *       1. 初始化 *clu_out = { dir = EOF, size = 0, flags = ALLOC_NO_FAT_CHAIN }；
 *          但 alloc_cluster v1 强制 flags == ALLOC_FAT_CHAIN，所以本 helper
 *          先把 flags 临时设 ALLOC_FAT_CHAIN 喂 alloc_cluster，再回填
 *          ALLOC_NO_FAT_CHAIN（与 Linux exfat_alloc_new_dir 一致：单簇
 *          chain 实质 NO_FAT，但 alloc 路径走 FAT 派系，alloc 后 flag 翻
 *          回 NO_FAT 表"chain 仅 1 簇，不需要 FAT walk"）。
 *       2. exfat_zeroed_cluster(sbi, clu_out->dir)：对**这个 helper**
 *          独享的 1 簇，失败则**回滚**：调 exfat_clear_bitmap + ent_set
 *          (clu_out->dir, EXFAT_FREE_CLUSTER) 释放该簇 → 透传错误。
 *          理由：alloc_new_dir 是它自己的"原子单元"，让上层 add_entry
 *          失去回滚负担（与 Q3 全局 no-rollback 例外一致）。
 *   - 返回值：
 *       0          ：成功；clu_out->dir 是新簇号；clu_out->size = 1；
 *                    clu_out->flags = ALLOC_NO_FAT_CHAIN；簇内已全 0。
 *       -EINVAL    ：sbi/clu_out NULL。
 *       -ENOSPC    ：alloc_cluster 报告无可用簇。
 *       -EIO       ：alloc_cluster 或 zeroed_cluster 任一失败；rollback
 *                    路径已释放；clu_out 状态：dir = EOF，size = 0。
 * ========================================================================== */
extern int exfat_alloc_new_dir(exfat_sb_info *sbi, exfat_chain *clu_out);

/* ============================================================================
 * Helper 4: exfat_init_dir_entry
 *
 * 调用约定：
 *   - 调用方持 sbi->s_lock；helper 自身 lock-free（调 exfat_set_dentry
 *     两次，set_dentry 已规定无锁；timestamp 取 LiteOS-A 系统时间快照
 *     `exfat_set_entry_time` 静态 inline，留给 code_gen 阶段从 Wave A
 *     既有 helper 复用——本 spec 只声明语义而不强 RELY）。
 *   - 在 [entry, entry+1] 两个槽位**写入**初始化的 file dentry（0x85）
 *     与 stream dentry（0xC0）；其余 num_entries-2 个 name dentry 由
 *     exfat_init_ext_entry 填。
 *   - file dentry：
 *       type           = EXFAT_FILE (0x85)
 *       attr           = (type == TYPE_DIR) ? ATTR_SUBDIR : ATTR_ARCHIVE
 *       create/modify/access 时间字段 = 当前时间快照（exfat_set_entry_time）
 *       num_ext        = 0（init_ext_entry 后续覆写为 num_entries-1）
 *       checksum       = 0（init_ext_entry 后续覆写为 chksum16）
 *   - stream dentry：
 *       type           = EXFAT_STREAM (0xC0)
 *       flags          = (type == TYPE_FILE) ? ALLOC_FAT_CHAIN : ALLOC_NO_FAT_CHAIN
 *       valid_size     = size（即 cluster_size for TYPE_DIR；0 for TYPE_FILE）
 *       size           = size（同上）
 *       start_clu      = start_clu（dir 路径下为 alloc_new_dir 给的簇号；
 *                         file 路径下为 EXFAT_EOF_CLUSTER 表示空文件）
 *       name_len       = 0（init_ext_entry 后续覆写）
 *       name_hash      = 0（init_ext_entry 后续覆写）
 *
 *   - 返回值：
 *       0          ：两个 set_dentry 都成功；entry / entry+1 已落盘。
 *       -EINVAL    ：sbi/p_dir NULL；type != TYPE_DIR 且 type != TYPE_FILE；
 *                    entry < 0。
 *       其他       ：set_dentry 错误透传（-EIO/-ENOMEM）；前一槽可能
 *                    已写入；caller 在 add_entry 顶层不回滚（Q3）。
 * ========================================================================== */
extern int exfat_init_dir_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                int entry, uint32_t type, uint32_t start_clu,
                                uint64_t size);

/* ============================================================================
 * Helper 5: exfat_init_ext_entry
 *
 * 调用约定：
 *   - 调用方持 sbi->s_lock；helper 自身 lock-free。
 *   - **必须**在 exfat_init_dir_entry 之后调用——因为本 helper 要回填
 *     file dentry 的 num_ext 与 checksum 字段，stream dentry 的 name_len
 *     与 name_hash 字段。
 *   - 步骤：
 *       1. exfat_get_dentry(entry) → 取 file dentry，覆写 num_ext =
 *          num_entries - 1 → exfat_set_dentry 落盘。
 *       2. exfat_get_dentry(entry+1) → 取 stream dentry，覆写
 *          name_len = p_uniname->name_len、name_hash = p_uniname->name_hash
 *          → exfat_set_dentry 落盘。
 *       3. 循环 i = 2..num_entries-1：构造 EXFAT_NAME (0xC1) dentry，
 *          unicode_0_14[k] = p_uniname->name[(i-2)*15 + k] for k ∈ [0,15)
 *          → exfat_set_dentry 落盘。
 *       4. 重新读 entry 与 entry+1 + 名字 dentry 共 num_entries 项，
 *          调 exfat_calc_chksum16(file_dentry, DENTRY_SIZE, 0, CS_DIR_ENTRY)
 *          作为 seed；后续 num_entries-1 项以 CS_DEFAULT 续算；得到
 *          整个 dentry-set 的 chksum16 → 回写 file dentry 的 checksum
 *          字段并 set_dentry 落盘。
 *   - 返回值：
 *       0          ：所有 dentry 写入并 chksum 回填成功。
 *       -EINVAL    ：sbi/p_dir/p_uniname NULL；num_entries < 3；
 *                    num_entries > EXFAT_DENTRY_SET_MAX；entry < 0；
 *                    p_uniname->name_len > 255。
 *       -EIO/-ENOMEM：set_dentry / get_dentry 错误透传；不回滚已写槽位。
 * ========================================================================== */
extern int exfat_init_ext_entry(exfat_sb_info *sbi, const exfat_chain *p_dir,
                                int entry, int num_entries,
                                const struct exfat_uni_name *p_uniname);

/* ============================================================================
 * Helper 6: exfat_add_entry  (composer)
 *
 * 调用约定：
 *   - 调用方持 sbi->s_lock；helper 自身 lock-free（依赖的 alloc_cluster
 *     自取 bitmap_lock）。
 *   - 编排顺序（与 Linux fs/exfat/namei.c::exfat_add_entry 行 471-542 同）：
 *       1. exfat_utf8_to_uni(name, ..., uniname.name, EXFAT_MAX_NAME_LEN+1,
 *          &uniname.name_len)；name_len == 0 → -EINVAL。
 *       2. uniname.name_hash = exfat_calc_chksum16(uniname.name,
 *          uniname.name_len * 2, 0, CS_DEFAULT)（直接对 UTF-16 字节流
 *          chksum；Linux 的 upcase 匹配在 lookup 阶段做，name_hash 字段
 *          只是快速 mismatch 过滤，本端用未 upcase 的 hash 兼容）。
 *       3. num_entries = exfat_calc_num_entries(&uniname)；< 0 透传。
 *       4. 取 parent_ei = parent_vp->data；p_dir = parent_ei->dir 副本，
 *          但 p_dir.dir = parent_ei->start_clu，size/flags 从 ei 拷贝；
 *          调 exfat_alloc_dentry_slot(&p_dir, num_entries, &dentry_idx)；
 *          返回 -ENOSPC / -EIO 透传。
 *       5. **TYPE_DIR 分支**（v1 唯一支持）：
 *          - exfat_alloc_new_dir(sbi, &new_clu)；失败 → 透传，不释放
 *            slot（Q3=a：no-rollback）。
 *          - start_clu = new_clu.dir；size = sbi->cluster_size。
 *       6. **TYPE_FILE 分支**：v1 直接 `return -ENOSYS`；Stage 4d evolve。
 *       7. exfat_init_dir_entry(sbi, &p_dir, dentry_idx, type, start_clu,
 *          size)；失败 → 透传；不释放 slot 也不释放 new_clu（Q3）。
 *       8. exfat_init_ext_entry(sbi, &p_dir, dentry_idx, num_entries,
 *          &uniname)；失败 → 透传；不回滚（Q3）。
 *       9. 装填 *info：
 *          info->dir         = p_dir
 *          info->entry       = dentry_idx
 *          info->type        = type        (TYPE_DIR)
 *          info->attr        = ATTR_SUBDIR (TYPE_DIR)
 *          info->start_clu   = start_clu
 *          info->flags       = ALLOC_NO_FAT_CHAIN
 *          info->size        = size
 *          info->num_subdirs = EXFAT_MIN_SUBDIR (=2)
 *
 *   - 返回值：
 *       0          ：dentry-set + 新目录簇全部就绪。
 *       -EINVAL    ：sbi/parent_vp/name/info NULL；name 解析后 name_len
 *                    == 0 或越界；type 非 TYPE_DIR / TYPE_FILE。
 *       -ENOSYS    ：v1 type == TYPE_FILE 分支。
 *       -ENOSPC    ：alloc_dentry_slot 或 alloc_new_dir 报告无空间。
 *       -EIO       ：底层 set_dentry / alloc_cluster / zeroed_cluster
 *                    失败；信息以 *info 传出（部分字段可能未填）；
 *                    caller 必须在 vol_dirty bracket 内消化。
 * ========================================================================== */
extern int exfat_add_entry(exfat_sb_info *sbi, struct Vnode *parent_vp,
                           const char *name, uint32_t type,
                           struct exfat_dir_entry *info);

/* ============================================================================
 * VOP entry: VfsExfatMkdir
 *
 * 调用约定：
 *   - 由 LiteOS-A VFS 在 sys_mkdir 路径上调本 VOP；caller **不**持任何
 *     sbi / ei 锁。
 *   - 入参约束：parent_vp != NULL；parent_vp->originMount != NULL；
 *               parent_vp->data != NULL；parent_vp->originMount->data
 *               != NULL；name != NULL && name[0] != '\0'；vpp != NULL。
 *   - 步骤：
 *       1. 参数校验失败 → 直接 -EINVAL；不取锁。
 *       2. sbi = parent_vp->originMount->data；
 *          parent_ei = parent_vp->data。
 *       3. LOS_MuxLock(&sbi->s_lock, LOS_WAIT_FOREVER)。
 *       4. exfat_set_volume_dirty(sbi)（成功为 0；失败仅 PRINT_ERR
 *          报警，不阻断 mkdir——与 Wave B Stage 2a 既有写路径一致）。
 *       5. 准备 struct exfat_dir_entry info，全 0 init。
 *       6. err = exfat_add_entry(sbi, parent_vp, name, TYPE_DIR, &info)。
 *       7. exfat_clear_volume_dirty(sbi)（无论成败；失败 PRINT_ERR
 *          报警）。
 *       8. err != 0 → goto unlock；返回 err。
 *       9. exfat_inode_alloc(&new_ei)；失败 → 不回滚 dentry-set / 新目录簇
 *          （Q3：留盘，fsck 修；vol_dirty 已 clear，但 fsck 仍会扫到孤儿
 *          dir cluster——本是 v1 已知泄漏）；返 -ENOMEM。
 *      10. 装填 new_ei：
 *           dir         = info.dir
 *           entry       = info.entry
 *           type        = info.type        (TYPE_DIR)
 *           attr        = info.attr        (ATTR_SUBDIR)
 *           start_clu   = info.start_clu
 *           flags       = info.flags       (ALLOC_NO_FAT_CHAIN)
 *           size        = info.size        (cluster_size)
 *           valid_size  = info.size
 *           i_size_ondisk = info.size
 *           num_subdirs = info.num_subdirs (2)
 *           i_pos       = ((uint64_t)info.start_clu << 32) | info.entry
 *           exfat_inode_init_dir_chain(new_ei, info.start_clu) 后由
 *           调用方覆写 size / type 等。
 *      11. VnodeAlloc(&g_exfatVops, &new_vp)；失败 → exfat_inode_free +
 *           返 -ENOMEM；同样 v1 不回滚盘上 dentry。
 *      12. 把 new_ei 关联到 new_vp：new_vp->data = new_ei；new_vp->type
 *           = VNODE_TYPE_DIR；new_vp->parent = parent_vp；new_vp->originMount
 *           = parent_vp->originMount。
 *      13. VfsHashInsert(new_vp, hash = info.start_clu)。
 *      14. parent_ei->num_subdirs += 1（in-memory only；不写盘）。
 *      15. *vpp = new_vp。
 *      16. unlock: LOS_MuxUnlock(&sbi->s_lock)；返回 err。
 *
 *   - 返回值：
 *       0          ：成功；*vpp 指向新目录 vnode；caller 拿到的 vnode
 *                    已加入 VFS hash，可直接被 lookup / readdir 访问。
 *       -EINVAL    ：参数校验失败。
 *       -ENOSPC    ：add_entry 透传。
 *       -ENOSYS    ：v1 不应触发（VfsExfatMkdir 内部硬定 type=TYPE_DIR）；
 *                    若发生说明常量错配。
 *       -EIO       ：add_entry 透传；vol_dirty 已 set，caller 视
 *                    bracket 信号，下次 mount fsck 修。
 *       -ENOMEM    ：inode_alloc / VnodeAlloc 失败；盘上孤儿待 fsck。
 * ========================================================================== */
extern int VfsExfatMkdir(struct Vnode *parent_vp, const char *name,
                         mode_t mode, struct Vnode **vpp);
```

[SPECIFICATION]

**Pre-Condition (exfat_calc_num_entries)**:
- `p_uniname != NULL`；`1 <= p_uniname->name_len <= EXFAT_MAX_NAME_LEN`。

**Post-Condition (Case 1: success)**:
- 返回 `((p_uniname->name_len - 1) / EXFAT_FILE_NAME_LEN) + 3`；落入
  [3, 19]。例：name_len=1 → 3；name_len=15 → 3；name_len=16 → 4；
  name_len=255 → 19。

**Post-Condition (Case 2: -EINVAL)**:
- p_uniname == NULL 或 name_len == 0 或 name_len > 255 → -EINVAL。

**Pre-Condition (exfat_zeroed_cluster)**:
- `sbi != NULL`；`clu >= EXFAT_FIRST_CLUSTER`；`clu < sbi->num_clusters`；
  `sbi->blocksize > 0`；`sbi->sect_per_clus_bits` 合法。

**Post-Condition (Case 1: success)**:
- 计算 first_sector = exfat_clu_to_sector(sbi, clu)；nr_sect =
  1 << sbi->sect_per_clus_bits；逐扇区 los_part_write(part_id,
  zero_buf, first_sector + i, 1) 调用 nr_sect 次全成功 → 返回 0。
  调用结束时该簇磁盘字节全 0；堆 zero_buf 已 LOS_MemFree。

**Post-Condition (Case 2: -EINVAL)**:
- 参数校验失败 → -EINVAL；不分配 buf；不调 IO。

**Post-Condition (Case 3: -ENOMEM)**:
- LOS_MemAlloc(blocksize) 返回 NULL → -ENOMEM；不调 IO。

**Post-Condition (Case 4: -EIO mid-loop)**:
- 循环到第 k 个扇区 (k < nr_sect) los_part_write 返回 < 0 → 释放
  zero_buf；返回 -EIO；前 k-1 个扇区已写为 0，**不**回滚（caller
  alloc_new_dir 会清 bitmap 释放整簇）。

**Pre-Condition (exfat_alloc_new_dir)**:
- `sbi != NULL`；`clu_out != NULL`；caller 持 sbi->s_lock。

**Post-Condition (Case 1: success)**:
- 局部 chain.flags = ALLOC_FAT_CHAIN（v1 alloc_cluster 限制）；
  exfat_alloc_cluster(sbi, 1, &chain) 返回 0；
  exfat_zeroed_cluster(sbi, chain.dir) 返回 0；
- clu_out->dir = chain.dir；clu_out->size = 1；
  clu_out->flags = ALLOC_NO_FAT_CHAIN（重写为 NO_FAT，因为单簇目录）。
- sbi->used_clusters += 1；clu_srch_ptr 由 alloc_cluster 更新。

**Post-Condition (Case 2: -ENOSPC from alloc_cluster)**:
- alloc_cluster 返 -ENOSPC → 透传；clu_out->dir = EXFAT_EOF_CLUSTER；
  size = 0；不调 zeroed_cluster；sbi 不变。

**Post-Condition (Case 3: -EIO from zeroed_cluster — rollback)**:
- alloc_cluster 已成功（簇 c 已 set bitmap + FAT[c]=EOF），但
  zeroed_cluster 返 -EIO 或 -ENOMEM：
  - 调 exfat_clear_bitmap(sbi, c) 释放 bitmap 位（rollback）；
  - 调 exfat_ent_set(sbi, c, EXFAT_FREE_CLUSTER) 把 FAT 项还回（与
    Wave B Stage 2b 的 free_cluster 同分层）；
  - sbi->used_clusters 减 1；
  - clu_out->dir = EXFAT_EOF_CLUSTER；size = 0；
  - 透传 zeroed_cluster 的 errno。

**Pre-Condition (exfat_init_dir_entry)**:
- `sbi != NULL`；`p_dir != NULL`；`entry >= 0`；
  `type == TYPE_DIR || type == TYPE_FILE`；caller 持 sbi->s_lock。

**Post-Condition (Case 1: TYPE_DIR success)**:
- 在 entry 槽位写入：file dentry { type=0x85, attr=ATTR_SUBDIR,
  create/modify/access time = now snapshot, num_ext=0, checksum=0,
  其他字段全 0 }。
- 在 entry+1 槽位写入：stream dentry { type=0xC0, flags=ALLOC_NO_FAT_CHAIN,
  start_clu=start_clu, valid_size=size, size=size, name_len=0,
  name_hash=0 }。
- 两次 set_dentry 均成功 → 返回 0。

**Post-Condition (Case 2: TYPE_FILE success)**:
- 同 Case 1，但 file.attr = ATTR_ARCHIVE，stream.flags =
  ALLOC_FAT_CHAIN，start_clu = EXFAT_EOF_CLUSTER（caller 传入），
  size = 0（caller 传入）。本 stage VfsExfatMkdir 不走该分支，但
  helper 自身完整。

**Post-Condition (Case 3: -EINVAL)**:
- type 非 TYPE_DIR/TYPE_FILE，或 sbi/p_dir NULL，或 entry < 0 →
  -EINVAL；盘上不变。

**Post-Condition (Case 4: -EIO mid)**:
- 第 1 次 set_dentry（file）成功，第 2 次 set_dentry（stream）失败：
  透传 -EIO/-ENOMEM；entry 槽已写 file dentry，entry+1 槽**未**改
  → 不回滚（Q3）；caller 在 vol_dirty bracket 内消化。

**Pre-Condition (exfat_init_ext_entry)**:
- `sbi != NULL`；`p_dir != NULL`；`p_uniname != NULL`；
  `3 <= num_entries <= EXFAT_DENTRY_SET_MAX`；`entry >= 0`；
  caller 持 sbi->s_lock；前置：`exfat_init_dir_entry` 已成功。

**Post-Condition (Case 1: success)**:
- entry 槽 file dentry 的 num_ext 字段 = num_entries - 1（落盘）。
- entry+1 槽 stream dentry 的 name_len = p_uniname->name_len，
  name_hash = p_uniname->name_hash（落盘）。
- entry+2 .. entry+num_entries-1 槽：每槽是 EXFAT_NAME (0xC1)
  dentry，unicode_0_14 字段填充对应 15-code-unit 切片，未占用槽位
  填 0x0000（与 Linux exfat_init_name_entry 行 423-439 一致）。
- 所有 num_entries 个 dentry 重新读取计算 chksum16：
  fep = entry 处 dentry；seed = exfat_calc_chksum16(fep, 32, 0,
  CS_DIR_ENTRY)；i = 1..num_entries-1：seed = exfat_calc_chksum16(
  &set[i], 32, seed, CS_DEFAULT)。
- file dentry 的 checksum 字段 = seed → set_dentry 回写。
- 返回 0。

**Post-Condition (Case 2: -EINVAL)**:
- 任一指针 NULL 或 num_entries 越界 → -EINVAL。

**Post-Condition (Case 3: -EIO mid)**:
- 中途任一 set_dentry / get_dentry 失败 → 透传 errno；前置已写槽位
  保留；不回滚（Q3）。chksum 字段未回填——盘上 file dentry 的 chksum
  与实际 set 不匹配，下次 lookup 会因 validate_dentry_set 失败而拒
  载入。该状态由 vol_dirty bracket 标记为脏盘等 fsck 修复。

**Pre-Condition (exfat_add_entry)**:
- `sbi != NULL`；`parent_vp != NULL`；`parent_vp->data != NULL`；
  `name != NULL`；`info != NULL`；`type ∈ {TYPE_DIR, TYPE_FILE}`；
  caller 持 sbi->s_lock。

**Post-Condition (Case 1: TYPE_DIR success)**:
- exfat_utf8_to_uni 返回 0 且 uniname.name_len > 0；
- num_entries ∈ [3, 19]；
- exfat_alloc_dentry_slot 返回 0 且 dentry_idx >= 0；
- exfat_alloc_new_dir 返回 0 且 new_clu.dir 是新分配的簇号且簇内全 0;
- exfat_init_dir_entry 返回 0；exfat_init_ext_entry 返回 0；
- *info 完整装填（dir, entry, type=TYPE_DIR, attr=ATTR_SUBDIR,
  start_clu=new_clu.dir, flags=ALLOC_NO_FAT_CHAIN, size=cluster_size,
  num_subdirs=2）；返回 0。

**Post-Condition (Case 2: TYPE_FILE — v1 -ENOSYS)**:
- type == TYPE_FILE → 直接返回 -ENOSYS；不取 slot；不分配簇；
  *info 不填。

**Post-Condition (Case 3: -EINVAL)**:
- 任一指针 NULL；name[0] == '\0'；type 不在合法集 → -EINVAL。

**Post-Condition (Case 4: -ENOSPC from alloc_dentry_slot)**:
- exfat_alloc_dentry_slot 返 -ENOSPC → 透传；不调 alloc_new_dir；
  *info 不填；sbi 不变。

**Post-Condition (Case 5: -ENOSPC from alloc_new_dir)**:
- alloc_dentry_slot 已成功（slot K 找好），alloc_new_dir 返 -ENOSPC：
  - 透传；slot K 不释放（Q3：保留 free slot；下次 mkdir 可能复用——
    free slot 自身不污染盘面，不需要 fsck 修）；*info 不填。

**Post-Condition (Case 6: -EIO from init_dir_entry / init_ext_entry)**:
- alloc_dentry_slot + alloc_new_dir 均成功，但 init_dir_entry 或
  init_ext_entry 中途返错：
  - 透传 errno；slot K 不释放；new_clu 簇不释放（Q3=a；
    vol_dirty 由 caller bracket 标记）；*info 部分字段可能已填或
    全 0——caller 不应使用 *info；调用方应**先**判 errno != 0 再
    用 info。

**Pre-Condition (VfsExfatMkdir)**:
- VFS 已为 parent_vp 取得引用；parent_vp->data 已被 mount/lookup
  填充并 LOS_MuxInit。

**Post-Condition (Case 1: success)**:
- 取 sbi->s_lock；set_volume_dirty 返 0 或 -EIO（仅记日志）；
- exfat_add_entry 返 0；clear_volume_dirty 调用（无论 add 成功）；
- exfat_inode_alloc / VnodeAlloc 均成功；new_ei 字段按 info 装填；
  new_vp 加入 VFS hash；parent_ei->num_subdirs += 1；
- 解锁；*vpp = new_vp；返回 0。

**Post-Condition (Case 2: -EINVAL)**:
- 入参 NULL 或 name == "" → -EINVAL；不取锁。

**Post-Condition (Case 3: add_entry 透传错误)**:
- add_entry 返负 errno → clear_volume_dirty → unlock → 透传该 errno；
  *vpp 不写；parent_ei 不变；盘上孤儿由 fsck 修。

**Post-Condition (Case 4: -ENOMEM in inode/VnodeAlloc)**:
- add_entry 已成功（盘上 dentry-set 与新目录簇都已创建），但
  exfat_inode_alloc 或 VnodeAlloc 返 NULL → unlock → -ENOMEM；
  盘上 dentry-set + 簇泄漏（v1 已知；fsck 修）。

**Invariant** (id=exfat-mkdir-vol-dirty-bracketed):
  VfsExfatMkdir 在 exfat_add_entry 调用前后严格 set_volume_dirty /
  clear_volume_dirty 配对。即使 add_entry 失败也调 clear；
  vol_dirty bracket 是 mkdir 失败路径下盘上孤儿/部分提交状态的唯一
  fsck 信号（与 Q3 no-rollback 配套）。

**Invariant** (id=exfat-init-dir-entry-time-set-on-success):
  Case 1 / Case 2 成功路径上，file dentry 的 create_time /
  modify_time / access_time 三组字段（含 _tz 与 _cs 子字段）均通过
  exfat_set_entry_time 写入当前系统时间快照；timestamp == 0 不允许
  作为合法成功状态（Linux 行为对齐）。

**Invariant** (id=exfat-init-ext-entry-chksum-spans-all-N):
  init_ext_entry 末尾计算的 chksum16 必须**横跨所有 num_entries
  个 dentry**——seed 来自 exfat_calc_chksum16(fep, 32, 0, CS_DIR_ENTRY)，
  后续 i=1..num_entries-1 项以 CS_DEFAULT 续算。漏掉任一项都会让下
  次 lookup 时 validate_dentry_set 报 chksum 不一致，新目录无法被
  访问。该 invariant 锁定 chksum 范围。

**Invariant** (id=exfat-zeroed-cluster-blocksize-iteration):
  zeroed_cluster **必须**按 sbi->blocksize 字节循环写入 nr_sect 个
  扇区，**禁止**一次 los_part_write 整簇——v1 不依赖块层批量 IO，
  与 Wave B 既有 set_dentry 同范式。zero_buf 大小恒为 blocksize；
  循环次数 = 1 << sbi->sect_per_clus_bits。继承 Linux fs/exfat/fatent.c
  行 296-312 的 per-block 写法骨架（buffer_head 层批量在 LiteOS-A 退化
  为单扇区循环）。

**Invariant** (id=exfat-alloc-new-dir-rollback-on-zero-fail):
  alloc_new_dir 是 add_entry 中**唯一**自带 rollback 的步骤——
  alloc_cluster 已成功但 zeroed_cluster 失败时，必须调 clear_bitmap
  + ent_set(FREE) 释放该簇。这是 add_entry 总体 no-rollback 策略
  （Q3=a）的有意例外：alloc_new_dir 是 add_entry 失败前的最后一
  个簇分配，"alloc 后立刻 zero" 在概念上是同一原子动作；其他失败
  路径不再回滚。

**Invariant** (id=exfat-add-entry-no-cluster-rollback-on-init-fail):
  init_dir_entry 或 init_ext_entry 失败时，已分配的 dir cluster
  与已找到的 dentry slot **均不**回滚（Q3=a）；vol_dirty bracket
  标脏；下次 mount fsck 修复孤儿簇与残缺 dentry-set。该 invariant
  锁定 add_entry 的失败语义，与 Linux 上游 namei.c::exfat_add_entry
  的 `goto out` 顺序一致（Linux 也不回滚，靠 buffer cache 顺序写
  + journal 等价物兜底；本端用 vol_dirty 等价兜底）。

**Invariant** (id=exfat-mkdir-vfs-hash-insert-after-data-set):
  VfsHashInsert 必须在 new_vp->data = new_ei **之后**调用——否则
  并发 lookup 拿到 vp 后 vp->data 仍是 NULL 会 NULL deref。
  exfat_inode_init_dir_chain 也必须先于 hash insert 完成，确保 ei
  的 dir / start_clu 字段对其他线程可见时已完整。

**Invariant** (id=exfat-add-entry-uniname-hash-cs-default):
  name_hash 字段用 CS_DEFAULT seed=0 over UTF-16 字节流计算
  （Linux fs/exfat/dir.c 中的 exfat_calc_chksum16 入口同步）；本端
  不参与 upcase 表的 hash（lookup 阶段对 mismatch 字符再做 upcase
  比较）。该 invariant 锁定本 stage 与 Wave A lookup name_hash 的
  对偶——双方必须用同一个公式，否则 mkdir 后 lookup 找不到。

**Invariant** (id=exfat-mkdir-no-parent-dentry-write):
  本 stage **不**写 parent dir 自身的 dentry——parent_ei->num_subdirs
  仅 in-memory 累加；on-disk parent dir 的 file dentry 中
  num_subdirs 字段（exFAT spec 称为 ValidDataLength 内嵌字段）
  保持旧值。下次 sync helper 持久化（v1 已知限制，Wave B 既有
  truncate VOP 同款）。

**Invariant** (id=exfat-init-dir-entry-stream-flags-by-type):
  stream dentry 的 flags 字段必须严格按 type 二选一：
  TYPE_DIR → ALLOC_NO_FAT_CHAIN；TYPE_FILE → ALLOC_FAT_CHAIN。
  该规则来自 Linux exfat_init_dir_entry 行 484-486；TYPE_DIR 单簇
  目录的 chain 不需要 FAT walk（Linux 用 NO_FAT 表"按 size 直推"），
  TYPE_FILE 默认走 FAT 链以便后续 truncate-extend 增长。

**Invariant** (id=exfat-mkdir-s-lock-bracketed):
  VfsExfatMkdir 取 sbi->s_lock 后所有出口路径（成功 / 各种失败）
  必走同一 unlock 标签 LOS_MuxUnlock(&sbi->s_lock)。锁深 1，不重入
  （helpers 自身 lock-free；alloc_cluster 内部取 bitmap_lock 而非
  s_lock，无 self-recurse）。-EINVAL 早退路径不取锁。

**Invariant** (id=exfat-add-entry-no-self-cluster-double-free):
  alloc_new_dir 内部 rollback 路径（Case 3）**不**调用公开
  `exfat_free_cluster` API——避免与 alloc_cluster 已持的 bitmap_lock
  自死锁；rollback 直接调 exfat_clear_bitmap + exfat_ent_set
  内联完成（与 Stage 2c invariant `exfat-alloc-cluster-rollback-on-error`
  同精神）。

**Invariant** (id=exfat-mkdir-leak-free-success):
  Case 1 成功路径上分配的所有内存（uniname 局部栈对象、info 局部栈
  对象、new_ei 堆对象、new_vp 堆对象）均不泄漏：栈对象生命期跟随
  函数；new_ei / new_vp 由后续 VFS 释放路径（reclaim 钩子调
  exfat_inode_free）回收。失败路径下 new_ei 已分配但 VnodeAlloc
  失败时必须 exfat_inode_free。

## Refine Prompt

Locking discipline:

- VFS 入口：caller **不**持任何 sbi / ei 锁。VfsExfatMkdir 取
  `sbi->s_lock`（与 Linux `mutex_lock(&EXFAT_SB(sb)->s_lock)`
  行 847 对齐；这是父目录变更操作的事实主锁）。一次性 LOS_MuxLock
  至 unlock 标签，无中间释放。
- 锁深 1：本 stage **不**取 parent_ei->inode_lock——parent dir 的
  in-memory 状态（num_subdirs）变更被 s_lock 单一序列化所覆盖；
  Linux 在 mkdir 路径下也仅持 s_lock，未持 parent inode->i_rwsem
  以外的锁（i_rwsem 由 VFS 上层传入，本端 LiteOS-A 由 VFS lookup
  锁等价）。
- helpers 锁矩阵（callee 不取，caller 持有）：
  - calc_num_entries：纯函数。
  - zeroed_cluster：lock-free（los_part_write 自身串行；本端无 buffer
    cache 锁竞争）。caller 持 s_lock 即可。
  - alloc_new_dir：lock-free 自身；调用的 alloc_cluster 内部取
    bitmap_lock（自管）；rollback 路径直接清 bitmap + ent_set 不重入
    bitmap_lock（Stage 2c invariant）。
  - init_dir_entry / init_ext_entry / add_entry 自身：lock-free；
    依赖 set_dentry / get_dentry / alloc_dentry_slot 各自 lock-free
    （已批准 Stage 4a / 4b invariant）。
- 锁序：caller(VOP) = s_lock；alloc_cluster = bitmap_lock(internal)；
  set_dentry / set_dentry_set / get_dentry / alloc_dentry_slot =
  lock-free；chksum 计算 = pure。锁层级 s_lock < bitmap_lock 在
  helper 边界都已释放，反复 mkdir 不会自死锁。
- 禁止：本 stage **不**取 parent_ei->inode_lock（Linux 上游 mkdir
  路径也不取——s_lock 单独序列化所有父目录变更；新 dir 的 ei 在
  VfsHashInsert 之前还没有任何路径可达，无并发风险）。同样禁止
  在 add_entry 内调用任何会取 s_lock 的 API（避免 self-recurse；
  alloc_cluster / set_dentry / get_dentry 均不取 s_lock，已合规）。
- spinlock 禁区：本路径含 LOS_MemAlloc + 多次 los_part_read /
  los_part_write，不可在持自旋锁状态进入。LiteOS-A 主流锁均为
  LosMux（睡眠锁）；本约束自然满足。

## Refine Prompt — Phase 2 (failure semantics & v1 limitations)

本 stage 选定 Q3=a "失败不回滚"策略与 v1 既定的多个限制（持久化
parent dentry / TYPE_FILE 分支 / extend_dir）共同约束如下：

- **失败语义统一锚点**：VOP 顶层 set_volume_dirty / clear_volume_dirty
  bracket 是 v1 mkdir 失败的**唯一**外部信号；任何 caller 在
  -EIO/-ENOSPC/-ENOMEM 路径下不需要也不应调任何 rollback API
  （exfat_clear_bitmap / exfat_remove_dentry 等都不在本 stage 调用
  侧出现）。这与 Wave A 既有 truncate VOP "不持久化" 的 v1 留白
  策略协调。
- **alloc_new_dir 例外**：唯一**自带** rollback 的步骤；理由是
  alloc_cluster + zeroed_cluster 在概念上是 alloc_new_dir 的
  原子单元——把"alloc 但未 zero 的簇"留盘等价于 alloc 失败，
  无法靠 vol_dirty 区分；让 alloc_new_dir 内部消化错误。
- **dentry slot 不释放**：alloc_dentry_slot 找到 K 但后续失败时，
  K 处的 dentry slot 保持原状（free 或 deleted）——free slot 不
  污染盘上目录视图（type==0x00 或高位 0 的字节都被 readdir 跳过），
  下次 mkdir 复用即可；不需要 fsck 修。
- **partial dentry-set**：init_dir_entry 写了 file dentry 但 stream
  dentry 失败（或 init_ext_entry 写了 stream + 部分 name 但 chksum
  未回填）→ 盘上是 chksum 不匹配的残缺 dentry-set；下次 lookup
  validate_dentry_set 失败而拒读，但这些 dentry 不会被误判为合法
  目录项（Linux fsck.exfat 会识别 type=0x85 但 num_ext mismatch
  或 chksum mismatch 的项并清理）。vol_dirty 标记为该状态的兜底信号。
- **TYPE_FILE 分支**：v1 -ENOSYS；Stage 4d evolve 时把 add_entry
  里 if (type == TYPE_DIR) 分支补成 if/else，TYPE_FILE 路径不调
  alloc_new_dir、start_clu = EXFAT_EOF_CLUSTER、size = 0、
  flags = ALLOC_FAT_CHAIN 喂给 init_dir_entry。
- **extend_dir 缺失**：alloc_dentry_slot v1 不扩容，父目录满时
  -ENOSPC 透传到 sys_mkdir；用户态收到 ENOSPC 后只能删除其他文件
  腾位置——这是 Wave B 的边界。Stage 4b' 后续 evolve 时引入
  exfat_alloc_dentry_slot_grow wrapper，在本 stage 调用点替换即可，
  add_entry 主流程不动。
- **持久化 parent ei 字段**：parent_ei->num_subdirs / mtime / ctime
  仅 in-memory；后续 sync helper（Wave B 收尾阶段）持久化。在
  那之前 reboot 即丢——这是 Wave B 既定的 v1 限制，与 truncate
  VOP 同款。
