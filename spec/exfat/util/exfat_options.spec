[PROMPT]
LiteOS-A 实现 exFAT mount 选项串解析器 `exfat_parse_options`。输入 `data` 是
逗号分隔的 `key=value` 对（或对开关式 key 单独出现），输出写入调用者提供的
`exfat_mount_options` 结构。v1 接受的 key 列表与 Linux `fs_parameter_spec
exfat_parameters[]` 子集对齐：iocharset、errors、uid、gid、fmask、dmask、
umask、discard、time_offset、allow_utime。其它 key 一律返回 `-EINVAL`。

本规范是**纯字符串解析**，不持有锁、不分配 sbi、不接触磁盘——隔离的可单测
逻辑。它的输出供 `VfsExfatMount` 在 sbi 已 zalloc 但未触磁盘前消费。

## First Prompt

[RELY]
```c
/* —— util.header 已导入 common.header（含 exfat_mount_options、enum exfat_error_mode）—— */

/* libsec / libc 字符串与转换 */
size_t  strlen(const char *s);
char   *strchr(const char *s, int c);
int     memcmp(const void *s1, const void *s2, size_t n);
errno_t strncpy_s(char *dest, size_t destMax, const char *src, size_t count);
errno_t memset_s(void *dest, size_t destMax, int c, size_t count);

/* 内存分配（v1 解析期不分配；iocharset 字段仅指向静态字符串 "utf8"）*/
extern UINT8 *m_aucSysMem0;
VOID *zalloc(size_t size);
UINT32 LOS_MemFree(VOID *pool, VOID *ptr);

/* 错误码（POSIX errno）*/
#define EINVAL  22
#define ERANGE  34
```

[GUARANTEE]
```c
/*
 * exfat_parse_options — 解析 mount 选项串到 exfat_mount_options。
 *
 * 调用约定：
 *   - 调用方在调用前**已经填好** opts 的默认值（典型由 VfsExfatMount Step 4 写入：
 *     fs_uid/fs_gid 继承 mount->vnodeBeCovered；fs_fmask/dmask=0022；utf8=1；
 *     errors=EXFAT_ERRORS_RO；其它清零）。
 *   - 本函数**只覆盖** data 中显式给出的 key 对应字段；未出现的 key 保持调用方默认。
 *   - 不持有任何锁；无并发上下文（mount 路径在持 mount->mountLock 时调用）。
 *   - 纯函数性质：无 IO，无 sbi 引用，无全局状态修改。
 *
 * 入参：
 *   data   NULL 或 '\0' 结尾 C 字符串。NULL/空串视为"全部使用默认"，立即返回 0。
 *   opts   非 NULL，已填好默认。本函数原位修改其字段。
 *
 * 返回：
 *   0       成功，所有给出的 key 都已被认可并写回 opts。
 *   -EINVAL 出现未识别的 key、值无法解析（非数字 / 非合法枚举字符串）、
 *           或 iocharset 不是 "utf8"。
 *   -ERANGE 数值字段超出允许范围（详见各字段 Cases）。
 *
 * 错误回滚：失败时 opts 字段中**已经被本次 parse 覆盖过**的可能保留中间状态；
 *          v1 不强制原子性——调用方在 -EINVAL 时直接走 mount.spec 的
 *          ERROR_OPTS 标签释放 sbi（opts 内嵌 sbi 中），中间状态无外溢。
 */
int exfat_parse_options(const char *data, exfat_mount_options *opts);
```

[SPECIFICATION]

**Pre-Condition**:
- `opts != NULL`，且调用前已写入完整默认值（不依赖 zero-init）。
- `data` 可为 NULL 或 '\0' 结尾 C 字符串。
- 当前线程不持有任何锁（v1 mount 路径在 sbi 暴露前调用）。

**Post-Condition**:

**Case 1（成功，返回 0）**：

`data` 解析为零或多个 `<key>` 或 `<key>=<value>` 项，分隔符 `,`。每项的 key 映射如下：

