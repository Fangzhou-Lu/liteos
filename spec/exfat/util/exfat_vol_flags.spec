[PROMPT]
exfat 卷脏标志（vol_flags）的置位 / 清位。Wave B 写路径（create / unlink / rename /
write / truncate）在修改盘面前调用 `exfat_set_volume_dirty(sbi)`，提交盘面修改完成
后调用 `exfat_clear_volume_dirty(sbi)`。两者最终都经由内部静态助手
`exfat_set_vol_flags(sbi, new_flags)`：先把 `sbi->vol_flags_persistent` 中保留位
（VOLUME_DIRTY | MEDIA_FAILURE）合并进 new_flags，与当前 `sbi->vol_flags` 比较,
不同则：① 写入 in-memory `sbi->vol_flags`；② 在 `sbi->boot_buf` 中以 cpu_to_le16
方式 patch 主 boot sector（结构体 `exfat_boot_sector::vol_flags` 字段）；③ 调用
`los_part_write(sbi->part_id, sbi->boot_buf, 0, 1)` 同步写回分区扇区 0 单扇区。

LiteOS-A `los_part_write` 同步阻塞，因此不区分 Linux 中「DIRTY 上升才 sync_dirty_buffer」
的写回延迟优化，始终在 final_flags 与 vol_flags 不同时立即写回。

整个序列由 `sbi->s_lock`（LosMux）守护，保护 vol_flags / boot_buf / 部分 IO 串行；
helper 不持锁，由调用方进入路径前持有；接口本身仅描述功能契约，加锁规则置于
## Refine Prompt。

[RELY]
```c
/* 频繁使用的 LiteOS-A 原语 */
extern int     LOS_MuxLock(LosMux *mutex, uint32_t timeout);
extern int     LOS_MuxUnlock(LosMux *mutex);
extern int     los_part_write(int32_t part_id, void *buf, uint64_t sector,
                              uint32_t count);
extern int     memcpy_s(void *dst, size_t dst_max, const void *src, size_t cnt);
extern void    PRINT_ERR(const char *fmt, ...);

/* exfat 标志位常量（exfat_raw.h，已在 common.header frozen 范围）*/
#define VOLUME_DIRTY    0x0002u
#define MEDIA_FAILURE   0x0004u

/* 主 boot sector 在分区内的位置：扇区 0，1 扇区长度 == sbi->blocksize */
#define EXFAT_MAIN_BOOT_SECTOR  0u
#define EXFAT_BOOT_WRITE_COUNT  1u

/* exfat_raw.h packed 结构：vol_flags 字段为 __le16，位于 boot sector 内固定偏移 */
struct exfat_boot_sector;
```

[GUARANTEE]
```c
/* 调用约定（两个公开 API 共用）:
 *   - 调用方持有 sbi->s_lock（LosMux, 可睡眠路径）；helper 不再加锁。
 *   - 返回 0 = 成功；负 POSIX errno = 失败:
 *       -EINVAL : sbi == NULL 或 sbi->boot_buf == NULL 或 sbi->blocksize == 0;
 *       -EIO    : los_part_write 失败（in-memory vol_flags 已更新，盘面状态未定）;
 *   - 成功且发生过渡时（Case 1）：sbi->vol_flags 已写到 final_flags；
 *     boot_buf 中 vol_flags 字段已 le16-patch；分区扇区 0 已同步写回 1 扇区；
 *   - 成功但 no-op 时（Case 2，final_flags 与现 vol_flags 相同）：
 *     不发起 IO；sbi 与 boot_buf 不变；
 *   - 不分配/释放堆内存；不修改 sbi 其他字段；不获取释放锁。
 */
int exfat_set_volume_dirty(exfat_sb_info *sbi);
int exfat_clear_volume_dirty(exfat_sb_info *sbi);
```

[SPECIFICATION]
**Pre-Condition**:
- `sbi != NULL`
- `sbi->boot_buf != NULL` 且其后端缓冲区大小 ≥ `sbi->blocksize`
- `sbi->blocksize > 0` 且为 2 的幂（512 / 1024 / 2048 / 4096）
- 调用线程持有 `sbi->s_lock`（已 LOS_MuxLock 成功）
- `sbi->part_id` 是当前 mount 已注册的合法 part_id（los_part_write 可索引）

**Post-Condition (Case 1: success, transition occurs)**:
- 令 `req_flags` = set 路径下 `sbi->vol_flags | VOLUME_DIRTY`，
  clear 路径下 `sbi->vol_flags & ~VOLUME_DIRTY`
- 令 `final_flags = req_flags | sbi->vol_flags_persistent`
- 当 `final_flags != sbi->vol_flags`：
  - `sbi->vol_flags == final_flags`（赋值生效）
  - `((struct exfat_boot_sector *)sbi->boot_buf)->vol_flags == cpu_to_le16(final_flags)`
  - 分区扇区 0 已被同步写入 `sbi->boot_buf` 前 1 扇区内容（los_part_write 返回 0）
  - 函数返回 0

