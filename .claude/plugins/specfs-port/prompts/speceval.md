<!--
Verbatim port of specfs/tools/gencode.py:200-211 speceval_prompt.
Used by Layer 3 (SpecEvaluator) — default OFF in HITL mode; enable via
/specfs-port-code --speceval-on.

Placeholder syntax: {NAME} substituted by server/prompts.py at assemble time.
Output MUST be JSON: {"is_good": bool, "comments": str}.
-->

This is a code generation validation task.
Please check if the generated code (in [Generated code]) meets the specification ([Spec]). If it does not, provide suggestions for modifications.

If the [Generated code] can attain essentially the same outcomes as [Spec], you should put a boolean: true, in the `is_good` variable in output (in JSON); otherwise, put a boolean false.
In case the [Generated code] does not fully meet the specification, you should put detailed instructions in variable `comments` to modify the code to fit the spec. Only check the code, do not check the comments.
Your output should be in JSON format.

Be thorough and skeptical. Flag any deviation, including:
- Function signatures differ from [GUARANTEE]
- Locking annotations missing or wrong (mux vs spinlock vs none)
- Hallucinated helpers not in [RELY] and not in [PRIOR CODE INTERFACE]
- Missing libsec `_s` variants for string/memory ops
- Linux primitives not translated (kmalloc, mutex_lock, printk, etc.)
- Goto-stack errno style violated

[Generated code]
{GENERATED_CODE}

[Spec]
{ORIGINAL_SPEC}
