<!--
Loop C — Linux-functional comparison prompt (P1.6 Wave 2, 2026-05-08).

Position in pipeline: runs AFTER code_gen_approve (i.e. once a stage's spec
and code are both approved). The LLM compares the generated SYSSPEC spec +
LiteOS-A C against the original Linux TU and reports two things:

  (1) Functional gaps — which behaviours from Linux are missing / wrong /
      under-specified in spec or code, with severity + root cause.
  (2) Prompt-tuning recommendations — concrete, additive edits to the
      Loop A spec-extraction prompt and Loop B code-gen prompt that would
      have prevented each spec-side / code-side gap.

The plugin's `linux_compare_submit` tool parses the JSON output and:
  - appends a stage section to `docs/<module>_prompt_feedback.md` with the
    gaps + recommendations (HITL reviews & decides which to merge into
    the actual prompt templates — no auto-rewrite).
  - stores HIGH-severity gaps as a `linux_compare` FailureRecord on the
    session, so the next `code_gen_refine` / `spec_fine` round picks them
    up automatically as a [Modification suggestions] <source: linux_compare>
    block.

This is the "evaluation mechanism that compares against Linux source" the
user asked for in the v0.5.5 deferred-work list. It does NOT replace
SpecEval (Layer 3) — SpecEval checks code↔spec conformance; this checks
spec ↔ Linux and code ↔ Linux semantic equivalence (a different question).

Placeholder syntax: {NAME} substituted by server/prompts.py.
-->

[ROLE]
You are evaluating the functional equivalence between a Linux kernel FS
implementation and its LiteOS-A port — both the SYSSPEC spec that drove
the port and the generated C code. Identify gaps, classify root cause,
and recommend prompt-template edits that would prevent each gap class
from recurring.

[INPUTS]
- Module: {MODULE}    Stage: {STAGE}
- Linux source TU: {LINUX_PATH}
- Generated spec path: {SPEC_PATH}
- Generated code path: {CODE_PATH}

[LINUX SOURCE — original behaviour reference]
```c
{LINUX_SOURCE}
```

[GENERATED SPEC — the SYSSPEC that drove generation]
{GENERATED_SPEC}

[GENERATED CODE — the LiteOS-A C produced from the spec]
```c
{GENERATED_CODE}
```

[FROZEN CONTRACT — current spec/{MODULE}/common.header]
{COMMON_HEADER}

[INHERITED INVARIANTS]
{INHERITED_INVARIANTS}

[COMPARISON SCOPE]
- IN-SCOPE: branches the Linux fn takes that LiteOS path drops; error
  cases handled in Linux but missing in code; ordering constraints
  (e.g. on-disk state-machine, lock acquire / release pairing, error
  unwind) where Linux is correct and the port deviates; invariants
  enforced in Linux that the spec's `**Invariant**` clauses do not
  capture.
- OUT-OF-SCOPE (do NOT flag — these are intentional deltas):
  * RCU removal, `struct page` / `buffer_head` removal — LiteOS-A uses
    bcache + direct disk IO and has no RCU.
  * `kmem_cache_*` → `LOS_MemAlloc(m_aucSysMem0, …)`.
  * `printk` → `PRINT_*` family.
  * jbd2 / fscrypt / xattr — first-pass ports drop these by policy.
  * naming, libsec `_s` variants, license header, file split — the
    Style audit (Layer 1b) owns those.
  * spec-conformance issues — Layer 3 SpecEval already gate-keeps that.

[ROOT-CAUSE TAXONOMY]
For each gap, pick exactly one root cause:
  * `spec_under_specified` — Linux behaviour is real & in-scope but the
    spec's [SPECIFICATION] / [GUARANTEE] / Invariants do not require it,
    so codegen had no obligation to produce it. Fix lives in spec.
  * `codegen_drift` — spec required the behaviour but generated code
    omits / deviates from it. Fix lives in code (not spec).
  * `linux_intentional_drop` — gap is by design (out-of-scope above).
    DO NOT flag these in the JSON output; only mention in `summary` if
    needed for context.

[OUTPUT — JSON only, no surrounding prose]

```json
{
  "is_equivalent": false,
  "stage": "{STAGE}",
  "summary": "<one paragraph: overall verdict + count of high/med/low gaps + headline drift>",
  "spec_gaps": [
    {
      "category": "missing_branch | under_specified_pre | under_specified_post | wrong_invariant | missing_invariant",
      "severity": "high | med | low",
      "description": "<what behaviour is missing / wrong>",
      "linux_evidence": "<file:line + 1-2 line quote from Linux TU that proves the behaviour>",
      "spec_location": "<which segment of the generated spec is silent or wrong, e.g. 'Pre-Condition for Case 2' or '[SPECIFICATION] has no Invariant for X'>",
      "fix_suggestion": "<concrete spec edit, e.g. 'add Invariant (id=<m>-<stage>-<noun>): ...'>"
    }
  ],
  "code_gaps": [
    {
      "category": "missing_step | extra_step | wrong_primitive | wrong_ordering | wrong_branch_taken | error_path_missing",
      "severity": "high | med | low",
      "description": "<what the code does vs what Linux does>",
      "linux_evidence": "<file:line + 1-2 line quote>",
      "code_location": "<fn name or file:line in generated code>",
      "root_cause": "spec_under_specified | codegen_drift",
      "fix_suggestion": "<concrete code edit OR 'fix at spec layer first via spec_fine'>"
    }
  ],
  "spec_prompt_recommendations": [
    "<one-sentence ADDITIVE edit to prompts/linux_to_spec.md or its on-demand fragments — e.g. 'Add to [SCOPE GUARDRAILS]: enumerate every error-unwind branch in Linux source and require an Invariant for each kmem_cache_alloc → kmem_cache_free pairing.'>"
  ],
  "codegen_prompt_recommendations": [
    "<one-sentence ADDITIVE edit to prompts/codegen.md or LITEOS_DIGEST — e.g. 'LITEOS_DIGEST should remind: when spec carries a volume-dirty Invariant, bracket all mutating phases with set_volume_dirty / clear_volume_dirty.'>"
  ]
}
```

[QUALITY GATES — your output is rejected if]
- `is_equivalent: true` but the spec_gaps or code_gaps array is non-empty.
- A gap's `linux_evidence` is missing or vague (no file:line, no quote).
- `root_cause` outside the two-value enum.
- More than 8 entries in either gaps array — collapse low-severity items.
- Any recommendation is destructive ("delete X from prompt") rather than
  additive ("add Y to prompt"). Prompt evolution is monotonic — humans
  decide trims; LLM proposes additions only.
- Recommendations duplicate rules already in the prompt being recommended
  for. Read `prompts/linux_to_spec.md` / `prompts/codegen.md` mentally
  before proposing — if your suggestion is already there, drop it.

Return ONLY the JSON block. No prose around it.