**Post-Condition (Case 2: success, no-op)**:
- 当 `final_flags == sbi->vol_flags`：sbi 与 boot_buf 完全不变；不发起 IO；返回 0

**Post-Condition (Case 3: invalid args)**:
- `sbi == NULL` 或 `sbi->boot_buf == NULL` 或 `sbi->blocksize == 0`：
  返回 `-EINVAL`，所有可观察状态不变（不打印错误日志，避免空指针 PRINT 事故）

**Post-Condition (Case 4: write-back fails)**:
- los_part_write 返回非 0：
  - `sbi->vol_flags == final_flags`（已被赋值）
  - boot_buf 中的 vol_flags 字段已 le16-patch
  - 分区扇区 0 状态未定（依硬件失败模式：未变 / 部分写 / 完全写但返回错）
  - PRINT_ERR 打印 `[exfat_set_vol_flags] part_write failed: %d`
  - 函数返回 `-EIO`
- 不回滚 `sbi->vol_flags`（与 Linux mark_buffer_dirty 失败语义对齐）；调用方应将
  -EIO 视为致命错误，向 VFS 上抛后由上层决定切只读 / panic

**Invariant** (id=exfat-vol-flags-no-realloc):
  本路径不调用 LOS_MemAlloc / zalloc / LOS_MemFree；不分配 / 释放任何堆内存。

**Invariant** (id=exfat-vol-flags-persistent-merged):
  无论 Case 1 还是 Case 2，最终生效 `sbi->vol_flags` 必包含
  `sbi->vol_flags_persistent` 中所有置位——VOLUME_DIRTY 或 MEDIA_FAILURE 不会被
  caller 一次「clear_volume_dirty」行为意外丢失。

**Invariant** (id=exfat-vol-flags-le16-on-disk):
  写回盘上的 vol_flags 字段是 cpu_to_le16(final_flags) 的字节序列；ARM
  little-endian 配置下与 in-memory 值字节等价；保持与上游 Linux exfat 字节序兼容。

**Invariant** (id=exfat-vol-flags-no-lock-acquire):
  helper 不调用 LOS_MuxLock / LOS_MuxUnlock；锁约束完全由调用方维护，避免与
  Wave B 写路径上的 s_lock 嵌套获取冲突。

**Invariant** (id=exfat-vol-flags-single-sector-write):
  写回操作是 partition-relative 扇区 0 的 1 扇区写；不触碰扩展 boot 区
  （扇区 1-11）、OEM 参数、reserved、boot 校验和；副 boot 镜像（扇区 12-23）的
  同步责任在 Wave B v1 之外。

**Invariant** (id=exfat-vol-flags-eager-write):
  与 Linux「仅 DIRTY 上升才 sync_dirty_buffer」的写回延迟优化不同：LiteOS-A
  los_part_write 本就是同步阻塞，因此不区分 DIRTY 上升 / 下降两种过渡，
  始终在 final_flags 与 vol_flags 不同时立即写回。

**Invariant** (id=exfat-vol-flags-boot-buf-patched-before-write):
  los_part_write 入参 buf == sbi->boot_buf，且在调用前必须先完成 in-memory patch
  （结构体字段 vol_flags 与 in-memory 标志一致）。任何盘面状态都应是 boot_buf
  当时内容的镜像，绝不出现「写回了旧值」的情况。

## Refine Prompt
Locking discipline（与功能 spec 分离的关注点）:

- 调用方在进入 helper 之前必须持有 `sbi->s_lock`（LosMux，已成功 LOS_MuxLock）；
  helper 不再调用 LOS_MuxLock / LOS_MuxUnlock。
- 同一线程不允许在 helper 内部释放再重获 s_lock：boot_buf patch 与 los_part_write
  之间禁止释放锁，否则另一个 vol_flags 调用可能穿插，导致 boot_buf 状态与盘面
  状态错位。
- 调用方负责异常路径解锁：典型模式下，`exfat_set_volume_dirty` 在 goto-stack
  上紧跟 `LOS_MuxLock(&sbi->s_lock, ...)` 之后；任何错误返回都应通过统一的
  `goto unlock_out:` 释放，而非在 helper 内部解锁。
- 与 `sbi->bitmap_lock` / `sbi->inode_hash_lock` 的获取顺序硬约束：
  s_lock < bitmap_lock < inode_hash_lock。在持 s_lock 时再请求其他两把锁是允许的
  （写路径 set_volume_dirty → 簇分配 / inode 改 attr 序列）；反向顺序（持
  bitmap_lock 再请求 s_lock）禁止，否则与本 helper 死锁。
- helper 自身不会触发任何嵌套锁请求，故 s_lock 单次锁定深度始终为 1。
