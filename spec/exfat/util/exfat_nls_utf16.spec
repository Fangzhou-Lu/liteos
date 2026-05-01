[PROMPT]
exfat_nls_utf16 —— UTF-16LE 与 UTF-8 双向转换 + 不区分大小写 (upcase-aware)
文件名比较。三件套：

1. `exfat_uni_to_utf8(uni[], uni_len, out_buf, out_max)` —— UTF-16LE 名字段
   解码为 UTF-8 字节流。处理 surrogate pair (高 0xD800..0xDBFF + 低
   0xDC00..0xDFFF → 4 字节 UTF-8)。返回写入字节数 ≥ 0 或 -EINVAL/-ENAMETOOLONG。

2. `exfat_utf8_to_uni(utf8, utf8_len, uni_buf, uni_max, *uni_len)` —— UTF-8
   编码 → UTF-16LE 码元数组。拒 overlong 形式、surrogate 半字 (D800..DFFF
   作输入)、超 0x10FFFF 码点。supplementary plane (>0xFFFF) 写为 surrogate
   pair (占 2 个 uint16)。

3. `exfat_uniname_cmp(sbi, a[], a_len, b[], b_len)` —— 比较两个 UTF-16 名。
   每个 BMP 码元先经 `sbi->vol_utbl` upcase 再比较；surrogate pair 整体
   bit-exact 比较 (Microsoft spec 规定 upcase 仅适用于 BMP 范围)。
   语义同 memcmp: <0 / 0 / >0；长度不同：短的为 "less"。

仅内存计算——无 IO，无 alloc，无锁。本 stage 只做 BMP (Basic Multilingual
Plane) 完整支持 + supplementary plane bit-exact 比较的最低集；v1 不支持
Unicode normalization (NFC/NFD)、ICU collation 等高阶规则——文件名按
Microsoft exFAT 规范的 OemName / FileName 格式逐 code unit 比较即可。

[RELY]
```c
/* common.header 已声明 (exfat_sb_info / exfat_inode_info) */

/* libsec */
extern int memcpy_s(void *dest, size_t destMax, const void *src, size_t count);

/* 日志 */
extern void PRINT_ERR(const char *fmt, ...);

/* exfat_raw.h / exfat.h */
#define EXFAT_MAX_NAME_LEN     255    /* UTF-16 code units (Microsoft 上限) */

/* upcase 表来自 sbi->vol_utbl (65536 entries × 2 字节 = 128KB)；
 * upcase[c] = uppercase form of BMP code unit c。
 * 由 exfat_create_upcase_table 在 mount 期填充；本 stage 只读。
 */

/* LE → host 包装；恒等于 LE host (与 fat_chain / dentry_iter 同款) */
#define LE16_TO_HOST(x) ((uint16_t)(x))

/* UTF-8 / UTF-16 常量 */
#define UTF16_HIGH_SURROGATE_MIN   0xD800u
#define UTF16_HIGH_SURROGATE_MAX   0xDBFFu
#define UTF16_LOW_SURROGATE_MIN    0xDC00u
#define UTF16_LOW_SURROGATE_MAX    0xDFFFu
#define UNICODE_MAX                0x10FFFFu
#define UNICODE_BMP_MAX            0xFFFFu
#define UTF8_MAX_PER_BMP           3       /* 1 BMP 码点最多 3 字节 */
#define UTF8_MAX_PER_SUPP          4       /* 1 supplementary 码点 4 字节 */
```

