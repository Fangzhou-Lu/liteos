<!--
Layer T cmocka 测试派生 — 从已批准 spec + 刚生成的代码合成单元测试。
Layer T cmocka test gen — synthesize a cmocka unit-test from approved spec + freshly generated code.

调用时机：Loop B Step 3a，紧跟代码生成之后；测试看到的是真实生成的
C 符号（函数名、签名、文件路径），不再是 spec 抽象。
Triggered: Loop B Step 3a, immediately after codegen so the test references real C symbols, not a spec abstraction.

Inputs:  approved spec ([SPECIFICATION] cases) + just-generated code + existing
         testsuites/unittest/<name>_host/ skeleton.
Output:  single file `test_<stage>.c`, cmocka-style, with setup/teardown +
         one testpoint per Case (happy + each -EXXX negative).

History/version: see ../CHANGELOG.md.
-->

This is a cmocka unit-test synthesis task for a freshly generated LiteOS-A FS
helper. Produce ONE C source file: `test_<stage>.c` covering every Case in the
spec's `[SPECIFICATION]` section.

[Generated code under test — modified files and unified diff]
The code lives in:
{CODE_FILES}

Unified diff vs HEAD (use this to see exactly what was added/changed):
```diff
{CODE_DIFF}
```

If the diff above is missing a symbol you need to test, read the file directly
from disk at the path listed under [Generated code under test — modified files].

[Spec — at path, full file is on disk]
{SPEC_PATH}

[Spec — abridged: PROMPT + GUARANTEE + SPECIFICATION segments only]
The spec's [RELY] / [SCOPE GUARDRAILS] / [REJECTION CRITERIA] / TWO-PHASE
TRIGGER / ASK-FIRST RULES segments are NOT inlined — they're identical
across stages and only matter to spec authoring, not test synthesis. If a
testpoint genuinely needs to inspect them, read the file at the path above.

{ORIGINAL_SPEC_ABRIDGED}

[Existing harness layout]
{HARNESS_LAYOUT}

## Output requirements

1. **One testpoint per Case** in `[SPECIFICATION]`. The Case label maps to the
 testpoint name: `test_<func>_<case>`. Examples:
 - `Case 1 (success)` → `test_<func>_happy`
 - `Case 2 (-ENOENT)` → `test_<func>_enoent`
 - `Case 3 (-EIO)` → `test_<func>_io_error`
 - `Case 4 (-EINVAL)` → `test_<func>_einval`
 - `Case 5 (-ENOMEM)` → `test_<func>_oom` (skip if mock_disk can't simulate it)

2. **For each [Invariant]** that's testable in isolation (no QEMU), add a
 dedicated testpoint named `test_<func>_invariant_<id>`. Examples:
 - `exfat-balloc-free-idempotent` → `test_free_bitmap_idempotent`
 - `exfat-chksum-byte-order-independence` → `test_chksum_byte_order_independent`

3. **Use existing harness primitives**:
 - `mock_disk_load(buf, len)` to set the in-RAM image (typically built via
 `<name>_image_builder_build(&img)` or a negative-variant generator).
 - When generated code calls helpers that require loaded in-memory FS state
 (for example `sbi->vol_amap` for bitmap/free-cluster helpers), initialize that
 state in setup using existing production loaders such as `exfat_load_bitmap()`
 and release it in teardown or at test end with the matching free helper.
 - `mock_disk_set_read_fail_at(N)` for -EIO injection (count reads in the
 order the function makes them; first read is N=1).
 - `mock_disk_reset_counters()` between tests if you reuse the image.
 - `assert_int_equal(rc, 0)`, `assert_int_equal(rc, -EINVAL)`, etc.
 - `cmocka_unit_test_setup_teardown(test_name, setup, teardown)` for tests
 needing image load.

4. **Forbidden**: do NOT call functions that need full VFS framework
 (`VnodeAlloc`, `VfsHashInsert`, `los_part_find`). Those belong to Layer B
 (QEMU LTP). If a Case requires VFS infrastructure, mark it
 `cmocka_unit_test_skip(...)` with a `// LAYER_B: needs QEMU` comment.

5. **File header**: BSD-3-Clause Huawei Device template (mirror existing
 test_*.c files in testsuites/unittest/<name>_host/).

6. **Includes**: standard cmocka headers + `"../../../fs/<name>/include/<name>.h"`
 + `"exfat_image_builder.h"` + `"mock_disk.h"`.

7. **End with**:
 ```c
 const struct CMUnitTest test_<stage>_tests[] = {
 cmocka_unit_test_setup_teardown(test_<func>_happy, <stage>_setup, <stage>_teardown),
 /* ... one entry per testpoint ... */
 };
 const size_t test_<stage>_tests_count =
 sizeof(test_<stage>_tests) / sizeof(test_<stage>_tests[0]);
 ```

8. **Side-effect updates** (callee handles, but you list them in `// TODO:`
 comments at the very top):
 - `Makefile::PROD_SRCS` add `$(EXFAT)/<name>_<stage>.c`
 - `main.c` add `extern const struct CMUnitTest test_<stage>_tests[]; extern const size_t test_<stage>_tests_count;`
 - `main.c::main()` add `total += run_suite("<stage>", test_<stage>_tests, test_<stage>_tests_count);`

## Output format

Return the full C source file in a ```c ``` fenced block. Nothing else.
The file MUST compile with the Makefile in the harness — no missing headers,
no undefined symbols, no Linux-only APIs.

## Quality gate (LLM self-check before returning)

- [ ] Every spec Case has at least one testpoint (or is explicitly marked LAYER_B).
- [ ] Destructive helpers: tests observe real backing-state mutations (bitmap / FAT / on-disk dentry), not only post-hoc in-memory field resets.
- [ ] Negative-path tests assert specific errno (`-EINVAL` not `-1`).
- [ ] No bare `strcpy`/`memcpy` (use `memcpy_s` from securec.h stub).
- [ ] No global state shared between testpoints (use setup/teardown).
- [ ] License header present.
- [ ] Closes the array + size constant the harness `main.c` will reference.
