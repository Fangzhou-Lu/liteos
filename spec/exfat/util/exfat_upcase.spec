[PROMPT]
LiteOS-A 实现 exFAT upcase 表的盘上加载与释放：

1. **`exfat_create_upcase_table(sbi)`** —— 经 `exfat_find_root_dentry(sbi,
   EXFAT_UPCASE, ...)` 定位 upcase dentry；按 dentry 的 `start_clu`、`size`
   读取 upcase 字节流；用 `exfat_calc_chksum32(buf, len, 0, CS_DEFAULT)`
   累计校验，与 dentry 中的 `checksum` 字段比对；通过则把字节流原位
   reinterpret 为 `uint16_t[N]`（每项 LE→host），并保留指针到
   `sbi->vol_utbl`；同时填 `sbi->vol_utbl_clu` 与 `sbi->vol_utbl_size`。

2. **`exfat_free_upcase_table(sbi)`** —— 幂等释放 vol_utbl，置 NULL。

v1 对盘上 upcase 失败的策略采取**严格语义**：checksum 不匹配 → 返回 -EINVAL；
disk IO 失败 → 返回 -EIO。**不**回退到内置默认表（与 Linux nls.c 不同，
Linux 在 chksum mismatch 时会 fallback；本 v1 留待后续以 `## Refine Prompt`
追加 fallback 行为，避免引入 128KB 静态表的体积代价）。

依赖：`dentry-v1::exfat_find_root_dentry` + `chksum-v1::exfat_calc_chksum32`
+ `los_part_read` + `LOS_MemAlloc`。

## First Prompt

[RELY]
```c
/* —— util.header 已导入 common.header（exfat_sb_info）+ exfat_raw（exfat_dentry,
 *    EXFAT_UPCASE）—— */

/* dentry-v1 与 chksum-v1 已 approved */
int      exfat_find_root_dentry(const exfat_sb_info *sbi, uint8_t type,
                                struct exfat_dentry *out);
uint32_t exfat_calc_chksum32(const void *data, uint32_t len,
                             uint32_t chksum, int type);
#define CS_DEFAULT 2

/* 块 IO */
INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);

/* 内存与字符串 */
extern UINT8 *m_aucSysMem0;
VOID   *LOS_MemAlloc(VOID *pool, UINT32 size);
UINT32  LOS_MemFree(VOID *pool, VOID *ptr);

/* 字节序 */
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
 * exfat_create_upcase_table — 从盘上加载 upcase 表到 sbi->vol_utbl。
 *
 * 调用约定：
 *   - 调用方（mount.spec Step 10）已经过 parse_boot_sector + verify_boot_region；
 *     sbi 几何字段已就绪；vol_utbl 必须为 NULL（未加载）。
 *   - 不持有任何 sbi 锁。
 *
 * 副作用与返回值：
 *   - 0：成功。sbi->vol_utbl 指向已分配的 N 个 uint16_t（每项已 LE→host）；
 *     sbi->vol_utbl_clu = upcase 起始簇；sbi->vol_utbl_size = dentry 声明字节数。
 *   - -ENOENT：root 中无 EXFAT_UPCASE 类型 dentry。
 *   - -EINVAL：upcase size 为 0、size 不是 2 字节倍数、size > 128 KB（65536 entries）、
 *              或 chksum32 校验失败（mismatch）。
 *   - -EIO：los_part_read 失败。
 *   - -ENOMEM：缓冲分配失败。
 *   - 失败路径下 vol_utbl 保持 NULL。
 */
int exfat_create_upcase_table(exfat_sb_info *sbi);

/*
 * exfat_free_upcase_table — 释放 sbi->vol_utbl。幂等。
 */
void exfat_free_upcase_table(exfat_sb_info *sbi);
```

[SPECIFICATION of exfat_create_upcase_table]

**Pre-Condition**:
- `sbi != NULL && sbi->vol_utbl == NULL`。
- 几何字段（cluster_size、blocksize、num_clusters、root_dir、part_id、
  sect_per_clus_bits、clu_offset）已填。

**Post-Condition**:

