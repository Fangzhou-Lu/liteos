[PROMPT]
LiteOS-A 实现 exFAT 簇分配位图（allocation bitmap）的加载、释放与统计：

1. **`exfat_load_bitmap(sbi)`** —— 经 `exfat_find_root_dentry(sbi, EXFAT_BITMAP, ...)`
   定位盘上 BITMAP dentry；按 dentry 的 `start_clu` 与 `size` 计算所需扇区数；
   分配单段连续缓冲（大小 = `map_sectors * blocksize` 字节）；用一次
   `los_part_read` 调用把全部位图扇区读入；写入 `sbi->{map_clu, map_sectors,
   vol_amap}`。

2. **`exfat_free_bitmap(sbi)`** —— 幂等释放 `sbi->vol_amap`（NULL 安全）；
   释放后 `sbi->vol_amap = NULL`、`sbi->map_sectors = 0`，避免悬指针。

3. **`exfat_count_used_clusters(sbi, *out)`** —— 内存遍历 `sbi->vol_amap`，
   计数已置位 bit；写到 `*out`。无 IO，无 sbi 锁——调用方 mount 在 sbi 未对外
   可见时调用。

依赖：`dentry-v1` 的 `exfat_find_root_dentry`、`los_part_read`、`LOS_MemAlloc`。

## First Prompt

[RELY]
```c
/* —— balloc.header 已导入 common.header（exfat_sb_info）+ exfat_raw（exfat_dentry,
 *    EXFAT_BITMAP, EXFAT_DATA_CLUSTER_COUNT(sbi)）—— */

/* 上一阶段产出（dentry-v1 已 approve）—— */
int  exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                            struct exfat_dentry *out);

/* 块 IO */
INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

/* 内存与字符串 */
extern UINT8 *m_aucSysMem0;
VOID   *LOS_MemAlloc(VOID *pool, UINT32 size);
UINT32  LOS_MemFree(VOID *pool, VOID *ptr);
errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

/* 字节序 */
#define LE32_TO_HOST(x) (x)
#define LE64_TO_HOST(x) (x)

/* 错误码 */
#define EINVAL  22
#define EIO     5
#define ENOMEM  12
#define ENOENT  2
```

[GUARANTEE]
```c
/*
 * exfat_load_bitmap — 加载簇分配位图到 sbi->vol_amap。
 *
 * 调用约定：
 *   - 调用方已通过 exfat_parse_boot_sector 填好 sbi 几何字段；用 mount.spec
 *     Step 11 的语义。
 *   - 不持有任何锁；mount 路径在 sbi 未对外可见时调用。
 *   - 必读 BITMAP dentry 的 start_clu 与 size，本函数自行调用 find_root_dentry。
 *
 * 副作用与返回值：
 *   - 0：成功。sbi->map_clu = bitmap 起始簇；sbi->map_sectors = 所需扇区数；
 *     sbi->vol_amap = 已加载的位图字节缓冲（heap，需在 free_bitmap/umount 释放）。
 *   - -ENOENT：root 目录中无 EXFAT_BITMAP 类型 dentry。
 *   - -EIO：bitmap dentry 声明 size < 实际所需 (need_map_size > size)；
 *           或 los_part_read 读 bitmap 扇区失败。
 *   - -ENOMEM：分配 vol_amap 缓冲失败。
 *   - 失败路径下 sbi->vol_amap 保持 NULL，map_sectors 保持 0；
 *     不留下泄漏。
 */
int exfat_load_bitmap(exfat_sb_info *sbi);

/*
 * exfat_free_bitmap — 释放 sbi->vol_amap。幂等。
 *
 * 调用约定：
 *   - vol_amap 可能是 NULL（mount 失败回滚 / load_bitmap 未成功 / 已被释放）。
 *   - 不持有锁。
 *
 * 副作用：vol_amap → NULL；map_sectors → 0；map_clu 字段保留（记录位图位置，
 *        即使内存已释放，便于调试）。
 */
void exfat_free_bitmap(exfat_sb_info *sbi);

/*
 * exfat_count_used_clusters — 内存扫描 vol_amap，计已用簇数。
 *
 * 调用约定：
 *   - 必须先成功 exfat_load_bitmap；否则返回 -EINVAL。
 *   - 纯内存计算，无 IO，无锁，无分配。
 *   - 计数算法：按字节遍历 vol_amap，对每个字节查表得到 set bit 数累加。
 *     最后一个不完整字节用 mask 屏蔽位图末尾对应 reserved cluster 之外的位。
 *
 * 入参：
 *   sbi  非 NULL；sbi->vol_amap、sbi->num_clusters 已就绪。
 *   out  非 NULL；写入 used cluster 数（不含 cluster 0/1 reserved）。
 *
 * 返回：
 *   0       成功，*out 已写。
 *   -EINVAL sbi/out NULL，或 vol_amap == NULL。
 */
int exfat_count_used_clusters(const exfat_sb_info *sbi, uint32_t *out);
```

