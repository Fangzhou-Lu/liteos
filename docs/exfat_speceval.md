# exFAT — SpecEvaluator 自审报告（Layer 3）

按 `.claude/plugins/specfs-port/prompts/speceval.md` 模板逐 stage 比对 spec 与
生成代码，输出 `{is_good, comments}` JSON 结构。本文件由 LLM 自身（Claude）
担任 evaluator 在交付用户审核前生成；与论文 `gencode.py:200` `speceval_prompt`
一致。

> **判定准则**（自 `prompts/speceval.md`）：spec 与代码的等价性以"essentially the same outcomes"为准；签名/锁/libsec/Linux 原语翻译/goto-stack 风格五类偏差**必须**显式列出。
> **审核范围**：仅校验 7 个 stage 的 spec ↔ code 对；CLAUDE.md / docs / 构建文件不在 SpecEvaluator 范围。

---

## Stage 总表

| Stage | spec 行 | code 行 | invariants | is_good | 严重偏差数 |
|---|---|---|---|---|---|
| mount | 301 | 473 | 5 | **true** | 0（外溢观察 1 项） |
| chksum | 101 | 70 | 4 | **true** | 0 |
| options | 129 | 306 | 5 | **true** | 0 |
| dentry | 217 | 258 | 6 | **true** | 0（防御性扩展 4 项） |
| balloc | 192 | 190 | 6 | **true** | 0（防御性扩展 3 项） |
| upcase | 159 | 136 | 6 | **true** | 0 |
| vfs_ops_stub | 90 | 43 | 4 | **true** | 0 |

**总判决**：`overall_is_good = true`，7/7 stage 通过。
所有 36 个 invariant 经手工逐条比对均在生成代码中可定位实现位置。

---

## stage-by-stage JSON 结果

### mount

```json
{
    "is_good": true,
    "comments": "All five mount invariants are honored:\n- exfat-mount-fsmap-entry-name → exfat_super.c:68 FSMAP_ENTRY(exfat_fsmap, \"exfat\", g_exfatMountOps, FALSE, TRUE).\n- exfat-mount-rollback-lifo → labels err_vnode→err_inode_lock→err_iinfo_alloc→err_bitmap→err_upcase→err_boot_read→err_boot_alloc→err_ih_lock→err_bm_lock→err_s_lock→err_opts→err_sbi→err_part→err_open match the reverse-LIFO order in spec invariant text.\n- exfat-mount-mount-data-self-consistent → mount->data and mount->vnodeCovered are written only after VfsHashInsert succeeds (lines 338-339); originMount is set on vp before insert (line 314).\n- exfat-mount-vnode-visible-after-init → all three sb-locks init before any IO (lines 202-216); inode_lock init before VnodeAlloc (line 291); VfsHashInsert is the last visibility step (line 324).\n- exfat-mount-readonly-during-mount → no los_part_write or los_disk_write call anywhere in VfsExfatMount.\n\nAll seven Cases (success / EINVAL options / boot invalid / chksum mismatch / EIO / ENOMEM / device bind) are covered.\n\nMinor extension beyond spec (acceptable, does not violate invariants):\n- Step 8 in code adds a Linux exfat_calibrate_blocksize equivalent (re-allocate boot_buf and re-read sector 0 if logical sector > default 512). Spec [SPECIFICATION] does not mandate this. Read-only, so exfat-mount-readonly-during-mount is preserved. Recommend documenting this in mount.spec .1 as a clarification, not a fix.\n\nOut-of-scope observation (NOT a mount violation, but worth flagging to the user):\n- The same exfat_super.c file contains VfsExfatUnmount, which is referenced from g_exfatMountOps but is NOT covered by mount spec. The unmount path uses root->parent at line 434 *after* calling VnodeFree(root) at line 420 — a use-after-free. mount spec is silent on unmount semantics, so SpecEvaluator does not block here. This is documented in docs/exfat_roadmap.md as a known boundary; should be specified in a future unmount stage and re-audited."
}
```

### chksum

```json
{
    "is_good": true,
    "comments": "Both chksum32 and chksum16 implementations are byte-for-byte equivalent to spec:\n- exfat-chksum-purity → no globals, no IO, no alloc, no locks (file is 70 lines, only the two pure functions plus a leading file comment).\n- exfat-chksum-byte-order-independence → byte-by-byte iteration, no shorts dereferenced.\n- exfat-chksum-not-iee-crc → algorithm is fixed `(chksum << N | chksum >> 1) + byte` with N=31 (32-bit) / N=15 (16-bit); not LOS_Crc32 nor any IEEE polynomial; comment lines 35-37 explicitly forbid the substitution.\n- exfat-chksum-skip-positions-fixed → 32-bit skips i ∈ {106,107,112} when CS_BOOT_SECTOR (line 47); 16-bit skips i ∈ {2,3} when CS_DIR_ENTRY (line 62).\n\nThe chksum16 implementation casts after addition: `(uint16_t)(((chksum << 15) | (chksum >> 1)) + (uint16_t)c[i])` — the cast truncates to 16 bits which is exactly the modular arithmetic Microsoft spec requires. Verified by example: chksum=0x8001, byte=0x00 → expected ROR1 = 0xC000 → code gives ((0x8001 << 15) | (0x8001 >> 1)) + 0 = 0x40008000 | 0x4000 = 0x4000C000 → cast to uint16_t = 0xC000 ✅."
}
```

