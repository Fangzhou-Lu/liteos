[PROMPT]
LiteOS-A 实现两个 exFAT mount 期 dentry 层 helper：

1. **`exfat_parse_boot_sector`** —— 解析已读入的 boot sector 字节缓冲到
   `exfat_sb_info` 几何字段：扇区大小、簇大小、FAT 位置/长度、数据区位置、
   簇数、root 起始簇、卷标志等；并对调用者传入的 `logical_sector_size`
   做一致性校验（必须等于 `1u << bs->sect_size_bits`）。

2. **`exfat_find_root_dentry`** —— 从 root_dir 起始簇出发，按 FAT 链遍历
   root 目录的所有簇，扫描每个簇内的 32B dentry 项，返回**第一个**匹配
   `type` 字节的 dentry 拷贝。FAT 链走法与 Linux exFAT 实现等价：每跨越
   一个簇都要读 FAT 表的 clu 项确定下一簇号；遇到 EOF/BAD/FREE 簇即结束。

两函数都由 `VfsExfatMount` 在 sbi 已 zalloc 但未对外可见时调用，没有
并发上下文。两函数都不持有任何锁、不写盘。

## First Prompt

[RELY]
```c
/* —— dentry.header 已导入 common.header（exfat_sb_info、exfat_chain）+
 *    exfat_raw（exfat_boot_sector、exfat_dentry、ALLOC_FAT_CHAIN、
 *    EXFAT_EOF_CLUSTER、EXFAT_BAD_CLUSTER、EXFAT_FREE_CLUSTER、
 *    DENTRY_SIZE、DENTRY_SIZE_BITS、EXFAT_UNUSED、BOOTSEC_FS_NAME_LEN、
 *    BOOTSEC_OLDBPB_LEN、EXFAT_MIN/MAX_SECT_SIZE_BITS）—— */

/* 块 IO（disk.h）—— 仅用于 part_read，不调 disk_write */
INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

/* libsec 安全函数 */
int     memcmp(const void *s1, const void *s2, size_t n);
errno_t memcpy_s(void *dest, size_t destMax, const void *src, size_t count);
errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

/* 内存（仅 find_root_dentry 在簇缓冲分配时使用 LOS_MemAlloc） */
extern UINT8 *m_aucSysMem0;
VOID  *LOS_MemAlloc(VOID *pool, UINT32 size);
UINT32 LOS_MemFree(VOID *pool, VOID *ptr);

/* 字节序（LiteOS-A ARM 是 LE，宏退化为 identity；保留 wrapper 以适配 BE 端口）*/
#define LE16_TO_HOST(x) (x)
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
 * exfat_parse_boot_sector — 解析 boot sector 字节缓冲，填充 sbi 几何字段。
 *
 * 调用约定：
 *   - 调用方已用 los_part_read 把扇区 0 读入字节缓冲并以 *bs 指向之；
 *     缓冲长度至少 sizeof(struct exfat_boot_sector) (512)。
 *   - 调用方需保证 sbi 已 zalloc 出来（其它字段尚未填）。
 *   - 不持有任何锁；不调用 IO。纯解析 + 写 sbi 字段。
 *
 * 入参：
 *   sbi                   非 NULL，其字段在本调用前未初始化（zero-init）。
 *   bs                    非 NULL，指向 boot sector 字节缓冲。
 *   logical_sector_size   调用方计算的逻辑扇区字节数（典型为
 *                         1u << bs->sect_size_bits）；本函数对其做一致性校验。
 *
 * 返回：
 *   0       成功，sbi 几何字段已填好（见 [SPECIFICATION] Case 1 列表）。
 *   -EINVAL boot sector 字段非法（signature、fs_name、must_be_zero、
 *           sect_size_bits 越界、num_fats 非 {1,2}、sect_per_clus_bits 越界、
 *           FAT/数据区一致性失败、或 logical_sector_size 与 bs 不一致）。
 */
int exfat_parse_boot_sector(exfat_sb_info *sbi,
                            const struct exfat_boot_sector *bs,
                            uint32_t logical_sector_size);

/*
 * exfat_find_root_dentry — 走 root 目录 FAT 链，返回首个匹配 type 的 dentry。
 *
 * 调用约定：
 *   - 调用方需先经 exfat_parse_boot_sector 填好 sbi 的所有 FAT/簇相关字段。
 *   - 内部使用 LOS_MemAlloc 分配 cluster_size 字节缓冲，遍历完毕后释放。
 *     失败路径（任意 los_part_read 失败、ENOMEM）必须释放该缓冲。
 *   - 不持有任何锁，不修改 sbi。
 *
 * 入参：
 *   sbi    非 NULL；几何字段已填；root_dir、cluster_size、blocksize 必须正确。
 *   type   要匹配的 dentry.type 字节（如 EXFAT_BITMAP、EXFAT_UPCASE）。
 *   out    非 NULL；本函数把首个匹配 dentry 的 32 字节内容用 memcpy_s 拷入。
 *
 * 返回：
 *   0       找到，*out 已填。
 *   -ENOENT 整个 FAT 链走完都没找到匹配 type，或遇到 EXFAT_UNUSED 终止符。
 *   -EIO    任一 los_part_read 失败（boot 区已校验过，正常应不发生），
 *           或 FAT 链中遇到 EXFAT_BAD_CLUSTER / EXFAT_FREE_CLUSTER。
 *   -ENOMEM 簇缓冲分配失败。
 */
int exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                           struct exfat_dentry *out);
```

