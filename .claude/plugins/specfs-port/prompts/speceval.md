<!--
Layer 3 — SpecEvaluator. Direct port of specfs/tools/gencode.py:200-211 with the
LiteOS-A style-audit dimensions folded in (formerly Layer S, removed in P1.1).

Single LLM round per code-gen retry. Output JSON: {"is_good": bool, "comments": str}.
The plugin auto-feeds `comments` back to codegen as [Modification suggestions]
when is_good=false. NO HITL gate — auto-feedback only, capped by the codegen
loop's own retry limit.

Placeholder syntax: {NAME} substituted by server/prompts.py at assemble time.
-->

This is a code generation validation task.

Check whether [Generated code] meets [Spec] AND conforms to LiteOS-A coding
conventions. If both hold, set `"is_good": true`. Otherwise set `"is_good": false`
and put concrete fix instructions in `"comments"`. Output JSON only.

# What to flag (be thorough and skeptical)

## Spec conformance
- Function signatures differ from [GUARANTEE]
- Pre/Post-conditions or Invariants violated
- Locking annotations missing or wrong relative to spec's `## Refine Prompt`
- Hallucinated helpers not in [RELY] and not in [PRIOR CODE INTERFACE]
- Branch / Case behavior diverges from [SPECIFICATION] cases
- Logic that satisfies post-condition but ignores **System Algorithm** phases

## LiteOS-A style (mirrors LITEOS_DIGEST in codegen prompt)
- Bare libc string/memory ops (`strcpy`/`strncpy`/`memcpy`/`memset`/`sprintf`/`snprintf`)
  instead of libsec `_s` variants
- `kmalloc` / `kzalloc` / `malloc` / `calloc` / `free` instead of
  `LOS_MemAlloc(m_aucSysMem0, ...)` / `zalloc` / `LOS_MemFree(m_aucSysMem0, ...)`
- Linux primitives left in: `printk` / `pr_err` / `mutex_lock` / `submit_bio` / `bread`
- Sleeping primitive called while holding spinlock
- VFS-callback returns POSITIVE errno (must be NEGATIVE: `-EINVAL`, `-ENOMEM`, etc.)
- Internal helper missing goto-stack reverse-LIFO cleanup pattern
- `los_disk_read` mixed with `los_part_read` (different namespaces) for partition-relative IO
- Missing `#ifdef LOSCFG_FS_<MODULE>` TU body wrap, or GPL Linux header pasted
- FS registration via `LOS_MODULE_INIT` instead of `FSMAP_ENTRY` linker table
- VFS callback name not `Vfs<Fs><Op>` PascalCase, or internal helper not `<fs>_<verb>_<noun>`
- Function body > 150 lines (suggest split via `comments`, do NOT rewrite)

# What NOT to flag

- Code comments / docstrings (only check code logic).
- Whitespace / clang-format-style issues (advisory only — fs/exfat/.clang-format is not a hard gate).
- Soft preferences with no rule violation.

# Output

```json
{
  "is_good": <bool>,
  "comments": "<actionable fix instructions; ordered by severity; reference line numbers when possible>"
}
```

If the code is clean, set `"is_good": true` and `"comments": ""`. Do not output any prose outside the JSON block.

[Generated code]
{GENERATED_CODE}

[Spec]
{ORIGINAL_SPEC}
