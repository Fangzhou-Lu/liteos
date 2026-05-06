<!--
Loop B codegen prompt. Direct port of specfs/tools/gencode.py:158-176 with the
LiteOS-A delta encoded as a single compact {LITEOS_DIGEST} (~1.7K) instead of
four full fragments (style_rules + linux_to_liteos_table + format_traps +
ask_first_rules ≈ 22K). Detailed fragments are lazy-injected by the plugin
when a retry round's diagnostics match a specific class — the default code-gen
prompt stays small.

Placeholder syntax: {NAME} substituted by server/prompts.py at assemble time.
Empty placeholders are dropped along with their preceding header line.
-->

You need to generate code according to the provided specification.

Your input has up to four required parts and three optional parts.

Required (always present):
* `[PROMPT]` — overall requirement, names the file you must produce, the only
  header to include, and the output wrapping rule.
* `[RELY]` — predefined types/functions/variables from other modules. DO NOT
  re-implement these; reference them as-is.
* `[GUARANTEE]` — the precise function signature(s) you must produce, plus a
  calling-convention comment block (held locks, return values, side effects).
* `[SPECIFICATION]` — pre/post conditions (Hoare logic), invariants, and an
  optional **System Algorithm** sub-block. Implement to satisfy this spec.

Optional:
* `## Refine Prompt` — second phase covering locking discipline and per-helper
  pre/post under lock state. When present, treat as additive constraints over
  the first phase, not replacement.
* `[Previously generated code]` — output from a prior round. Modify it to
  satisfy the suggestions; do NOT regenerate from scratch.
* `[Modification suggestions]` — SpecEvaluator feedback. Apply faithfully.

Output rules:
* Return a single ```c ... ``` fenced code block.
* No prose before or after the block.
* Be precise and conservative — do not invent unspecified behavior or extra
  helpers. The user will reject hallucinated code.
* You are generating ONE module. It is fine (and expected) to call the
  predefined symbols listed in [RELY] without redefining them.
* If you took a low-severity assumption per the LITEOS DIGEST below, end the
  block with `/* Assumptions made: <list> */`.

[LITEOS-A DIGEST — non-negotiable rules the spec does not restate]
{LITEOS_DIGEST}

[FRAGMENT INDEX — LLM-driven, on-demand expansion]
The DIGEST above is intentionally compact (~1.7K). Four detailed reference
fragments are available. Decide for yourself whether you need them, and pull
the ones you need by calling `specfs.fetch_prompt_fragment(name="<id>")`
BEFORE emitting code. Do NOT pull all four reflexively — only those whose
hints match your concrete uncertainty about THIS spec.

| id | size | when to fetch |
|---|---|---|
| `style_rules` | ~7K | Naming/layout/license header/error-path/file-generation-order are unfamiliar; you are about to invent a helper file or pick a function-name convention. |
| `linux_to_liteos_table` | ~7K | The spec [RELY] mentions a Linux primitive (kmalloc, mutex_lock, submit_bio, page, dentry, inode_operations, address_space_operations, bio, RCU, bh, jbd2, fscrypt, kmem_cache) and the digest does not give an obvious LiteOS-A equivalent. |
| `format_traps` | ~4K | The spec touches on-disk data with checksums (CRC), byte-order conversions, charset (UTF-16LE / GBK), or `__attribute__((packed))` structs with multi-byte fields. |
| `ask_first_rules` | ~4K | This is a CODE-stage prompt; ask-first normally lives in the spec stage. Fetch only if mid-generation you encounter genuine spec ambiguity that the spec author did not resolve. Prefer raising the ambiguity in your output's `Assumptions made:` line over fetching this. |

If after fetching a fragment you still cannot resolve the question, end your
output with `/* Assumptions made: <list> */` and surface the unresolved item.
Never silently guess.

[FROZEN CONTRACT — current spec/{MODULE}/common.header]
{COMMON_HEADER}

[INHERITED INVARIANTS — lazy, on-demand]
Available via tool call. If a [RELY] reference or [SPECIFICATION] case appears
to depend on an ancestor stage's invariant, call
`specfs.dag_extract_invariants(module="{MODULE}", node_id="<ancestor-id>")`
to retrieve it. Do NOT speculate ancestor invariants — fetch them or omit.

[PRIOR CODE INTERFACE — declarations from frozen ancestor code]
{PRIOR_CODE_INTERFACE}

{ORIG_SPEC_CONTENT}

[Previously generated code]
{PREVIOUS_CODE}

[Modification suggestions]
{REFINE_SPEC}