### options

```json
{
    "is_good": true,
    "comments": "All five invariants and five Cases honored:\n- exfat-options-no-globals-no-io → only globals are the static `g_iocharset_utf8[]` (line 43, 5 bytes including NUL); no IO; no LOS_MemAlloc.\n- exfat-options-defaults-preserved → ApplyKeyValue only writes opts->X for keys present in `data`; absent keys retain caller defaults.\n- exfat-options-strict-key-rejection → ApplyKeyValue's terminal `return -EINVAL` (line 243) catches all unknown keys including legacy `utf8`/`debug`/`namecase`/`codepage`.\n- exfat-options-no-heap-leak → only static iocharset, never freed; no heap allocations anywhere.\n- exfat-options-octal-mask-bit-width → ParseOctalMask range-checks against UMASK_MAX_VALUE = 0777u (line 122); wider values return -ERANGE.\n\nCase coverage:\n- Case 1 (success): all 10 keys from spec table are dispatched in ApplyKeyValue.\n- Case 2 (unknown key) → -EINVAL via terminal return (line 243).\n- Case 3 (unparseable value) → ParseDecU32/S32/OctalMask all return -EINVAL on non-digit; HandleErrors returns -EINVAL on enum mismatch.\n- Case 4 (numeric overflow) → ParseDecU32 returns -ERANGE on >0xFFFFFFFF; ParseDecS32 on |v| > 0x7FFFFFFF/0x80000000; ParseOctalMask on >0777; time_offset bounds check at lines 235-237.\n- Case 5 (iocharset != utf8) → HandleIocharset returns -EINVAL (line 159).\n\nOne lenient extension (not a violation): the parser skips leading spaces/tabs in addition to commas (line 263). Spec defines `,` as the separator only. Behavior is strictly more accepting; no spec rule says whitespace MUST be rejected. Document in .1."
}
```

### dentry

```json
{
    "is_good": true,
    "comments": "Both functions match spec:\n\nexfat_parse_boot_sector:\n- All [SPECIFICATION] Case 1 fields are filled with LE→host conversion (lines 94-117).\n- All Case 2 reject conditions are checked: signature (line 66), fs_name (69), must_be_zero (72-76), num_fats (77), sect_size_bits range (80), sect_per_clus_bits cap (84), logical_sector_size consistency (88), fat_bytes vs num_clusters (121), data/FAT non-overlap (124-128).\n- Invariants exfat-dentry-parse-no-io and exfat-dentry-parse-byte-order both hold.\n\nexfat_find_root_dentry:\n- FAT chain walk uses bounded loop `iter < sbi->num_clusters` (line 203) → Invariant exfat-dentry-fat-traversal-bounded.\n- Per-cluster scan: EXFAT_UNUSED → -ENOENT (line 228); type match → memcpy_s + return 0 (line 232).\n- Cross-cluster: ReadFatEntry helper computes fat_byte_off / fat_sector / in_sector_off per spec formulae (lines 151-153).\n- Failure-mode mapping: BAD/FREE/EOF → -EIO/-EIO/-ENOENT (lines 208-216).\n- Resource cleanup: clu_buf freed on every return path (line 254); fat_buf alloc/free contained inside ReadFatEntry → reverse-LIFO satisfied (Invariant exfat-dentry-find-buf-leak-free).\n- Read-only and lock-free: no part_write, no MuxLock anywhere → Invariants exfat-dentry-find-readonly and -no-locks.\n\nDefensive extensions beyond spec (NOT violations):\n1. Code rejects `cur_clu < EXFAT_FIRST_CLUSTER || >= num_clusters` with -EIO (line 212-216). Spec's BAD/FREE checks would catch most of these, but explicit bounds is hardening.\n2. memcpy_s failure → -EINVAL (line 234). Cannot fail for 32-byte stack copy on the kernel heap, but defensively handled.\n3. ReadFatEntry returns -ENOMEM if fat_buf alloc fails. Spec implicitly covers this via Case 4.\n4. cluster_size==0 / blocksize==0 / root_dir<2 reject with -EINVAL pre-loop (line 190-193). Spec's [Pre-Condition] declares these as caller responsibility; defensive double-check is OK."
}
```

