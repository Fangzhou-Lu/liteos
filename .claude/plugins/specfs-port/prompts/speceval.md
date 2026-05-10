<!--
Step 4 spec conformance 提示词 — Loop code Step 4 spec/code audit 的 spec conformance 子提示词。
Step 4 spec-conformance check — used inside Loop code's merged spec/code audit.

Scope: SPEC CONFORMANCE ONLY. Style audit is Step 2.2 (see prompts/style_audit.md
against prompts/style_rules.md); kernel build is Step 2.3. Do NOT inline or
reference style rules here. Linux equivalence checks belong to the heterogeneous
audit half of Step 4 (see prompts/heterogeneous_audit.md mode=code_audit).

Single LLM round per code-gen retry. Output JSON: {"is_good": bool, "comments": str}.
The plugin auto-feeds `comments` back to codegen as [Modification suggestions]
when is_good=false. NO HITL gate — auto-feedback only, capped by the codegen
loop's own retry limit.

History/version: see ../CHANGELOG.md.

Placeholder syntax: {NAME} substituted by server/prompts.py at assemble time.
Required placeholders: {GENERATED_CODE}, {ORIGINAL_SPEC}.
-->

This is a spec-conformance validation task.

Check whether [Generated code] meets [Spec]. If yes, set `"is_good": true`.
Otherwise set `"is_good": false` and put concrete fix instructions in
`"comments"`. Output JSON only.

Style / convention violations are out of scope here — a separate Style audit
pass owns those. Focus exclusively on the items below.

# What to flag (be thorough and skeptical)

## Spec conformance — the only bucket
- Function signatures differ from [GUARANTEE]
- Pre/Post-conditions or Invariants violated
- Locking annotations missing or wrong relative to spec's `## Refine Prompt`
- Hallucinated helpers not in [RELY] and not in [PRIOR CODE INTERFACE]
- Branch / Case behavior diverges from [SPECIFICATION] cases
- Logic that satisfies post-condition but ignores **System Algorithm** phases
- **Phase-layering mismatch**: code uses lock-acquisition primitives
  (`LOS_MuxLock` / `LOS_MuxUnlock` / `LOS_SpinLock` / `LOS_SpinUnlock` and
  variants) but the spec lacks a `## Refine Prompt` section that anchors
  the expected lock state. Treat this as a SPEC GAP, not a code defect:
  set `"is_good": false` and write `comments` like
  `"phase_layering_violation: code acquires <X> but spec has no Phase 2.
   Either spec should be SpecFine'd to add Phase 2 lock-state contract, or
   code should remove the lock acquisition. Recommend: spec polish."`
  This routes to F3 SpecFine instead of code regen.
- **Phase-1 lock leakage in spec → wrong code expectation**: if spec's
  Phase 1 [SPECIFICATION] mentions held locks (e.g. "caller holds X") but
  there is no `## Refine Prompt`, the spec is malformed. Flag with
  `"phase_layering_violation: spec leaks lock state into Phase 1 with no
   Phase 2; ambiguous lock contract. Recommend: spec polish."`

# What NOT to flag here

- Naming, libsec usage, allocation primitives, locking primitives, error-code
  sign, FS-registration mechanism, license header, file-organisation rules.
  These are checked by the Style audit layer (prompts/style_audit.md) using
  the canon in prompts/style_rules.md. Do NOT duplicate that logic in this
  prompt; if you flag a style issue here it will be a false positive.
- Code comments / docstrings (only check code logic).
- Whitespace / clang-format-style issues.
- Soft preferences with no rule violation in [Spec].

# Output

```json
{
  "is_good": <bool>,
  "comments": "<actionable fix instructions; ordered by severity; reference line numbers when possible>"
}
```

If the code is clean, set `"is_good": true` and `"comments": ""`. Do not
output any prose outside the JSON block.

[Generated code]
{GENERATED_CODE}

[Spec]
{ORIGINAL_SPEC}