[SPECIFICATION of exfat_parse_boot_sector]

**Pre-Condition**:
- `sbi != NULL && bs != NULL`。
- `logical_sector_size > 0`。

**Post-Condition**:

**Case 1（成功，返回 0）**：

字段填充，**全部经字节序转换**（LE → host）：

| 字段                       | 公式 / 来源                                          |
|----------------------------|------------------------------------------------------|
| `sect_size_bits`           | `bs->sect_size_bits`                                  |
| `sect_per_clus_bits`       | `bs->sect_per_clus_bits`                              |
| `num_fats`                 | `bs->num_fats`                                        |
| `partition_offset`         | `LE64_TO_HOST(bs->partition_offset)`                  |
| `vol_length`               | `LE64_TO_HOST(bs->vol_length)`                        |
| `fat_offset`               | `LE32_TO_HOST(bs->fat_offset)`                        |
| `fat_length`               | `LE32_TO_HOST(bs->fat_length)`                        |
| `fat2_offset`              | `fat_offset` (num_fats==1) 或 `fat_offset + fat_length` (num_fats==2) |
| `clu_offset`               | `LE32_TO_HOST(bs->clu_offset)`                        |
| `num_clusters`             | `LE32_TO_HOST(bs->clu_count) + EXFAT_RESERVED_CLUSTERS` |
| `root_dir`                 | `LE32_TO_HOST(bs->root_cluster)`                      |
| `cluster_size_bits`        | `sect_size_bits + sect_per_clus_bits`                 |
| `cluster_size`             | `1u << cluster_size_bits`                             |
| `blocksize`                | `1u << sect_size_bits`                                |
| `blocksize_bits`           | `sect_size_bits`                                      |
| `dentries_per_clu`         | `cluster_size >> DENTRY_SIZE_BITS`                    |
| `vol_flags`                | `LE16_TO_HOST(bs->vol_flags)`                         |
| `vol_flags_persistent`     | `vol_flags & (VOLUME_DIRTY \| MEDIA_FAILURE)`         |
| `s_maxbytes`               | `(uint64_t)(num_clusters - EXFAT_RESERVED_CLUSTERS) << cluster_size_bits` |
| `clu_srch_ptr`             | `EXFAT_FIRST_CLUSTER` (= 2)                           |
| `used_clusters`            | `EXFAT_CLUSTERS_UNTRACKED` (mount 后 step 12 才填)    |

**Case 2（boot 字段非法，返回 `-EINVAL`）**：
- `LE16_TO_HOST(bs->signature) != BOOT_SIGNATURE` (0xAA55)；
- `memcmp(bs->fs_name, "EXFAT   ", 8) != 0`；
- `bs->must_be_zero[i]` 任一字节非零（i ∈ [0, BOOTSEC_OLDBPB_LEN)）；
- `bs->num_fats ∉ {1, 2}`；
- `bs->sect_size_bits < EXFAT_MIN_SECT_SIZE_BITS` 或 `> EXFAT_MAX_SECT_SIZE_BITS`；
- `bs->sect_per_clus_bits > 25 - bs->sect_size_bits`；
- `((uint64_t)fat_length << sect_size_bits) < (uint64_t)num_clusters * 4u`（FAT 太短放不下簇表）；
- `clu_offset < fat_offset + fat_length * num_fats`（数据区与 FAT 重叠）；
- **`logical_sector_size != (1u << bs->sect_size_bits)`**（参数与 boot 不一致——用户答 1）。