[GUARANTEE]
```c
/*
 * 调用约定:
 * - 纯计算，无 IO/无 alloc/无锁。spinlock-safe (与 fat_chain 的
 *   clu_to_sector 同等级)。
 * - 入参: uni 是 UTF-16LE 码元数组 (uni[i] 已是 host endianess——调用方
 *   在读 dentry 时通过 LE16_TO_HOST 转换；本函数不重复转换);
 *   uni_len ≥ 0; uni_len ≤ EXFAT_MAX_NAME_LEN.
 * - out_max 必须 ≥ 1; 输出 NUL-terminated 时考虑 +1 byte.
 * - 成功 (≥ 0): 返回写入 out 的字节数 (不含可选 NUL terminator)；
 *   out[returned..out_max-1] 不动。
 * - 失败 (<0): 返回负 POSIX errno; out 内容未定义 (调用方不应依赖)。
 * - **不**写入 out terminator——调用方按需自行写 '\0' (本函数返回字节数
 *   不算 terminator，与 strlen 语义一致)。
 */
int exfat_uni_to_utf8(const uint16_t *uni, int uni_len,
                      char *out, int out_max);

/*
 * 调用约定:
 * - 纯计算 (同 uni_to_utf8)。spinlock-safe。
 * - 入参 utf8 按 utf8_len 字节读 (不依赖 NUL terminator); utf8_len ≥ 0.
 * - 出参: uni 接收 UTF-16 码元；uni_max 是 uni 数组容量 (uint16 个数);
 *   *uni_len 写入实际码元数。
 * - supplementary plane 码点占 2 个 uni 元素 (surrogate pair)。
 * - 成功 (=0): *uni_len 写入；uni[0..*uni_len-1] 填充。
 * - 失败 (<0): 负 errno; *uni_len 与 uni 内容未定义。
 *
 * 拒绝条件 (返 -EINVAL):
 *   1. UTF-8 字节序列违反 RFC 3629 (overlong / 非续字节 / 起始字节非法).
 *   2. 解码出 BMP surrogate 码点 (0xD800..0xDFFF) ——这些只能由 UTF-16
 *      surrogate pair 表示，UTF-8 不允许直接编码。
 *   3. 码点 > 0x10FFFF.
 *   4. utf8_len 为正但起始字节越界 (例如 utf8 == NULL).
 *
 * 缓冲不足 (返 -ENAMETOOLONG):
 *   *uni_len 估算超出 uni_max。
 */
int exfat_utf8_to_uni(const char *utf8, int utf8_len,
                      uint16_t *uni, int uni_max, int *uni_len);

/*
 * 调用约定:
 * - 纯计算。spinlock-safe (访问 sbi->vol_utbl 是读，无锁需求；
 *   sbi 在 boot sector 解析后即冻结其几何字段，vol_utbl 在
 *   exfat_create_upcase_table 后只读——参见 frozen contract)。
 * - 入参: a/b 是 UTF-16 码元数组 (host endianess); a_len/b_len ≥ 0;
 *   sbi != NULL && sbi->vol_utbl != NULL.
 * - upcase 应用规则: a[i] / b[i] 各自先用 vol_utbl 转大写
 *   (但仅当 a[i] 不是 surrogate half——否则不变)；之后逐元素比较。
 *   注意: surrogate pair (high+low) 整体当作 supplementary 码点比较，
 *   两侧 high/low 都不查 vol_utbl。
 * - 比较结果: < 0 / 0 / > 0 同 memcmp 语义。长度不同时 prefix 完全相等
 *   则短的为 "less"。
 *
 * 错误返回: 不可能——本函数无 -E* 错误路径 (假设输入合法；非法输入
 *   行为未定义但不崩溃，最坏情况返回任意大小关系)。
 *   sbi == NULL / sbi->vol_utbl == NULL → 返回 -EINVAL (sentinel)。
 */
int exfat_uniname_cmp(const exfat_sb_info *sbi,
                      const uint16_t *a, int a_len,
                      const uint16_t *b, int b_len);
```

[SPECIFICATION]

**Pre-Condition (exfat_uni_to_utf8)**:
uni != NULL || uni_len == 0; 0 ≤ uni_len ≤ EXFAT_MAX_NAME_LEN;
out != NULL; out_max ≥ 1.