[SPECIFICATION of exfat_load_bitmap]

**Pre-Condition**:
- `sbi != NULL && sbi->vol_amap == NULL`（未加载）。
- 几何字段（cluster_size、blocksize、num_clusters、root_dir、part_id）已填。

**Post-Condition**:

**Case 1（成功，返回 0）**：
1. `bitmap_dentry = exfat_find_root_dentry(sbi, EXFAT_BITMAP, ...)`：成功；
2. `sbi->map_clu = LE32_TO_HOST(bitmap_dentry.dentry.bitmap.start_clu)`；
3. `map_size = LE64_TO_HOST(bitmap_dentry.dentry.bitmap.size)`（盘上声明的字节数）；
4. `need_map_size = (EXFAT_DATA_CLUSTER_COUNT(sbi) - 1) / 8 + 1`（所需位图字节）；
5. **若 `need_map_size > map_size`：返回 -EIO**（位图太小，无法表示所有簇）；
6. **若 `need_map_size < map_size`：PRINT_WARN，继续**（位图偏大，盘上常见，容忍）；
7. `sbi->map_sectors = (need_map_size + sbi->blocksize - 1) / sbi->blocksize`；
8. `sbi->vol_amap = LOS_MemAlloc(m_aucSysMem0, sbi->map_sectors * sbi->blocksize)`；
9. 计算 bitmap 起始扇区 `data_sector = sbi->clu_offset + (sbi->map_clu - EXFAT_FIRST_CLUSTER) * sect_per_clus`；
10. `los_part_read(sbi->part_id, sbi->vol_amap, data_sector, sbi->map_sectors, TRUE)` 成功；
11. 返回 0。

**Case 2（root 中无 BITMAP dentry，返回 `-ENOENT`）**：
- `exfat_find_root_dentry` 返回 -ENOENT。直接转传。

**Case 3（位图太小，返回 `-EIO`）**：
- need_map_size > map_size。已分配的资源（如有）已释放。

**Case 4（IO 失败，返回 `-EIO`）**：
- find_root_dentry 返回 -EIO；
- 或 los_part_read 失败。
- 已分配 vol_amap 已 LOS_MemFree。

**Case 5（内存不足，返回 `-ENOMEM`）**：
- vol_amap 分配失败。无中间状态。

[SPECIFICATION of exfat_free_bitmap]

**Pre-Condition**:
- `sbi != NULL`。
- `sbi->vol_amap` 可为 NULL 或先前由 load_bitmap 分配的指针。

**Post-Condition**:
- 若 `vol_amap != NULL`：调用 `LOS_MemFree(m_aucSysMem0, vol_amap)`；置 `vol_amap = NULL`。
- 若 `vol_amap == NULL`：no-op。
- 写 `sbi->map_sectors = 0`。
- map_clu 字段不动（保留盘上位置作为调试线索）。

[SPECIFICATION of exfat_count_used_clusters]

**Pre-Condition**:
- `sbi != NULL && out != NULL`。
- `sbi->vol_amap != NULL`（load_bitmap 已成功）。

**Post-Condition**:

**Case 1（成功，返回 0）**：
- `data_clusters = EXFAT_DATA_CLUSTER_COUNT(sbi)`；
- 完整字节数 `full = data_clusters / 8`；剩余位 `tail = data_clusters % 8`；
- 计数 = `Σ popcount(vol_amap[0..full-1])` + `popcount(vol_amap[full] & ((1u << tail) - 1u))`；
- 写入 `*out`，返回 0。

**Case 2（前置失败，返回 `-EINVAL`）**：
- sbi/out 为 NULL，或 sbi->vol_amap 为 NULL。

**Invariant** (id=exfat-balloc-load-leak-free):
load_bitmap 失败路径下不留下任何已分配 heap。具体：vol_amap 在分配后若 IO 失败
必须 LOS_MemFree 并置 NULL。

**Invariant** (id=exfat-balloc-free-idempotent):
free_bitmap 可重复调用、可对未加载 sbi 调用，结果一致：vol_amap → NULL。

**Invariant** (id=exfat-balloc-count-mem-only):
count_used_clusters 不调 IO，不获取锁，不修改 vol_amap 内容。纯计算。

**Invariant** (id=exfat-balloc-bitmap-size-policy):
size 策略与 Linux 等价：
- `need_map_size > map_size`：硬错（-EIO）；
- `need_map_size < map_size`：PRINT_WARN，继续；
- 等于：直接成功。
此策略允许格式化工具的 size padding 而拒绝结构上小到放不下的位图。

**Invariant** (id=exfat-balloc-no-write):
本层任何函数都不调用 los_part_write / los_disk_write。set_bitmap、clear_bitmap
留待 v2 写路径阶段实现。

**Invariant** (id=exfat-balloc-bitmap-tail-mask):
count_used_clusters 在最后一个字节用 `(1u << tail) - 1u` 掩码屏蔽超出
data_cluster_count 的位，避免计入 bitmap padding 的 set 位。