[SPECIFICATION of exfat_find_root_dentry]

**Pre-Condition**:
- `sbi != NULL && out != NULL`。
- sbi 几何字段已填齐（cluster_size、blocksize、root_dir、fat_offset、clu_offset、num_clusters）。
- `sbi->root_dir` 是有效起始簇，且 `>= EXFAT_FIRST_CLUSTER`。
- 不持有任何 sbi 锁。

**Post-Condition**:

**Case 1（找到，返回 0）**：
- 走 root 的 FAT 链：从 `cur_clu = sbi->root_dir` 开始，每个簇读取 cluster_size
  字节到内部缓冲，按 32B 步进扫 dentries：
  - 若 `dentries[i].type == EXFAT_UNUSED` (0x00)：终止整个搜索，返回 -ENOENT；
  - 若 `dentries[i].type == type`：用 `memcpy_s(out, sizeof(*out), &dentries[i], sizeof(*out))` 拷贝并返回 0；
  - 否则 i++ 继续。
- 当前簇遍历完仍未找到 → 读 `FAT[cur_clu]` 得 `next_clu`：
  - 若 `next_clu == EXFAT_EOF_CLUSTER`：FAT 链走完未找到 → 返回 -ENOENT；
  - 若 `next_clu == EXFAT_BAD_CLUSTER` 或 `EXFAT_FREE_CLUSTER`：FAT 损坏 → 返回 -EIO；
  - 否则 `cur_clu = next_clu`，循环。

  FAT 表读取公式：
  - `fat_byte_off = (uint64_t)cur_clu * 4u`；
  - `fat_sector  = sbi->fat_offset + (fat_byte_off / sbi->blocksize)`；
  - `in_sector_off = fat_byte_off % sbi->blocksize`；
  - 读取 1 个扇区到临时缓冲；
  - `next_clu = LE32_TO_HOST(*(uint32_t *)(fat_buf + in_sector_off))`。

  簇扇区读取公式：
  - `data_sector = sbi->clu_offset + (uint64_t)(cur_clu - EXFAT_FIRST_CLUSTER) * sect_per_clus`；
  - 其中 `sect_per_clus = 1u << sbi->sect_per_clus_bits`；
  - 读取 `sect_per_clus` 个扇区到簇缓冲。

**Case 2（未找到，返回 `-ENOENT`）**：
- 走完整条 FAT 链未匹配 type；
- 或在某簇内遇到 EXFAT_UNUSED 终止符（exFAT 规定：UNUSED 之后的 dentry 都被视为不存在）。

**Case 3（IO 失败，返回 `-EIO`）**：
- 任一 los_part_read 调用返回负值；
- FAT 链中读到 EXFAT_BAD_CLUSTER 或 EXFAT_FREE_CLUSTER（卷损坏）。

**Case 4（内存不足，返回 `-ENOMEM`）**：
- 簇缓冲（cluster_size 字节）或 FAT 扇区缓冲（blocksize 字节）的 LOS_MemAlloc 失败。

**Invariant** (id=exfat-dentry-parse-no-io):
parse_boot_sector 不调用任何 IO 接口。仅读 *bs，写 sbi。

**Invariant** (id=exfat-dentry-parse-byte-order):
所有从 `bs` 读取的多字节字段都经 `LE*_TO_HOST` 转换。直接 `bs->vol_length`
等用法被禁止。这是 BE host 兼容性的硬约束。

**Invariant** (id=exfat-dentry-find-buf-leak-free):
find_root_dentry 在所有返回路径（成功 + 4 个失败 case）下，由本函数 LOS_MemAlloc
分配的临时缓冲（cluster buf + fat sector buf）都已 LOS_MemFree。
**严格反 LIFO**：先释放 fat_buf，再释放 clu_buf。

**Invariant** (id=exfat-dentry-find-readonly):
find_root_dentry 不调用 los_part_write/los_disk_write，不修改 sbi 任何字段。
只读语义。

**Invariant** (id=exfat-dentry-find-no-locks):
find_root_dentry 不获取/释放/检查任何锁。调用方（mount 路径）保证本函数
被调用时 sbi 尚未对外可见，没有锁竞争问题。

**Invariant** (id=exfat-dentry-fat-traversal-bounded):
FAT 链遍历有上界保护：`max_iter = sbi->num_clusters`；超过即返回 -EIO。
防止恶意/损坏卷构造 FAT 环导致无限循环。