**Case 1（成功，返回 0）**：
1. `dentry = exfat_find_root_dentry(sbi, EXFAT_UPCASE, ...)`：成功；
2. `tbl_clu = LE32_TO_HOST(dentry.dentry.upcase.start_clu)`，必须 ≥ EXFAT_FIRST_CLUSTER 且 < num_clusters；
3. `tbl_size = LE64_TO_HOST(dentry.dentry.upcase.size)`，必须 > 0、为 2 的倍数、≤ 65536 * 2 (= 131072 bytes)；
4. `expected_chksum = LE32_TO_HOST(dentry.dentry.upcase.checksum)`；
5. `num_sectors = (tbl_size + sbi->blocksize - 1) / sbi->blocksize`；
6. `buf = LOS_MemAlloc(m_aucSysMem0, num_sectors * sbi->blocksize)`：非 NULL；
7. `data_sector = sbi->clu_offset + (tbl_clu - EXFAT_FIRST_CLUSTER) * sect_per_clus`，
   其中 `sect_per_clus = 1u << sbi->sect_per_clus_bits`；
8. `los_part_read(part_id, buf, data_sector, num_sectors, TRUE)` 成功；
9. `actual_chksum = exfat_calc_chksum32(buf, tbl_size, 0, CS_DEFAULT)`：
   仅取前 tbl_size 字节（非 buf 全长，可能超出对齐）；
10. **`actual_chksum == expected_chksum`**，否则失败 -EINVAL；
11. 把 buf 前 tbl_size 字节按 uint16_t LE→host 解释——本 v1 在 LE host 上是
    identity，等价于直接 reinterpret cast；vol_utbl = (uint16_t *)buf；
12. `sbi->vol_utbl_clu = tbl_clu`；`sbi->vol_utbl_size = tbl_size`；
13. 返回 0；buf 所有权转给 sbi。

**Case 2（root 中无 UPCASE dentry，返回 `-ENOENT`）**：
- find_root_dentry 返回 -ENOENT。

**Case 3（dentry 字段非法，返回 `-EINVAL`）**：
- tbl_clu 越界（< 2 或 ≥ num_clusters）；
- tbl_size == 0；
- tbl_size % 2 != 0（必须 2 字节倍数，因每 entry 是 uint16_t）；
- tbl_size > 131072（v1 上界：完整 BMP 65536 entries）；
- chksum mismatch。
- 已分配 buf 必须 LOS_MemFree。

**Case 4（IO 失败，返回 `-EIO`）**：
- find_root_dentry 返回 -EIO；
- 或 los_part_read 失败。
- 已分配 buf 必须 LOS_MemFree。

**Case 5（内存不足，返回 `-ENOMEM`）**：
- LOS_MemAlloc 失败。

[SPECIFICATION of exfat_free_upcase_table]

**Pre-Condition**: `sbi != NULL`，`vol_utbl` 可为 NULL 或先前加载的指针。

**Post-Condition**:
- `vol_utbl != NULL`：LOS_MemFree → vol_utbl = NULL；vol_utbl_size = 0；vol_utbl_clu 保留。
- `vol_utbl == NULL`：no-op。

**Invariant** (id=exfat-upcase-no-write):
不调用 los_part_write / los_disk_write。upcase 在 v1 mount 仅读盘。

**Invariant** (id=exfat-upcase-checksum-verified):
任何写入 sbi->vol_utbl 之前必须 chksum 校验通过。chksum mismatch 路径绝不
保留盘数据到 sbi。本 invariant 由 Step 10 强制：mismatch → goto err 释放 buf。

**Invariant** (id=exfat-upcase-byte-order):
upcase 表每 entry 是 LE16；本 v1 在 LE host 上 reinterpret cast 不动数据；
若未来端口到 BE host，本函数必须改为逐 entry `LE16_TO_HOST`。**当前实现
依赖 LE host 假设**——通过 `__LITEOS_A__` 宏保证（ARM-LE）。

**Invariant** (id=exfat-upcase-free-idempotent):
free_upcase_table 可重复调用、可对未加载 sbi 调用，结果一致：vol_utbl → NULL。

**Invariant** (id=exfat-upcase-leak-free):
失败路径下不留下任何已分配 heap。具体：buf 分配后若任意后续步骤失败必须
LOS_MemFree。

**Invariant** (id=exfat-upcase-strict-no-fallback):
v1 不退回内置默认 upcase 表。chksum mismatch / dentry 缺失 / IO 失败都直接
返回错误，导致 mount 失败。这与 Linux nls.c 在 chksum mismatch 时 fallback
不同；v1 选择此严格策略以避免引入 128KB 静态默认表的体积代价。