**Post-Condition (Case 1: success)**:
返回 written (≥ 0)；written ≤ out_max；out[0..written-1] 是 uni 解码后的
UTF-8 字节序列。具体规则：
- uni[i] in [0x0001, 0x007F]: 1 byte 0xxxxxxx
- uni[i] == 0x0000: 2 bytes 0xC0 0x80 (Modified UTF-8 of NUL，避免与
  C 字符串 terminator 混淆) —— **或** 1 byte 0x00；本 v1 选 1 byte 0x00
  与 Linux exfat 一致 (不使用 Modified UTF-8)。
- uni[i] in [0x0080, 0x07FF]: 2 bytes
- uni[i] in [0x0800, 0xFFFF] excluding [0xD800, 0xDFFF]: 3 bytes
- uni[i] high surrogate + uni[i+1] low surrogate: 4 bytes; consume 2 units.

**Post-Condition (Case 2: invalid surrogate)**:
高 surrogate 后非低 surrogate，或孤立低 surrogate → 返回 -EINVAL。
out 内容未定义。

**Post-Condition (Case 3: buffer too small)**:
解码所需字节数 > out_max → 返回 -ENAMETOOLONG。
out[0..out_max-1] 内容未定义 (实现可能写了部分前缀)。

**Pre-Condition (exfat_utf8_to_uni)**:
utf8 != NULL || utf8_len == 0; utf8_len ≥ 0;
uni != NULL; uni_max ≥ 1; uni_len_out != NULL.

**Post-Condition (Case 1: success)**:
返回 0；*uni_len_out 写入 (≥ 0)；*uni_len_out ≤ uni_max；
uni[0..*uni_len_out-1] 是 utf8 编码后的 UTF-16 码元序列。

**Post-Condition (Case 2: malformed UTF-8)**:
- 起始字节非法 (0x80..0xBF, 0xC0..0xC1, 0xF5..0xFF)；
- 续字节缺失或不在 [0x80, 0xBF]；
- overlong 编码 (例如 0xC0 0x80 编码 NUL) ；
- 编码出 surrogate 码点 (0xD800..0xDFFF) ；
- 编码出码点 > 0x10FFFF.
任一条件 → 返回 -EINVAL；*uni_len_out / uni 未定义。

**Post-Condition (Case 3: insufficient uni buffer)**:
某次需写入 1 或 2 个 uint16 但 uni_max 不够 → 返回 -ENAMETOOLONG；
*uni_len_out / uni 未定义。

**Pre-Condition (exfat_uniname_cmp)**:
sbi != NULL && sbi->vol_utbl != NULL;
a != NULL || a_len == 0; b != NULL || b_len == 0;
a_len ≥ 0 && b_len ≥ 0.

**Post-Condition (Case 1: equal)**:
逐元素 (BMP 经 upcase, surrogate pair 整体) 比较，所有位置相等且
a_len == b_len → 返回 0。

**Post-Condition (Case 2: a precedes b)**:
某位置 i 上 upcased(a[i]) < upcased(b[i])；或 a 是 b 的 strict prefix
(a_len < b_len 且 a_len 个码元都相等) → 返回 < 0。

**Post-Condition (Case 3: a follows b)**:
对称 Case 2 → 返回 > 0。

**Post-Condition (Case 4: invalid args)**:
sbi == NULL 或 sbi->vol_utbl == NULL → 返回 -EINVAL。

**Invariant** (id=exfat-nls-utf16-pure):
所有三个函数无 IO / 无 LOS_MemAlloc / 无 LOS_MemFree / 无 LOS_MuxLock / 无
LOS_SpinLock。spinlock-safe。该 invariant 由 [RELY] 段未引入这些 API 直接
保证 — codegen 不可引入新依赖。

**Invariant** (id=exfat-nls-utf16-readonly):
本 stage 任何函数都不调用 los_part_write / los_disk_write，不写 sbi 任何
字段，不写 vol_utbl。

