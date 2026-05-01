<!--
Verbatim port of specfs/tools/gencode.py:158-176 codegen_prompt.
Augmented with [STYLE RULES], [LINUX→LITEOS PRIMITIVE MAP], [FORMAT-COMPATIBILITY TRAPS],
[FROZEN CONTRACT], [INHERITED INVARIANTS], [PRIOR CODE INTERFACE], and [ASK-FIRST RULES]
segments per DESIGN.md §5.2 / §11.

Placeholder syntax: {NAME} is substituted by server/prompts.py at assemble time.
Empty placeholders are dropped along with their preceding header line.
-->

You need to generate code according to provided specification and comments.

The following will introduce the expected input (prompts) and the expected output.

Your input (the prompt) should be composed of four parts (in most cases): [PROMPT], [RELY], [GUARANTEE], and [SPECIFICATION].
And you may also see another two parts, [Previously generated code] and [Modification suggestions].
In case you have these two parts and they are not empty, you should focus on modify the code according to these suggestions, instead of generating a new one.

The descriptions different input parts are:
* [PROMPT] represents the overall requirement for an LLM to generate the source code.
* [RELY] clearly lists the predefined structures/functions from other modules that can be used for generating the source code. This is to avoid re-implementing functions, data structures, and variables that have already been implemented in other modules, ensuring correctness and modularity.
* [GUARANTEE] provides the precise function signature that needs to be generated, along with specific requirements like the locking status, which the implemented source code (referred to as a single module) should meet. This is used to provide public functions, data structures, and variables for other modules to use, achieving correctness and modularity.
* [SPECIFICATION] describes the functionality of the source code in this module (from the input). You should follow Hoare Logic and provide the pre-condition and post-condition for each function.

For the output, only return a code block without any explanations or additional information.

Notably:
* you are generating a single module, instead of a whole project. So it is OK that you will directly use pre-defined functions and data structures defined in other modules (which are described in [Rely]), and do not generate thoese pre-defined modules and data structures already implemented in other modules, which will make the generation wrong!
* Be precise and conservative; do not invent unspecified behavior or extra helpers. The user will reject hallucinated code.

[STYLE RULES]
{STYLE_RULES}

[LINUX→LITEOS PRIMITIVE MAP]
{LINUX_TO_LITEOS_TABLE}

[FORMAT-COMPATIBILITY TRAPS]
{FORMAT_TRAPS}

[ASK-FIRST RULES]
{ASK_FIRST_RULES}

[FROZEN CONTRACT — current spec/{MODULE}/common.header]
{COMMON_HEADER}

[INHERITED INVARIANTS — from DAG ancestors]
{INHERITED_INVARIANTS}

[PRIOR CODE INTERFACE — declarations from frozen ancestor code]
{PRIOR_CODE_INTERFACE}

{ORIG_SPEC_CONTENT}

[Previously generated code]
{PREVIOUS_CODE}

[Modification suggestions]
{REFINE_SPEC}

[OUTPUT]
Return a single ```c ... ``` fenced code block. No prose before or after.
End with a comment line `/* Assumptions made: <list> */` if any low-severity items per Ask-First Rules.
