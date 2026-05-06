[PROMPT]
Provide complete `exfat_inode.c` fragment that implements `exfat_zeroed_cluster`.
Only include `"exfat.h"`; output a single C code block.
`exfat_zeroed_cluster` is called by `exfat_alloc_new_dir` right after a new cluster
is FAT-linked: it writes `sbi->blocksize` zero-bytes to every sector of the cluster
so a freshly allocated directory never exposes stale data. Iteration is per-sector
via `los_part_write` (one call per sector, no bulk write). Validate
`EXFAT_FIRST_CLUSTER ≤ clu < sbi->num_clusters` before any I/O.

[RELY]
```c
#define EXFAT_FIRST_CLUSTER          2u
#define EXFAT_MAX_SECT_PER_CLUS_BITS 25u

// Returns first absolute partition sector for clu. Requires clu >= EXFAT_FIRST_CLUSTER.
static inline uint64_t exfat_clu_to_sector(const exfat_sb_info *sbi, uint32_t clu);

// Write count sectors at sector; count=1 per call here. Returns 0 or negative errno.
int los_part_write(INT32 part_id, const void *buf, uint64_t sector, uint32_t count);

// Kernel heap alloc/free against the primary pool.
void *LOS_MemAlloc(void *pool, uint32_t sz);
void  LOS_MemFree(void *pool, void *ptr);
extern UINT8 m_aucSysMem0[];

// Secure zero (libsec). Returns EOK on success.
errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

void PRINT_ERR(const char *fmt, ...);
```

[GUARANTEE]
```c
/* Caller holds: nothing (lock-free; caller wraps alloc+zero under s_lock if needed).
   Returns 0 on success or:
     -EINVAL  bad sbi / blocksize==0 / clu out of range / sect_per_clus_bits too large
     -ENOMEM  zero-buffer allocation failed
     -EIO     memset_s error or any los_part_write error */
int exfat_zeroed_cluster(exfat_sb_info *sbi, uint32_t clu);
```

[SPECIFICATION]
**Pre-Condition**:
`sbi` non-NULL, `sbi->blocksize > 0`, `sbi->sect_per_clus_bits ≤ 25`,
`sbi->part_id` bound to a writable partition.
`EXFAT_FIRST_CLUSTER ≤ clu < sbi->num_clusters`. No lock held; sleeping permitted.

**Post-Condition**:
**Case 1 (success)**:
  - Every sector in `[first_sect, first_sect + nr_sect)` written with
    `sbi->blocksize` zero-bytes, one `los_part_write(…, 1u)` call per sector.
  - Zero buffer freed. Returns 0.

**Case 2 (validation failure)**:
  - `sbi` NULL, `blocksize` zero, `clu` out of range, or `sect_per_clus_bits > 25`.
  - No allocation, no I/O. Returns -EINVAL.

**Case 3 (allocation failure)**:
  - `LOS_MemAlloc` returns NULL. No I/O. Returns -ENOMEM.

**Case 4 (I/O failure)**:
  - `memset_s` non-EOK or `los_part_write` negative; failing sector logged via
    `PRINT_ERR`. Zero buffer freed. Prior sectors remain zeroed (no rollback).
    Returns -EIO.

**Invariant** (id=exfat-zeroed-cluster-blocksize-iteration):
  `zeroed_cluster` MUST iterate
`nr_sect` sectors writing `sbi->blocksize` bytes each — NO single bulk-cluster
write. v1 does not depend on block-layer batched IO; mirrors Wave B set_dentry
pattern. `zero_buf` size is always `blocksize`; iteration count =
`1 << sbi->sect_per_clus_bits`. Inherits the per-block write skeleton from
Linux `fs/exfat/fatent.c:296-312` (the buffer_head batch in Linux degrades to
single-sector loop in LiteOS).

**Invariant** (id=exfat-zeroed-cluster-free-on-error):
  the zero buffer MUST be freed on every
error path before returning. No caller-visible allocation survives a non-zero return.

**Invariant** (id=exfat-zeroed-cluster-no-partial-rollback):
  on mid-loop write failure, already-
written sectors stay zeroed. The caller abandons the cluster via `exfat_free_cluster`.