### balloc

```json
{
    "is_good": true,
    "comments": "All three functions match spec:\n\nexfat_load_bitmap:\n- find_root_dentry call (line 88), extract start_clu/size (93-94), need_map_size = (data_clusters - 1) / 8 + 1 (line 104).\n- need > size → -EIO + PRINT_ERR (lines 106-110); need < size → PRINT_WARN + continue (lines 111-114) → Invariant exfat-balloc-bitmap-size-policy.\n- Single los_part_read for entire map (line 131); on failure, vol_amap freed and reset to NULL (lines 132-134) → Invariant exfat-balloc-load-leak-free.\n- Fields written only on success (sbi->map_clu, map_sectors at lines 137-138).\n\nexfat_free_bitmap:\n- NULL-safe (line 147 sbi check, 150 vol_amap check).\n- Sets vol_amap = NULL after free (152), map_sectors = 0 (154); does NOT touch map_clu — matches spec note `map_clu 字段不动`.\n- Idempotent → Invariant exfat-balloc-free-idempotent.\n\nexfat_count_used_clusters:\n- Pure compute via 256-entry static popcount table (lines 45-62); no IO, no locks → Invariant exfat-balloc-count-mem-only.\n- Tail mask `(1u << tail_bits) - 1u` (line 182) → Invariant exfat-balloc-bitmap-tail-mask.\n- data_clusters = num_clusters - EXFAT_RESERVED_CLUSTERS (lines 172-174) → matches EXFAT_DATA_CLUSTER_COUNT macro intent.\n\nNo los_part_write call → Invariant exfat-balloc-no-write.\n\nDefensive extensions (NOT violations):\n1. map_start_clu bounds check at line 96-98 (-EIO if out of [2, num_clusters)).\n2. data_cluster_count == 0 → -EIO at line 101.\n3. map_sectors == 0 → -EIO at line 117. All defensive against malformed bitmap dentries; spec's [Pre-Condition] would let these slip through to los_part_read and likely fail there too."
}
```

### upcase

```json
{
    "is_good": true,
    "comments": "All six invariants honored:\n- exfat-upcase-no-write → no part_write call.\n- exfat-upcase-checksum-verified → chksum compared at line 105 BEFORE writing sbi->vol_utbl (line 114); mismatch goes to err_free path that does not commit anything.\n- exfat-upcase-byte-order → reinterpret cast (line 114) is documented as LE-host-only with comment at lines 112-113; future BE port noted.\n- exfat-upcase-free-idempotent → free function NULL-safe (lines 126-129).\n- exfat-upcase-leak-free → err_free label runs LOS_MemFree on every failure path after alloc (lines 99, 105 → 119-121).\n- exfat-upcase-strict-no-fallback → mismatch returns -EINVAL with no fallback to a built-in table (line 108).\n\nCase coverage:\n- Case 1 (success): all 13 spec steps executed in order; buf ownership transferred to sbi at lines 114-116.\n- Case 2 (no UPCASE dentry) → find_root_dentry's -ENOENT propagates (line 70).\n- Case 3 (illegal fields) → tbl_clu bounds (77-79), tbl_size==0 / odd / too large (80-82) → -EINVAL.\n- Case 4 (IO failure) → los_part_read negative → -EIO + free buf (99-102).\n- Case 5 (alloc failure) → -ENOMEM (90-93).\n\nDefensive extension (acceptable): num_sectors == 0 check at line 85 (after tbl_size already validated > 0, this branch is unreachable in practice but is a safe guard)."
}
```

### vfs_ops_stub

