<!--
Loop B codegen prompt. Direct port of specfs/tools/gencode.py:158-176 +
LiteOS-A delta encoded as a compact LITEOS_DIGEST.

P1.6 (2026-05-08) slim: 84 → ~50 lines. Cut the verbose "Required (always
present)" / "Optional" enumeration — the LLM reads spec heading literals
directly and doesn't need an explanation paragraph for each of [PROMPT] /
[RELY] / [GUARANTEE] / [SPECIFICATION] / `## Refine Prompt`.

P1.7 (2026-05-08) integrate additive recommendations from Loop C (exfat):
  - Tombstone Invariants must evict path_cache / VfsHashRemove BEFORE
    releasing the lock that protects the tombstone (Reclaim at refcount=0
    is async and unsafe for blocking concurrent lookup).
  - Parent-inode metadata refresh (vnode dirty mark + parent
    exfat_inode_info timestamp update) is the caller's job on
    unlink-style mutations — emit between s_lock unlock and any deferred
    Phase-2 work.
  - Phase 2 lock-free w.r.t. a primary lock: SAMPLE inode-info fields
    (start_clu, flags) into stack locals BEFORE releasing the primary
    lock, and after free / unlink completes RESET those fields to their
    sentinel so a future Phase-2 evolve cannot re-run the operation.

Placeholder syntax: {NAME} substituted by server/prompts.py at assemble time.
Empty placeholders are dropped along with their preceding header line.
-->

Generate LiteOS-A C from a SYSSPEC spec.

Input segments (LLM should recognize by literal heading):
- `[PROMPT]` — destination file, header to include, output rule, intent.
- `[RELY]` — predefined types/functions; reference, do NOT redefine.
- `[GUARANTEE]` — signature(s) + calling-convention comment.
- `[SPECIFICATION]` — Pre/Post conditions, Invariants, optional **System Algorithm**.
- `## Refine Prompt` (optional) — additive locking constraints over Phase 1.
- `[Previously generated code]` (optional) — modify per suggestions; don't restart.
- `[Modification suggestions]` (optional) — SpecEvaluator feedback; apply faithfully.

Output:
- ONE ```c ... ``` fenced block. No prose outside.
- Conservative: do not invent unspecified behavior or extra helpers.
- If you took an assumption per LITEOS DIGEST, end with `/* Assumptions made: <list> */`.

Concurrency / mutation rules (apply when the spec triggers them):
- Tombstone Invariant set under a lock → evict the target Vnode from
  path_cache (`VnodePathCacheFree`) and `VfsHashRemove` BEFORE that lock
  is released. Do NOT defer to Reclaim — Reclaim runs at refcount=0 and
  is not synchronous with unlink, so concurrent lookups can still hit
  the doomed vnode.
- Unlink-style parent-inode refresh: between unlocking the primary
  s_lock and any deferred / Phase-2 work, mark the parent vnode dirty
  and update parent `exfat_inode_info` timestamps (mtime / ctime /
  iversion-equivalent). The Linux caller does this implicitly; LiteOS-A
  port must do it explicitly.
- Phase 2 declared lock-free w.r.t. a primary lock but reading inode-info
  fields (e.g. `start_clu`, `flags`):
  1. SAMPLE those fields into stack locals BEFORE releasing the primary lock.
  2. Use only the stack locals in Phase 2.
  3. After free / unlink completes, RESET the original inode-info fields
     to their sentinel value (e.g. `EXFAT_EOF_CLUSTER`, zero flags) so a
     subsequent Phase-2 evolve cannot re-run the operation.

[LITEOS-A DIGEST — non-negotiable rules the spec does not restate]
{LITEOS_DIGEST}

[FRAGMENT INDEX — fetch on demand, NOT reflexively]
Pull only when uncertainty matches:

| id | size | when to fetch |
|---|---|---|
| `style_rules` | ~7K | naming / layout / license / file-organization unfamiliar |
| `linux_to_liteos_table` | ~7K | spec [RELY] mentions Linux primitive (kmalloc / mutex / page / dentry / bio / RCU / bh / jbd2 / fscrypt / kmem_cache) and DIGEST gives no obvious LiteOS map |
| `format_traps` | ~4K | on-disk CRC / byte-order / charset / packed structs with multi-byte fields |
| `ask_first_rules` | ~4K | mid-generation spec ambiguity (rare; prefer surfacing in `Assumptions made:`) |
| `two_phase_rules` | ~5K | borderline Phase 1/2 question (e.g. `LOS_MuxInit` placement) |

Tool: `specfs.fetch_prompt_fragment(name="<id>")`. After fetch, if still
unresolved, end output with `/* Assumptions made: <list> */`. Never silently guess.

[FROZEN CONTRACT — current spec/{MODULE}/common.header]
{COMMON_HEADER}

[INHERITED INVARIANTS — lazy]
{INHERITED_INVARIANTS}
On demand: `specfs.dag_extract_invariants(module="{MODULE}", node_id="<ancestor-id>")`.

[PRIOR CODE INTERFACE — declarations from frozen ancestor code]
{PRIOR_CODE_INTERFACE}

{ORIG_SPEC_CONTENT}

[Modification suggestions]
{REFINE_SPEC}
