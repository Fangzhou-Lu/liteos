[PROMPT]
Provide complete `exfat_inode.c` addition that implements `exfat_calc_num_entries`.
Only include `exfat.h` as the header (already included by the surrounding translation
unit). Output a single C code block; no surrounding boilerplate.

`exfat_calc_num_entries` is a pure-compute helper called by `exfat_add_entry` before
reserving dentry slots in the parent directory. It translates a UTF-16 filename length
into the total number of on-disk directory-entry slots the name consumes:

  1 (file primary dentry)
+ 1 (stream secondary dentry)
+ ceil(name_len / EXFAT_FILE_NAME_LEN)  (EXFAT_NAME secondary dentries, 15 UTF-16
                                          units each)

The formula collapses to `((name_len - 1) / EXFAT_FILE_NAME_LEN) + 3` using integer
division (identical to Linux fs/exfat/dir.c::exfat_calc_num_entries). The result is
always in [3, EXFAT_DENTRY_SET_MAX] for any valid input. No IO, no lock, no alloc.

[RELY]
```c
/* common.header 已声明 (此处仅列本函数直接依赖部分) */

/* exfat_uni_name — 已在 common.header mkdir 段声明 */
struct exfat_uni_name {
    uint16_t name[EXFAT_MAX_NAME_LEN + 1]; /* UTF-16LE 字符序列 */
    uint16_t name_hash;
    uint8_t  name_len;                      /* 有效 UTF-16 单元数，[1, 255] */
};

/* 每个 EXFAT_NAME dentry 可携带的 UTF-16 单元数 (exfat_raw.h) */
#define EXFAT_FILE_NAME_LEN   15

/* 每个 dentry-set 的最大槽数 (dentry_iter stage, common.header) */
#define EXFAT_DENTRY_SET_MAX  19

/* 文件名最大 UTF-16 单元数 (common.header) */
#define EXFAT_MAX_NAME_LEN    255
```

[GUARANTEE]
```c
/*
 * 调用约定：
 * - 纯计算，无 IO / 无 alloc / 无锁。spinlock-safe，可在任何上下文调用。
 * - p_uniname != NULL，且 p_uniname->name_len ∈ [1, EXFAT_MAX_NAME_LEN]；
 *   违反任一约束返回 -EINVAL。
 * - 成功时返回 dentry-set 总槽数，值域 [3, EXFAT_DENTRY_SET_MAX]。
 * - 不读取 p_uniname->name[] 数组内容，也不读取 name_hash；仅读 name_len。
 * - 不修改 *p_uniname 任何字段。
 */
int exfat_calc_num_entries(const struct exfat_uni_name *p_uniname);
```

[SPECIFICATION]

**Pre-Condition**:
p_uniname != NULL；p_uniname->name_len ∈ [1, EXFAT_MAX_NAME_LEN]（即 [1, 255]）。
调用方（exfat_add_entry）已完成 UTF-8 → UTF-16 转换并将结果长度写入 name_len；
当前不持任何锁（本函数不要求，但调用方上下文通常持 sbi->s_lock）。

**Post-Condition (Case 1: 成功)**:
p_uniname != NULL 且 name_len ∈ [1, 255] → 返回整数 n，其中：

  n = ((name_len - 1) / EXFAT_FILE_NAME_LEN) + 3

- n ∈ [3, 19]（即 [3, EXFAT_DENTRY_SET_MAX]）。
- *p_uniname 内容不变。

**Post-Condition (Case 2: NULL 指针)**:
p_uniname == NULL → 返回 -EINVAL；无副作用。

**Post-Condition (Case 3: name_len 越界)**:
name_len == 0 或 name_len > EXFAT_MAX_NAME_LEN → 返回 -EINVAL；无副作用。

**Invariant** (id=exfat-calc-num-entries-positive):
对任意合法输入（name_len ∈ [1, 255]），返回值 n 满足 3 ≤ n ≤ EXFAT_DENTRY_SET_MAX。
下界：name_len=1 → ((0)/15)+3 = 3（1 file + 1 stream + 1 name dentry）。
上界：name_len=255 → ((254)/15)+3 = 16+3 = 19 = EXFAT_DENTRY_SET_MAX。
结果永不为负、永不超过槽上限；调用方可直接将返回值传给 exfat_alloc_dentry_slot
而无需额外范围检查（前提是入参校验已通过）。

**Invariant** (id=exfat-calc-num-entries-pure):
本函数不调用 los_part_read / los_part_write / LOS_MemAlloc / LOS_MemFree /
LOS_MuxLock / LOS_MuxUnlock，也不修改任何全局状态。可在持自旋锁的上下文中
安全调用。