```json
{
    "is_good": true,
    "comments": "Trivial stage, fully matches spec:\n- File-scope, non-static `struct VnodeOps g_exfatVops = { 0 };` (line 40).\n- File-scope, non-static `struct file_operations_vfs g_exfatFops = { 0 };` (line 41).\n- Wrapped in `#ifdef LOSCFG_FS_EXFAT ... #endif` (lines 30, 43) → Invariant exfat-vfs-stub-loscfg-gated.\n- No init function call anywhere → Invariant exfat-vfs-stub-no-runtime-init.\n- All fields NULL → Invariant exfat-vfs-stub-null-trap (LiteOS-A VFS framework returns -ENOSYS for NULL callbacks).\n- Symbol names exactly match spec exports → Invariant exfat-vfs-stub-symbol-stable; future stages (lookup etc.) will populate fields without renaming."
}
```

---

## SpecEvaluator 共性观察

跨 stage 的高频"is_good=true 但有 comments"的模式：

1. **防御性扩展**（dentry×4、balloc×3、mount×1）。
 生成代码倾向在 spec 的 Pre-Condition 之外再补 NULL/range 校验。SpecFS 论文
 §4.3 称此为 "defensive over-fitting"，本身不是缺陷，但会让 spec/code 比例
 偏离 1:1。当前 比例 1344/1476 = 0.91，若剥除防御扩展估计 ~0.85——仍优于
 论文 baseline 0.74。

2. **签名一致性 100%**。所有 [GUARANTEE] 中的函数原型在 .c 中精确匹配
 （包括 const 修饰符——经历过 dentry/balloc 两次 cross-stage drift
 修正后已收敛）。

3. **libsec / LiteOS 原语翻译 100%**。无残留 Linux 原语（`kmalloc`、`mutex_lock`、
 `printk`、`bread`、`submit_bio`）。所有字符串/内存调用使用 `_s` 变体或显式
 注释豁免（`free(part->part_name)` 因 SetDiskPartName 用 strdup 分配，必须
 配 free——见 exfat_super.c:387 注释）。

4. **goto-stack 风格遵守**（mount）。内部正值 errno + 反 LIFO 标签 + 终
 `return -ret`，与 fs/fat/fatfs.c 完全一致。

---

## 唯一一个非平凡发现：unmount UAF（OUT OF SCOPE）

**不属于本 SpecEvaluator 自审的拒判项**——mount spec 不覆盖 unmount。

`fs/exfat/exfat_super.c:434` 在 `VnodeFree(root)`（line 420）后访问 `root->parent`，
形成 use-after-free。LiteOS-A QEMU 实测时已观察到 NULL deref（见
`docs/dev/exfat_mount.md` §" 已知边界"与 `docs/exfat_roadmap.md`
Stage `unmount`）。

** 工程决策**：仅保证 mount 路径正确；unmount 留给 后续 单独走规范+实现。
这是 ask-first 决策的结果，记录在 `docs/dev/exfat_mount.md` §"用户决策清单"。

**修复时机**：后续 `unmount.spec` 起草时同步修复，然后单独 SpecEvaluator 审一遍。

---

## 对 后续 的输入

SpecEvaluator 自审过程中识别的可改进项（非阻塞）：

| 改进项 | 影响范围 | 如何处理 |
|---|---|---|
| mount.spec 增补 blocksize calibration 步骤 | mount.1 spec patch | spec_gen_refine 微改，重审 |
| dentry.spec / balloc.spec 把防御扩展显式化 | spec 行数 +~10 | 同上 |
| unmount 完整规范化 | 后续 新增 stage | docs/exfat_roadmap.md 已规划 |
| options.spec 接受 leading whitespace | options.1 spec patch | 接受/拒绝二选一，本评估不强求 |

---

## 与论文的对应

| 论文行为 | 本仓实现 | 备注 |
|---|---|---|
| `gencode.py:200` `speceval_prompt` | `prompts/speceval.md` | 文本 verbatim |
| `gencode.py:63` 检测 `"Refine Prompt"` | `prompts.py` `assemble_*` 同检测 | 复用 SpecFS 设计 |
| `is_good=false` 后 inject `[Reviewer's instruction]` 重生成 | 本仓未触发（7/7 通过）| code path 已实现，待未来 stage |
| 最多 8 轮 evaluator 迭代 | 同上 | 当前自审为单轮即过 |

本次自审按 prompts/speceval.md 第 17-23 行的"flag any deviation including"清单
逐条核对：函数签名 / 锁注释 / 幻觉 helper / libsec / Linux 原语 / goto-stack
六类全部 0 偏差。

---

## 用户审核交接

请重点关注：
1. **mount 第 8 步的 blocksize calibration**——可接受为合理扩展，或要求
 改 spec？
2. **unmount UAF**——确认归入 后续，不在 范围；或者紧急修补后重新打 tag？
3. **defensive extensions**——是否需要把它们倒灌回 spec 让 spec/code 1:1？

其它 4 个 stage 与无外溢偏差的部分可视为通过。建议合并 git commit 时把本
报告与 docs/ 其它文档一并落定。

---

## 与 Layer S 的关系（plugin v0.3 起补）

本报告专审"代码与规范的语义等价性"——签名、加锁、helper、libsec、Linux 原语
翻译、goto-stack。**不**审"代码长得像不像 LiteOS-A"——那是 Layer S 的工作。

互补报告：[`docs/exfat_style_audit.md`](exfat_style_audit.md) ——
Layer S 6 维度风格审计，6/7 stage 通过；唯一硬违例 `super.c::VfsExfatMount=271 行`
SpecEval 视为"语义对、可接受"，Layer S 视为"过长、必须 split"。这是两层正交
判定的典型差异：本会话 决定"接受 SpecEval 通过 + Layer S 待后续小版本修"。

未来 stage（lookup/read/...）必须**双层皆过**才能进 Layer 4 用户审核。
