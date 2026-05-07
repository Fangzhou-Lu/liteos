<!--
LiteOS-A coding-style audit (added in plugin v0.3 per user "加入编码风格
评估环节"). Topology evolution: Layer S (v0.3 serial after compile) →
Layer 1b (P1.2 sibling of compile, parallel) → Layer 1a.2 (P1.4 sequential
sub-step inside Layer 1a, runs only AFTER 1a.1 LSP is clean).

Loaded as {STYLE_AUDIT} placeholder via prompts.py::assemble_style_audit_prompt.
Output MUST be JSON: {"is_good": bool, "score": int (0..100), "comments": str, "violations": [...]}.

Output is parsed by specfs_server.py and either:
 - is_good=true & score >= 80 → advance to Layer 3 SpecEval (P1.4 promoted
   ahead of Layer 2 build/QEMU)
 - otherwise → inject violations as [Modification suggestions] source=style and
 loop back to codegen (max 5 rounds — independent of LSP retry budget).

Companion runtime checks (run BEFORE this prompt is shown to the LLM):
 - clang-format --dry-run against fs/<module>/.clang-format anchor — captured
 as an ADVISORY hint (the .clang-format anchor is advisory, not a hard gate;
 LiteOS-A's manual whitespace alignment in fs/fat/* and fs/jffs2/* cannot be
 cleanly captured by clang-format options — see fs/exfat/.clang-format
 header comment). The diff goes into [AUTO CHECKS] for the LLM to consider.
 - simple regex scan for bare strcpy / strncpy / memcpy / sprintf
 - function-length / cyclomatic-complexity heuristic
The runtime fills the "[AUTO CHECKS]" section before the LLM judges. The LLM
weighs the auto checks alongside its own 6-dimension scoring; do NOT treat any
single auto-check as authoritative.
-->

This is a coding-style audit task for LiteOS-A kernel filesystem code.

The generated code MUST conform to LiteOS-A conventions. Evaluate the code in
[Generated code] against the [LiteOS-A style rules] below across **6 dimensions**.

If the code conforms (`is_good=true`, score ≥ 80), set both fields.
Otherwise, return `is_good=false` with an itemised `violations` list — each
violation must name the dimension, line range, and the rule it breaks.
**Do not propose unrelated rewrites — only flag actual rule violations.**

[Auto checks already performed]
{AUTO_CHECKS}

[LiteOS-A style rules]

## Dimension 1 — Naming
- VFS callbacks (members of `g_<fs>Vops` / `g_<fs>Fops`): `Vfs<Fs><Op>` PascalCase.
 Examples: `VfsExfatMount`, `VfsExfatLookup`, `VfsExfatRead`.
- Internal kernel API surface (rare, only if exported): `LOS_Xxx` for functions,
 `LosXxx` for types, `LOS_ERRNO_*` for errors.
- FS-private static helpers: `<fs>_<verb>_<noun>` lowercase_snake. The `<fs>`
 prefix is mandatory and must match the directory name.
 Examples: `exfat_parse_boot_sector`, `exfat_load_bitmap`, `exfat_calc_chksum32`.
- On-disk struct types: keep upstream names verbatim — `struct exfat_dentry`,
 `struct exfat_boot_sector`. (Upstream = Linux fs/<name>/<name>_raw.h.)
- File-scope globals: `g_<fs><Thing>` for tables (e.g., `g_exfatVops`),
 `g_<fs>_<thing>` for caches/ buffers.
- Local variables: lowercase_snake, no Hungarian, no single-letter except `i j k n m`
 in tight loops. Reject `pTr`, `iCount`, `lpFoo`.
- Macros: ALL_CAPS_SNAKE; FS-specific macros prefix with `<FS>_`
 (e.g., `EXFAT_DEFAULT_BLOCKSIZE`).

## Dimension 2 — Function complexity & length
- Cyclomatic complexity SHOULD be ≤ 15 per function. > 25 is a hard violation.
 (Counted as 1 + #if + #else if + #for + #while + #case + #&& + #||.)
- Function body SHOULD be ≤ 100 lines. > 150 is a hard violation, suggest split.
- Each function SHOULD do one thing — if violations are concentrated in one giant
 function, suggest extracting helpers (don't write them yourself; flag only).

## Dimension 3 — Code layout
- 4-space indent. **Tabs are forbidden** in any file under fs/<module>/.
- K&R braces: opening brace on same line for control flow (`if (x) {`), on the
 next line for functions:
 ```
 int VfsExfatMount(struct Mount *mount, struct Vnode *blk, const void *data)
 {
 ...
 }
 ```
- Always brace single-statement bodies (`if (foo) { return -EINVAL; }`).
 Bare `if (foo) return ...;` is a violation — diff-noise hostility.
- Pointer asterisk binds to the variable: `int *p`, not `int* p` or `int * p`.
- Header guard: `_EXFAT_H`, `_EXFAT_RAW_H`, etc. Public headers MUST also wrap
 the body in `#ifdef LOSCFG_FS_<NAME>` for symbol invisibility when disabled.
- License header: BSD-3-Clause Huawei Device template (see fs/fat/os_adapt/fatfs.c
 line 1-27 for the canonical text). GPL headers from Linux are forbidden.

## Dimension 4 — Memory & sec-coding
- Every kernel-heap allocation: `LOS_MemAlloc(m_aucSysMem0, sz)` or `zalloc(sz)`.
 Pairs with `LOS_MemFree(m_aucSysMem0, ptr)`.
- All `strcpy` / `strncpy` / `memcpy` / `sprintf` MUST be `_s` libsec variants.
 Bare libc variants (no _s) are hard violations — Huawei security-coding policy.
- All file-scope buffers explicitly initialised; no implicit-zero reliance for
 pointers (`uint8_t *p = NULL` not `uint8_t *p`).

## Dimension 5 — Locking
- May-sleep paths use `LOS_MuxLock`/`LOS_MuxUnlock`.
- Never-sleep / IRQ-safe paths use `LOS_SpinLock`/`LOS_SpinUnlock`.
- **Never** sleep while holding a spinlock. Calling LOS_MemAlloc inside a
 spin-locked region is a hard violation.

## Dimension 6 — Error path style
- VFS-boundary returns: NEGATIVE POSIX errno (`-EINVAL`, `-ENOMEM`, `-EROFS`).
 Internal helpers MAY use positive errno + final `return -ret;`.
- Goto-stack rollback: error labels in reverse-LIFO of resource acquisition.
 Label naming: `err_<resource>` (e.g., `err_boot_alloc`, `err_part_name`).
- No `panic()` / `BUG()` / `WARN_ON()` (Linux primitives) — use `PRINT_ERR` +
 graceful errno return.

[Generated code]
{GENERATED_CODE}

---

Output JSON only, in this exact shape:

```json
{
    "is_good": <bool — true if score ≥ 80 AND no hard violations>,
    "score": <int 0..100 — weighted across the 6 dimensions>,
    "summary": "<one-line digest of the verdict>",
    "violations": [
    {
    "dimension": "<naming|complexity|layout|memory|locking|error_path>",
    "severity": "<hard|soft>",
    "line_range": "<L1-L2 or single line>",
    "rule": "<which rule was broken — quote rule wording>",
    "fix": "<concrete remediation, ≤ 30 words>"
    }
    ]
}
```

If the code is clean, return `"violations": []` and a one-line summary in
`summary`. Do not output any prose outside the JSON block.
