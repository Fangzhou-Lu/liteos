<!--
Step 6 用户审核 aid — 渲染给用户的 markdown checklist，含自动填值。
Step 6 user review aid — markdown checklist rendered to USER (not LLM),
with status auto-filled from grep / regex / build-log parsing.

History/version: see ../CHANGELOG.md.
-->

# Step 6 Review — Stage `{STAGE_ID}`

## 1. Symbol existence
For each LiteOS-A API referenced in generated code, plugin auto-greps the repo:

{SYMBOL_EXISTENCE_TABLE}

Format:
| Symbol | Found? | Source |
|---|---|---|
| `VnodeAlloc` | ✅ | `fs/vfs/include/vnode.h` |
| `los_part_find` | ✅ | `drivers/block/disk/include/disk.h` |
| `<unfound symbol>` | ❌ | **flag for user — likely hallucination** |

## 2. Pattern consistency (auto-checked %)
- [ ] Goto-stack reverse-LIFO error handling (regex `goto ERROR_`): {GOTO_STACK_PCT}%
- [ ] Internal positive errno + final `return -ret`: {ERRNO_PCT}%
- [ ] `bind_check` → `SetDiskPartName` → resource init sequence preserved (mount specs only): {BIND_CHECK_OK}
- [ ] `mount->data = sbi` AND `mount->vnodeCovered = vp` both set (mount specs only): {MOUNT_DATA_OK}
- [ ] `VfsHashInsert(vp, root_dir)` placed AFTER all internal state initialized (mount specs only): {VFSHASH_OK}

## 3. Style rule compliance (auto-checked)
- [ ] Copyright header: BSD-3-Clause Huawei Device template: {COPYRIGHT_OK}
- [ ] All `strcpy`/`strncpy`/`memcpy`/`memset`/`sprintf` are `_s` variants:
 libsec count = {LIBSEC_COUNT}, unsafe count = {UNSAFE_COUNT}
- [ ] `LOSCFG_FS_<NAME>` ifdef wraps body: {IFDEF_OK}
- [ ] No `kmalloc` / `kzalloc` / `kfree` / bare `malloc` / `free`: {ALLOC_OK}
- [ ] No `printk` / `pr_err` / `pr_info` (replaced with PRINT_ERR/INFO/PRINTK): {PRINTK_OK}
- [ ] Lock primitive matches sleep-context (mux for sleep paths, spin else): {LOCK_OK}

## 4. Format-compatibility self-check (Step 0 ask-first echo)
For each of the 4 traps from Step 0 ask-first scan:
- [ ] Checksum algorithm: {CRC_STATUS} (irrelevant / addressed / **unaddressed**)
- [ ] Byte order: {ENDIAN_STATUS}
- [ ] Charset: {CHARSET_STATUS}
- [ ] Alignment / packed: {ALIGN_STATUS}

If any unaddressed → block approval.

## 5. Cross-stage invariant preservation
For each ancestor invariant ID, plugin generates a static check or asks user:

{INVARIANT_CHECK_TABLE}

Format:
| Invariant ID | Source | Auto-check | User confirms |
|---|---|---|---|
| `exfat-mount-locked-on-success` | mount | regex confirms mount->data still non-NULL on success | [ ] |
| `exfat-part-name-claimed` | mount | regex confirms SetDiskPartName + free pair | [ ] |

## 6. QEMU smoke baseline (Step 5.2 output)
Plugin runs the smoke commands and (optionally) shows side-by-side log
comparison against a cached prior-approved baseline if one exists:

```
Backup version (from prior approved run):
{BACKUP_QEMU_LOG}

New version (this round):
{NEW_QEMU_LOG}

Diff highlighted:
{QEMU_DIFF}
```

User confirms equivalence or that differences are intentional improvements.

## 7. Auto-step status (Loop code 6-step pipeline)
- [ ] Step 2.1 clangd LSP: {COMPILE_STATUS} (clean / N issues)
- [ ] Step 2.2 style audit: {STYLE_STATUS} (is_good / score / violations)
- [ ] Step 2.3 kernel build: {BUILD_STATUS} (pass / fail / image_path)
- [ ] Step 3 cmocka build: {CMOCKA_BUILD_STATUS} (pass / fail / N testpoints compiled)
- [ ] Step 4 spec/code audit: {AUDIT_STATUS} (skipped / is_good=true / comments=...)
- [ ] Step 5.1 cmocka exec: {CMOCKA_EXEC_STATUS} (pass N/N / fail / skipped)
- [ ] Step 5.2 QEMU smoke: {QEMU_STATUS} (pass / fail)

## 8. cmocka test draft (Step 3)
- [ ] test draft: {TESTGEN_STATUS} (skipped / N testpoints / iterations=M)

## 9. Diff vs ancestor / vs backup
```
{CODE_DIFF}
```

## User actions

- [ ] **Approve** — all checks pass; plugin will `git add` files (no commit)
- [ ] **Suggest edits** — provide free-form text below; plugin will inject as
 `[Modification suggestions]` source=user and re-roll
- [ ] **Inline-edit** — user manually edits files, plugin acknowledges and
 records as approved with manual override
- [ ] **Reject and regenerate** — discard this round, regen from spec
- [ ] **Reject and revise spec** — go back to Loop spec; mark spec as needing edit

User feedback (if Suggest):
```
{USER_FEEDBACK_PLACEHOLDER}
```
