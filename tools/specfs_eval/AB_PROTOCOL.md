# A/B 测试协议（specfs-port 优化合入门）

每条 prompt / plugin 优化合入主分支前必须通过本协议。设计原则：**自动化优先，
量化决策；当所有硬门通过且至少 1 个改进信号 ≥ 阈值时方可合入**。

## 0. 选 baseline stage

- 首选：与待测 stage 同类的已完成 stage（如 mkdir 优化时基线 = mkdir 实测）
- 次选：最近 3 个完成 stage 的中位数
- 当待测 stage 是首例（无同类基线）时：以 Wave A `lookup` 为通用基线（最早稳定 stage）

## 1. 四道门（必须全过）

| # | 门 | 失败动作 |
|---|---|---|
| 1 | **cmocka 通过** — 新 prompt 跑出的 code 跑全套 cmocka，0 failure | 否决，回滚 |
| 2 | **kernel build 通过** — OHOS clang `build.sh` 退出 0，OHOS_Image 产出 | 否决，回滚 |
| 3 | **invariant 保真** — baseline spec 的 invariant 名字集合 ⊆ 优化后 spec 的 invariant 名字集合（允许新增不允许丢失） | 否决，回滚（漏 invariant = 行为退化） |
| 4 | **覆盖保真** — `testpoint_count_optimized ≥ baseline × 0.8` | 否决，回滚 |

## 2. 改进信号（至少 1 项 ≥ 阈值方可合入）

| 信号 | 阈值 | 说明 |
|---|---|---|
| `prompt_chars` 减少 | ≥ 20% | A 类指标，主要看 token 节省 |
| `wall_min` 减少 | ≥ 20% | C 类指标，端到端时间 |
| `agent_flagged_decisions` 减少 | ≥ 50%（绝对值不超过 1） | B 类指标，自动化提升 |
| `ask_first_questions` 减少 | ≥ 50%（绝对值不超过 1） | B 类指标，人工决策减少 |
| `speceval_rounds` 减少 | ≥ 1 轮 | C 类指标，SpecEval 早收敛 |
| `total_tokens` 减少 | ≥ 25% | C 类指标（agent 实测时可用） |

## 3. 中性信号（监测但不门控）

- `spec_loc` / `code_loc`：可能因优化结构调整有变动，不做硬约束
- `spec_code_ratio`：监测论文命题；本仓常态 ≥ 1.4，优化目标是别让它继续涨

## 4. 流程

```
1. 选 baseline stage。运行 collect.py <baseline> 得 baseline.json。
2. 应用待测优化（修改 prompt / plugin code）。
3. 选实验 stage（理想：与 baseline 同类的下一个 stage）。
4. 跑 spec_gen_start 取新 prompt → 记录 prompt_chars。
5. 跑 spec_gen → spec_gen_approve → code_gen → cmocka → kernel build。
6. 运行 collect.py <experiment> 得 experiment.json。
7. 跑 ab_run.py baseline.json experiment.json：
   - 计算 4 道门通过/失败
   - 计算 6 个改进信号 vs 阈值
   - 输出 markdown 报告
8. 报告通过 → commit 优化；不通过 → revert + 调整。
```

## 5. 多优化批量合入

当 N > 1 个优化要一次性合入时：
- **递增 A/B**：先合 #1，跑测；通过后合 #2，跑测；以此类推
- **不允许打包合入**：不能 "#1 + #2 一起测"——一旦失败无法定位是哪条优化退化
- **回滚单元 = 单条优化**：每条优化必须独立 commit，便于精准 revert

## 6. 回归触发

每合入 3 条优化后必跑 `tools/regress/run_all.sh`：cmocka + LTP smoke 全套，
确保没有累积质量漂移。

## 7. 终止条件

- 累计 5 条优化合入 → 跑 LTP smoke + 一个全新 stage 的端到端实测 → 出"插件优化收益总报告"
- 任何门 #3 invariant 退化 → 立刻全部回滚，重新审 prompt 设计

## 8. 数据记录

每次实验追加一行到 `runs/index.csv`：
```
date,optimization,baseline_stage,experiment_stage,gates_passed,signals_passed,merge_decision
```