| key            | value 形式      | 写入字段                | 范围/接受值                                |
|----------------|-----------------|-------------------------|---------------------------------------------|
| `iocharset`    | 字符串          | `opts->iocharset`        | 仅 `"utf8"`（静态 "utf8"）；同时 `opts->utf8 = 1` |
| `errors`       | `continue`/`panic`/`remount-ro` | `opts->errors`  | 三选一枚举                                  |
| `uid`          | 十进制 uint32   | `opts->fs_uid`           | `[0, UINT32_MAX]`                            |
| `gid`          | 十进制 uint32   | `opts->fs_gid`           | `[0, UINT32_MAX]`                            |
| `umask`        | 八进制 0..0777  | `opts->fs_fmask = fs_dmask` 同时写两字段 | `[0, 0777]` |
| `fmask`        | 八进制          | `opts->fs_fmask`         | `[0, 0777]`                                  |
| `dmask`        | 八进制          | `opts->fs_dmask`         | `[0, 0777]`                                  |
| `discard`      | 无 value（开关）| `opts->discard = 1`      | （v1 解析但运行时忽略——块层不支持 trim）|
| `time_offset`  | 十进制 int32    | `opts->time_offset`      | `[-24*60, 24*60]`，否则 `-EINVAL`            |
| `allow_utime`  | 八进制          | `opts->allow_utime`      | `[0, 0777]`                                  |

`data == NULL` 或 `*data == '\0'` 时直接返回 0，不修改任何字段。

**Case 2（未识别 key，返回 `-EINVAL`）**：
出现 Case 1 表中之外的任何 key（含 Linux 已废弃的 `utf8` `debug` `namecase` `codepage`——v1 不接受）。

**Case 3（无法解析的 value，返回 `-EINVAL`）**：
- `errors=` 后非 `continue`/`panic`/`remount-ro`；
- 数字字段含非数字字符（uint32/octal/int32 解析失败）；
- 应有 value 的 key 缺 `=` 或 value 为空（除 `discard` 外）；
- `discard` 后跟 `=...`（discard 是开关式，不接 value）。

**Case 4（数值越界，返回 `-ERANGE`）**：
- `time_offset` 超出 `[-24*60, 24*60]`；
- `umask`/`fmask`/`dmask`/`allow_utime` 超出 `[0, 0777]`（注意是 9-bit mask，不是 12-bit）；
- `uid`/`gid` 数字溢出 uint32（罕见；接受任何能表示的值）。

**Case 5（iocharset 非 utf8，返回 `-EINVAL`）**：
v1 仅支持 UTF-8；`iocharset=gbk`、`iocharset=ascii` 等都失败。返回 `-EINVAL`。
**不**返回 `-ENOTSUP`（与 Linux 不一致；保持 errno 集合简单）。

**Invariant** (id=exfat-options-no-globals-no-io):
解析过程不读写任何全局变量、不调用 IO 接口、不进行内存分配（iocharset 设为
静态字符串字面量地址，避免堆分配带来的释放责任问题）。

**Invariant** (id=exfat-options-defaults-preserved):
未在 `data` 中显式出现的 key，其对应字段在 opts 中保持**调用方写入的默认值
不变**。这是与 mount.spec Step 4 协作的硬约束——mount 已写好默认，parser 只覆盖显式项。

**Invariant** (id=exfat-options-strict-key-rejection):
未识别的 key 一律返回 `-EINVAL`，绝不"宽容跳过"。即使是 Linux 历史上的废弃项
（utf8/debug/namecase/codepage）也不静默接受——v1 简化兼容面。

**Invariant** (id=exfat-options-no-heap-leak):
失败路径上不留下任何已分配的堆内存。这意味着 v1 解析全程不调用 zalloc/LOS_MemAlloc——
iocharset 直接指向静态字符串 `"utf8"` 的地址（在 .rodata 段），由调用方在
sbi 释放时**不**调用 LOS_MemFree。

**Invariant** (id=exfat-options-octal-mask-bit-width):
所有 mask 字段（umask/fmask/dmask/allow_utime）按 9-bit 解析（最大 0777）。
解析器拒绝任何高位被置位的值。这与 POSIX 文件权限位一致。
