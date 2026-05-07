<!--
v0.3.2 起：从已批准的 spec 派生 cmocka 单元测试草稿。
v0.3.4 起：调用时机迁移到 Loop A 的 `spec_gen_approve` 之后，与 Loop B
代码生成并行；不再耦合 Layer S / Layer 3 的通过条件。
P1.2 (2026-05-07) 拓扑：style 审计回归为 Layer 1 sibling，但与本 prompt
无依赖（test_gen 只看已批准 spec + 生成的代码 + harness 骨架）。

输入：原 spec（含 [SPECIFICATION] 各 Case）+ 刚生成的代码 + 已存在的
testsuites/unittest/<name>_host/ 目录骨架。

输出：单文件 `test_<stage>.c`，cmocka 风格，含 setup/teardown + 每个 Case
对应至少一个测点（happy + 各 -EXXX 负向）。
-->

This is a cmocka unit-test synthesis task for a freshly generated LiteOS-A FS
helper. Produce ONE C source file: `test_<stage>.c` covering every Case in the
spec's `[SPECIFICATION]` section.

[Generated code under test]
{GENERATED_CODE}

[Spec]
{ORIGINAL_SPEC}

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
- [ ] Negative-path tests assert specific errno (`-EINVAL` not `-1`).
- [ ] No bare `strcpy`/`memcpy` (use `memcpy_s` from securec.h stub).
- [ ] No global state shared between testpoints (use setup/teardown).
- [ ] License header present.
- [ ] Closes the array + size constant the harness `main.c` will reference.
