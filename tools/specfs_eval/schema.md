# specfs-port plugin 评估指标 schema

衡量"优化插件流程是否在保留质量的前提下降低了时间与 token 消耗"的最小指标集。
每个指标标注 (类别 / 采集源 / 用途)。

## 类别

- **A** = 输入指标（prompt 侧）：衡量优化是否压缩了输入侧
- **B** = 产出指标（artifact 侧）：衡量优化是否保住质量
- **C** = 流程指标（运行时侧）：衡量优化是否压缩了时间/token

## 指标清单

### A. 输入指标

| 指标 | 采集源 | 用途 |
|---|---|---|
| `prompt_chars` | `spec_gen_start.prompt_for_llm` 长度 | 总体规模 |
| `prompt_section_chars` | 解析 prompt `[SECTION]` 头部分段统计 | 哪段冗余 |
| `inherited_invariants_count` | `dag.extract_invariants(stage)` 返回长度 | 祖先约束注入是否有效 |
| `common_header_used_ratio` | spec [RELY] 引用的 extern 数 / common.header 总 extern 数 | header 是否过度 |

### B. 产出指标

| 指标 | 采集源 | 用途 |
|---|---|---|
| `spec_loc` | `wc -l <spec_file>` | 规模 |
| `spec_invariants` | `grep -c '^**Invariant**' <spec_file>` | 约束密度 |
| `spec_segments` | 是否含 `[PROMPT]` `[RELY]` `[GUARANTEE]` `[SPECIFICATION]` 4 段 | 结构合规 |
| `spec_refine_prompts` | `grep -c '^## Refine Prompt' <spec_file>` | 锁/失败语义独立轮次数 |
| `code_loc` | git diff stat per stage commit | 实现规模 |
| `code_files` | git commit changed files in fs/exfat | 落点散度 |
| `function_count` | `grep -c '^[a-z].*(' <code_file>` 或解析符号表 | 函数数量 |
| `spec_code_ratio` | `spec_loc / code_loc` | 论文核心命题 < 1.0 |
| `testpoint_count` | `grep -c 'cmocka_unit_test' <test_file>` | 测试覆盖 |
| `agent_flagged_decisions` | commit body 中"agent 派生 N 个补充决策" / agent 报告 flagged 项 | 自动化失败率 |
| `ask_first_questions` | spec PROMPT / commit body "ask-first 决策矩阵" Q 计数 | 用户决策点 |

### C. 流程指标

| 指标 | 采集源 | 用途 |
|---|---|---|
| `wall_min` | git log 相邻 stage commit 时间戳差（粗估） | 端到端时间 |
| `total_tokens` | agent task notification `usage.total_tokens`（仅当走 Task() 派工时可用） | LLM 成本 |
| `agent_tool_uses` | agent task notification `usage.tool_uses` | 工具调用密度 |
| `speceval_rounds` | DAG node `spec.approval_iterations` | SpecEval 实际迭代 |
| `cmocka_pass` | 远端 `make` 退出码 + `=== exfat TOTAL FAILURES: 0 ===` | 硬质量门 |
| `kernel_build_pass` | 远端 `build.sh` 退出码 + liteos.bin 是否产出 | 硬质量门 |
| `qemu_smoke_pass` | `tools/regress/run_all.sh` 退出码 | 端到端质量门（可选） |

## 采集时机

- **回填基线**：对已落档的 26 个 stage 一次性扫描 git + 文件系统，输出 `baseline_metrics.json`。
- **新 stage 实测**：spec_gen_approve 后立即采一次 A+B 部分；code_gen_approve 后采一次 B+C 部分；最终归档为 `metrics_<stage>.json`。
- **A/B 对比**：同一 target_stage 跑老/新 prompt，分别采集，写 `ab_<stage>_<variant>.json`。

## 输出文件

```
tools/specfs_eval/
├── schema.md                  本文件
├── AB_PROTOCOL.md             A/B 测试协议（4 门标准）
├── collect.py                 采集器
├── baseline.py                批量回填基线 driver
├── baseline_metrics.json      26 个已完成 stage 的基线
└── runs/
    └── <stage>_<variant>.json A/B 测试单次运行记录
```

## 缺失信号说明

- `total_tokens` / `agent_tool_uses`：历史 stage 没记录，基线该字段 `null`；只对 Step 2 起的新 stage 有效。
- `speceval_rounds`：plugin v0.3.4 才在 DAG 写入；早期 stage 字段缺失。
- `wall_min`：基于 git commit 时间戳粗估，含 HITL 等待时间，与"agent 真实运行时间"有差异；A/B 对比时建议结合 task notification `usage.duration_ms`。
