<!--
Loop C — prompt-template optimisation meta-prompt (P1.6 Wave 2, 2026-05-08).

Triggered by `prompt_optimize_propose`. Reads accumulated Loop C
recommendations from docs/<module>_prompt_feedback.md and asks the LLM to
roll them up into a concrete edit of a TARGET prompt template
(linux_to_spec.md, codegen.md, or one of the on-demand fragments).

Hard constraints baked in:
  - Output is the FULL revised prompt text, not a diff. Server diffs vs
    current to surface the change to HITL.
  - Edits must be ADDITIVE (paper §"Sharpen the Spec" methodology — the
    prompt evolves monotonically, with humans doing trim passes manually).
  - Recommendations that duplicate rules already in the template must be
    dropped silently — the LLM is the de-duplication layer.
  - Recommendations whose merge would require restructuring sections (e.g.
    splitting [SCOPE GUARDRAILS] into two) MUST be flagged with a comment
    `# RESTRUCTURE NEEDED:` instead of executed — restructuring is a HITL
    decision.

Placeholder syntax: {NAME} substituted by server/prompts.py.
-->

[ROLE]
Optimise a SYSSPEC-port prompt template by integrating accumulated
Loop C recommendations from completed stages.

[INPUTS]
- Target prompt: {TARGET_PROMPT_NAME}.md
- Module the recommendations came from: {MODULE}
- Recommendation type filter: {REC_TYPE}    (spec | codegen)
- Number of stages whose feedback was rolled up: {N_STAGES}

[CURRENT PROMPT TEMPLATE — verbatim contents of prompts/{TARGET_PROMPT_NAME}.md]

```markdown
{CURRENT_PROMPT}
```

[ACCUMULATED RECOMMENDATIONS — extracted from docs/{MODULE}_prompt_feedback.md]

These were produced by the linux_compare LLM round across {N_STAGES}
completed stages. Each line is one additive recommendation. Some may
duplicate rules already in the template — drop those. Some may overlap
each other — merge them.

```
{RECOMMENDATIONS}
```

[OUTPUT FORMAT]

Return ONLY the revised full prompt template text, in the SAME format as
the current template (markdown with the leading `<!-- ... -->` developer
comment block and the `[SECTION]` / `## Heading` skeleton preserved).

Do NOT wrap the output in code fences. Do NOT prefix with "Here is the
revised prompt:" or any other prose.

Bump the version line in the leading comment block. Convention: append a
new dated line like:
  P1.7 (2026-05-08) integrate {N} additive recommendations from Loop C:
    - <one-line summary of recommendation 1>
    - <one-line summary of recommendation 2>
    ...

[QUALITY GATES — your output is rejected if]
- Any section header from the current template is missing in the output
  (additive only; no deletions allowed).
- Total length grew by more than 30% — that signals you copied verbose
  recommendation text instead of distilling it to imperative rules.
- A recommendation was duplicated verbatim alongside an existing rule
  it overlaps with.
- The leading developer-comment block is missing or its dated history was
  rewritten.
- Any line `# RESTRUCTURE NEEDED: ...` is silently merged instead of
  preserved as a TODO comment for the human reviewer.
- You inserted prose outside the documented prompt structure. Stay in the
  template's own grammar.

Begin the revised prompt now.
