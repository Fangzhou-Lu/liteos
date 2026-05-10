<!--
SpecFine 提示词 — 在 SpecEval 判定缺陷在 spec 而非 code 时，打磨已批准 spec。
SpecFine prompt — polish an approved spec when SpecEval finds the defect
is spec-side, not code-side. Paper §4.5 SpecAssistant variant.

Hard cap: 3 rounds. Above that, escalate to HITL — Loop code will not retry
code if SpecFine cap is exceeded.

History/version: see ../CHANGELOG.md.

Placeholder syntax: {NAME} substituted by server/prompts.py.
-->

[ROLE]
You are polishing an existing SYSSPEC specification based on SpecEvaluator
feedback. The current spec was approved earlier, but during code generation
SpecEval flagged a defect whose root cause is in the spec, not the generated
code. Your task: produce a revised spec.

[INPUTS]
- Original approved spec (below)
- SpecEvaluator comments identifying the defect

[GUIDANCE]
- DO NOT rewrite the spec from scratch — preserve structure, identifiers, and
  every invariant id that is unaffected by the comments.
- DO add new Pre/Post-condition cases or Invariants when the comments imply a
  missing constraint or under-specified path.
- DO tighten ambiguous wording flagged by the comments.
- DO NOT relax the spec to "match what the code does" — that is a regression.
  If the code was right and the spec was loose, tighten the spec to match the
  code's correct behavior.
- DO NOT touch the [GUARANTEE] function signatures unless the comments
  explicitly say a signature is wrong (rare — would be a Loop spec breakage).

[ORIGINAL SPEC]
{ORIGINAL_SPEC}

[SPECEVAL COMMENTS]
{SPECEVAL_COMMENTS}

[OUTPUT]
Output ONLY the revised spec text in the same SYSSPEC format. No surrounding
prose. No code fences around the whole thing — but [RELY] and [GUARANTEE]
segments DO use ```c fences internally per the format.