**Invariant** (id=exfat-nls-utf16-le-host-only):
LE16_TO_HOST 是恒等宏，依赖 LE host (ARM-LE)。**调用方**负责在读 dentry
name1..nameK 字段时已做 LE16 → host 转换；本函数不重复转换。BE host 端口
须改调用方。继承 fat_chain / dentry_iter 同策略。

**Invariant** (id=exfat-nls-utf16-utf8-rfc3629-strict):
exfat_utf8_to_uni 严格按 RFC 3629 拒绝：
- overlong 编码 (任何码点用比最少字节多的字节数编码)
- surrogate 半字直接编码 (D800..DFFF 不允许出现在 UTF-8 输入)
- > 0x10FFFF 码点 (历史 5/6-byte UTF-8 全拒)
v1 不接受 "宽松 UTF-8" / Modified UTF-8 / WTF-8 / CESU-8。

**Invariant** (id=exfat-nls-utf16-surrogate-pair-bit-exact):
exfat_uniname_cmp 对 surrogate pair (high + low 共 2 uint16) 的处理:
**整体当作 supplementary 码点比较，bit-exact 不查 vol_utbl**。这与 Microsoft
exFAT 规范一致——upcase 表只覆盖 BMP，supplementary plane 默认无 case
转换。**绝不**对 high 或 low 单独查 vol_utbl，因为查到的会是 surrogate
半字本身的"upcase"(常常是该半字本身)，但语义错误。

**Invariant** (id=exfat-nls-utf16-name-len-bounded):
所有三个函数对 uni_len / utf8_len 的处理上界为 EXFAT_MAX_NAME_LEN
(255 UTF-16 units) 与 EXFAT_MAX_NAME_LEN * UTF8_MAX_PER_BMP (765 字节)。
超过 → 直接返回 -ENAMETOOLONG (uni_to_utf8 / utf8_to_uni)；超过这个
上限的 lookup/readdir 名一律拒。这是 Microsoft 规范上限，不是性能优化。

**Invariant** (id=exfat-nls-utf16-zero-name-allowed):
uni_len == 0 / utf8_len == 0 / a_len == 0 / b_len == 0 是合法输入：
- uni_to_utf8: written = 0；返回 0。
- utf8_to_uni: *uni_len = 0；返回 0。
- uniname_cmp: 空名比较：两侧都空 → 0；一侧空一侧非空 → 空的为 less。

**Invariant** (id=exfat-nls-utf16-no-modified-utf8):
NUL 字节 (uni[i] == 0x0000) 在 uni_to_utf8 中编码为单字节 0x00 (与 Linux
exfat 一致)，**不**使用 Modified UTF-8 的 0xC0 0x80。这意味着输出 UTF-8
可包含 NUL，调用方若需 C string 必须额外检查或拷贝。Linux exfat 对应
位置的逻辑 (见 nls.c::utf16s_to_utf8s) 也是单字节 0x00；本 v1 严格对齐。

**Invariant** (id=exfat-nls-utf16-no-spinlock-safe-exception):
与 fat_chain / dentry_iter 不同，本层 **可以**在持自旋锁时调用——
所有三函数纯计算，无睡眠 API。该例外由 invariant exfat-nls-utf16-pure
保证。

**Invariant** (id=exfat-nls-utf16-vol-utbl-immutable-after-mount):
exfat_uniname_cmp 假设 sbi->vol_utbl 在 mount 之后是只读的 — 由 mount
阶段 invariant exfat-mount-mount-data-self-consistent 提供。本 stage
不主动加锁保护读取；如果未来引入 remount 写 vol_utbl 路径，本 invariant
失效，须重审。

**Invariant** (id=exfat-nls-utf16-cmp-prefix-rule):
exfat_uniname_cmp 当 a 为 b 的 strict prefix 时 (即 a_len < b_len 且
前 a_len 个 (upcased) 码元相等) 返回 < 0；对称返 > 0；完全相同返 0。
与 strcmp / memcmp 的 prefix 规则一致。
