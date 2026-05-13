# specfs-port-intro - Design Spec

> 介绍 `specfs-port` 插件的项目汇报 PPT — 基于 SYSSPEC (FAST'26) 方法论的 Linux→LiteOS-A 文件系统移植 spec-first 工作流，外加 HITL 闸门与七层防御。

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | specfs-port-intro |
| **Canvas Format** | PPT 16:9 (1280×720) |
| **Page Count** | 22 |
| **Design Style** | B) General Consulting + academic-defense |
| **Target Audience** | 项目内部技术评审 / 同事介绍 |
| **Use Case** | 本地汇报；中文讲解 + 英文术语 |
| **Created Date** | 2026-05-11 |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | PPT 16:9 |
| **Dimensions** | 1280×720 |
| **viewBox** | `0 0 1280 720` |
| **Margins** | left/right 60px, top 50px, bottom 56px (page-number band) |
| **Content Area** | 1160×614 |

---

## III. Visual Theme

### Theme Style

- **Style**: B) 通用咨询 + 学术汇报 (academic-defense)
- **Theme**: Light theme
- **Tone**: 严谨 / 结构化 / 可信 (rigorous · structured · trustworthy)

### Color Scheme

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#F7FAFC` | 极浅灰底，减少纯白疲劳 |
| **Secondary bg** | `#EDF2F7` | 卡片底 / 代码段底 |
| **Primary** | `#1A365D` | IPADS 深海蓝，主标题/流程箭头/边框 |
| **Accent** | `#C53030` | 砖红，强调关键词 / 缺陷标注 / Linux→LiteOS gap |
| **Secondary accent** | `#2C7A7B` | 青绿，成功 / 已通过 / approved 节点 |
| **Body text** | `#2D3748` | 炭黑正文 |
| **Secondary text** | `#4A5568` | 次级说明 |
| **Tertiary text** | `#718096` | 页码 / 注脚 |
| **Border/divider** | `#CBD5E0` | 卡片描边 / 分隔线 |
| **Success** | `#2C7A7B` | 同 secondary_accent |
| **Warning** | `#C53030` | 同 accent |

---

## IV. Typography System

### Font Plan

**Typography direction**: modern CJK sans-serif，中文标题字重稍重；技术名词用 Latin 等宽。

| Role | Chinese | English | Fallback tail |
| ---- | ------- | ------- | ------------- |
| **Title** | `"Microsoft YaHei"` | `Arial` | `sans-serif` |
| **Body** | `"Microsoft YaHei"`, `"PingFang SC"` | `Arial` | `sans-serif` |
| **Emphasis** | `"Microsoft YaHei"` | `Georgia` | `serif` |
| **Code** | — | `Consolas`, `"Courier New"` | `monospace` |

**Per-role font stacks**:

- Title: `"Microsoft YaHei", "PingFang SC", Arial, sans-serif`
- Body: `"Microsoft YaHei", "PingFang SC", Arial, sans-serif`
- Emphasis: `Georgia, "Microsoft YaHei", serif`
- Code: `Consolas, "Courier New", monospace`

### Font Size Hierarchy

**Baseline**: Body font size = 18px (dense-leaning，技术汇报常见)

| Purpose | Ratio | Size | Weight |
| ------- | ----- | ---- | ------ |
| Cover title | 3.3x | 60px | Bold |
| Chapter opener | 2.5x | 45px | Bold |
| Page title | 1.8x | 32px | Bold |
| Hero number | 1.8x | 32px | Bold |
| Subtitle | 1.3x | 24px | SemiBold |
| **Body content** | **1x** | **18px** | Regular |
| Annotation | 0.78x | 14px | Regular |
| Page number | 0.61x | 11px | Regular |

---

## V. Layout Principles

### Page Structure

- **Header**: 0-72px — page title + 顶部蓝条 (4px primary)
- **Content**: 72-664px — 主区
- **Footer**: 664-720px — 页码 + 项目署名 + 章节缩写

### Patterns used

- `single column centered` — Cover / Summary
- `symmetric split (5:5)` — Loop spec / Loop code 双环；Linux↔LiteOS 映射
- `asymmetric split (3:7)` — 流水线图 + 简短说明
- `top-bottom split` — 五阶段流水线 / 七层防御
- `three column cards` — Challenges 三项；Toolchain 模块
- `center-radiating` — DAG 树（中心 + 卫星节点）

### Spacing

| Element | Value |
| ------- | ----- |
| Canvas safe margin | 50px |
| Content block gap | 28px |
| Icon-text gap | 12px |
| Card gap | 24px |
| Card padding | 24px |
| Card border radius | 12px |
| Three-column card width | ~360px |

---

## VI. Icon Usage Specification

### Source

- Built-in: `templates/icons/chunk-filled/`
- 用于流水线节点、Loop spec/code 边界、缺陷标注

### Recommended Icon List

| Purpose | Icon Path | Page |
| ------- | --------- | ---- |
| spec / 文档 | `chunk-filled/file-text` | P10, P11, P15 |
| 代码 | `chunk-filled/code` | P12 |
| 用户确认 | `chunk-filled/circle-checkmark` | P11, P12 |
| 失败 / 缺陷 | `chunk-filled/circle-x` | P05, P08 |
| 流水线箭头 | `chunk-filled/arrow-right` | P11, P12, P15 |
| DAG / 树 | `chunk-filled/hierarchy` | P13 |
| 防御 / 盾 | `chunk-filled/shield` | P16 |
| 反馈 / 循环 | `chunk-filled/arrow-clockwise` | P17 |
| 测试 / 验证 | `chunk-filled/clipboard-check` | P19 |
| 评估 / 数据 | `chunk-filled/chart-bar` | P21 |

---

## VII. Visualization Reference List

不使用 `templates/charts/` 模板（DESIGN.md 内容是流程图与表格为主，非数据图）。所有可视化为自绘 SVG，按页生成。

---

## VIII. Image Resource List

不使用栅格图片。**全部用 SVG 原生几何 + 文本表达**。

---

## IX. Content Outline

### Part 1 · 封面与动机 (Motivation)

#### Slide 01 — Cover

- **Layout**: single column centered
- **Title**: specfs-port
- **Subtitle**: 基于 SYSSPEC 的 Linux→LiteOS-A 文件系统 spec-first 移植工作流
- **Info**: 项目内部介绍 · 2026-05-11

#### Slide 02 — 为什么要做 spec-port

- **Layout**: top-bottom split (上：问题陈述；下：3 个 pain points)
- **Title**: 文件系统移植：成本不在编译，在假设错配
- **Content**:
  - Linux FS 沉淀十几年 Linux 假设（`struct page` / `buffer_head` / RCU / `kmem_cache` / `bio` / dcache）
  - 直接搬到 LiteOS-A → 编译过 / 行为微妙错乱 / 内存泄漏
  - 编译器拦不住的 bug 类：libsec 未使用 / 锁配对 / 错误码符号 / 链路 IO 命名空间

#### Slide 03 — LLM 能不能直接生 C 代码？

- **Layout**: asymmetric split (3:7)
- **Title**: 一次性生成 vs. spec 隔离
- **Content**:
  - 直生 C：把"做什么"和"怎么做"耦合在一起；review 时两类问题混在 diff 里
  - spec-first：先剥掉 Linux 味道写"做什么"，再围绕 LiteOS-A 原语落"怎么做"
  - 把审 spec 与审 code 切成两步，审查认知负担减半

### Part 2 · 挑战 (Challenges)

#### Slide 04 — Challenge I: 语义鸿沟 (Semantic Gap)

- **Layout**: symmetric split (5:5)
- **Title**: Linux 假设 → LiteOS-A 原语
- **Content**:
  - 左：典型 Linux 假设清单 (page cache / RCU / kmem_cache / mutex / submit_bio / dentry hash …)
  - 右：LiteOS-A 等价或缺失 (bcache / 无 RCU / LOS_MemAlloc / LOS_Mux / los_part_read / path_cache …)
  - 注脚：libsec _s 变体 / FSMAP_ENTRY 链接器表 / partition vs disk 索引

#### Slide 05 — Challenge II: 跨阶段依赖 (Cross-stage Dependency)

- **Layout**: center-radiating
- **Title**: 一次只移植一个 op 不够 — DAG 决定可移植顺序
- **Content**:
  - mount → {lookup, readdir, open} → read / write
  - 每个节点继承祖先 invariants
  - 任一祖先 spec 变 → 后代 dirty，需重新评审

#### Slide 06 — Challenge III: LLM 不可信 (LLM Hallucination)

- **Layout**: three column cards
- **Title**: 三类典型幻觉
- **Content**:
  - 函数签名漂移：编出 Linux 风格签名
  - 幻觉 helper：调用不存在的辅助函数
  - 风格污染：strcpy/memcpy 直接出，不走 libsec _s

#### Slide 07 — Challenge IV: spec ↔ code drift

- **Layout**: asymmetric split (3:7)
- **Title**: 单次生成无法保证 spec 真的指导了 code
- **Content**:
  - 论文 SpecEval：spec↔code conformance
  - 我们新增 Loop C linux_compare：spec/code ↔ Linux 原始功能
  - 区分 spec_under_specified vs codegen_drift，决定走 spec_fine 还是 code_refine

### Part 3 · 设计 (Design)

#### Slide 08 — 设计总览：两个 HITL 闭环

- **Layout**: symmetric split (5:5)
- **Title**: Loop spec · Loop code · DAG 持久化
- **Content**:
  - 左 Loop spec：Linux 源码 → SYSSPEC 四段 spec（[PROMPT][RELY][GUARANTEE][SPECIFICATION] + 可选 Refine Prompt）
  - 右 Loop code：spec → C，七层防御 + 用户终审
  - 中部：DAG 节点 = (spec layer, code layer, invariants, exports, depends_on)

#### Slide 09 — SYSSPEC 规范结构

- **Layout**: top-bottom split
- **Title**: 4 段必备 + 1 段可选
- **Content**:
  - [PROMPT] 目标文件 / 头文件 / 输出规则 / 高层意图
  - [RELY] 真实 LiteOS-A 类型与辅助函数签名（禁止 Linux 原语）
  - [GUARANTEE] 函数签名 + 调用约定注释（锁状态属 Phase 2）
  - [SPECIFICATION] Pre / Post-Condition + Cases + Invariants (id=`<m>-<stage>-<noun>`) + System Algorithm
  - `## Refine Prompt` (Phase 2 锁状态契约，仅在 Linux 路径取锁时触发)

#### Slide 10 — Loop spec — Linux → spec

- **Layout**: top-bottom split (流程箭头)
- **Title**: 6 步：摄取 → 抽取 → 用户审 → 评估 → 调优 → 批准
- **Content**:
  - 流程：spec_gen_start → AskUserQuestion（消歧）→ spec_gen_submit → SpecEval (Layer 3) → spec_fine (≤3 round) → spec_gen_approve
  - 闸门：ambiguity 必须先 ask_first；spec 不能含 Linux 原语原文

#### Slide 11 — Loop code — spec → code + 七层防御

- **Layout**: top-bottom split
- **Title**: 7 步：生成 → 编译 → 风格 → 内核 build → cmocka 测试 → spec/code audit → 用户终审
- **Content**:
  - Step 1 codegen → Step 2.1 LSP / 2.2 style / 2.3 kernel build → Step 3 cmocka test gen → Step 4 spec/code audit → Step 5 cmocka exec + QEMU smoke → Step 6 用户终审
  - 各 step 独立 retry 预算（compile 4 / style 5 / build 3 / test_gen 3 / audit 3 / qemu 3）
  - 失败按 source 标签注入 [Modification suggestions]

#### Slide 12 — DAG of stages

- **Layout**: center-radiating
- **Title**: 多阶段 invariant 继承
- **Content**:
  - 根节点 mount，每节点 (spec, code, invariants[], exports[], depends_on[])
  - 任意阶段生成 prompt 时自动注入祖先 invariants
  - 任一祖先 spec 变 → 后代 dirty
  - common.header 自动同步 (auto-extract exports after code approve)

#### Slide 13 — spec 覆盖规则

- **Layout**: three column cards
- **Title**: 接口与窄工具才 spec
- **Content**:
  - (a) VFS 回调 (Vfs<Op>) 必须 spec
  - (b) Linux 公共头函数 (`exfat_fs.h`) 必须 spec
  - (c) 窄工具 (单一职责 / ≤100 LOC / 公式或纯计算) 必须 spec
  - ❌ 实现编排器、字段布局 helper、内部回滚循环都不 spec

### Part 4 · 工具链 (Toolchain)

#### Slide 14 — 工具链架构

- **Layout**: top-bottom split
- **Title**: MCP server + prompt assembly + LLM agent
- **Content**:
  - server/specfs_server.py: 30+ MCP 工具
  - prompts/: 8+ 模板 + 5 on-demand fragment
  - skills/specfs-port: 方法论 + reference manuals
  - commands: /specfs-port-spec, /specfs-port-code

#### Slide 15 — 分层防御拓扑

- **Layout**: top-bottom split
- **Title**: LSP → 风格 → SpecEval → 构建 → QEMU → 用户
- **Content**:
  - Step 2.1 clangd LSP only（P1.3 去掉 gcc -fsyntax-only 兜底）
  - Step 2.2 风格审计 (LLM self-judge vs prompts/style_rules.md)
  - Step 4 SpecEval 子项 + 异构 Linux 审计合并（P1.4）
  - Step 5.1 cmocka host + Step 5.2 QEMU LTP smoke
  - Step 6 HITL 终审，代码 + 测试一起过

#### Slide 16 — Loop C：反向优化 prompt 模板 (v0.5.6 P1.6 Wave 2)

- **Layout**: symmetric split (5:5)
- **Title**: linux_compare + prompt_optimize_{propose, apply}
- **Content**:
  - 左：完成的 stage → linux_compare_start 对比生成 spec/code vs Linux 原始 TU → JSON gaps + 推荐
  - 右：累积推荐 → prompt_optimize_propose 元提示 → fresh agent 改写 → apply 备份并写入
  - fast_eval 模式：spec/code 各只生一遍，禁用所有 refine — 测的是 prompt 引导能力，不是修复能力
  - `.first` 快照保护：refine 不覆盖首次产物

### Part 5 · 案例 (Case Study)

#### Slide 17 — exFAT 移植：从 mount 到 unlink

- **Layout**: top-bottom split
- **Title**: 真实跑通 9 个 stage，1600+ LOC
- **Content**:
  - mount / umount / lookup / open_close / read / getattr / mkdir / create / unlink
  - 每 stage spec ~250 行 / code ~150 行（spec/code ~ 1.6 — SpecFS 生产力命题成立）
  - QEMU 端到端：mount + readdir + read 验证通过

#### Slide 18 — 两层回归

- **Layout**: symmetric split (5:5)
- **Title**: cmocka host (2s) + QEMU LTP smoke (~5min)
- **Content**:
  - Wave A：49 testpoint / 5 helper TU / 主机 x86_64 跑 / mock_disk + 合成 exFAT 镜像
  - Wave B：6 LTP case (creat01/open01/read01/write01/unlink05/stat01) / QEMU virt / 真实 exFAT 分区
  - run_all.sh 串联，docs/test/exfat_regression_<ts>.md 报告

#### Slide 19 — Loop C 跑 unlink 基线一例

- **Layout**: top-bottom split
- **Title**: prompt 反向优化端到端
- **Content**:
  - 对比 fs/exfat/exfat_inode.c::VfsExfatUnlink vs Linux fs/exfat/namei.c::exfat_unlink
  - 找到 5 spec gaps + 3 code gaps（4 高严重）
  - 主要差异：父目录 mtime 刷新缺失 / name-cache 驱逐缺失 / chain reset 缺失
  - 推荐落到 docs/exfat_prompt_feedback.md，apply 后 linux_to_spec.md +50% / codegen.md +66%

### Part 6 · 评估 (Evaluation)

#### Slide 20 — 评估数据

- **Layout**: three column cards
- **Title**: 生产力 / 测试 / 反馈
- **Content**:
  - Cards 1 — spec 体量：spec/code LOC ratio ~1.6（论文 ~1.2，本项目编辑器/常量略多）
  - Cards 2 — 测试：cmocka 49 testpoint / LTP 6 case，覆盖 5 helper TU + 主路径
  - Cards 3 — Loop C：194/194 测试不变；首轮 unlink 比对累积 6 条 prompt 推荐

### Part 7 · 总结

#### Slide 21 — 总结

- **Layout**: single column centered
- **Title**: spec-first 让 LiteOS-A FS 移植可复用、可审、可优化
- **Content**:
  - SYSSPEC 四段 + Refine Prompt：剥离 Linux 假设
  - Loop spec / Loop code 双闭环 + 七层防御：约束 LLM 走对
  - DAG：跨阶段 invariant 继承，common.header 自动同步
  - Loop C (v0.5.6)：用 Linux 比对反向优化 prompt 本身
  - 后续：rmdir / rename / write path / 第二个 FS 验证迁移性

#### Slide 22 — Q & A

- **Layout**: single column centered
- **Title**: Thanks · Q & A
- **Content**:
  - 仓库：feature/spec-port (gitee + github 双远端)
  - 主要参考：FAST'26 arXiv:2512.13047 + .claude/plugins/specfs-port/DESIGN.md

---

## X. Speaker Notes Requirements

每页一份 markdown，存 `notes/`，文件名匹配 SVG。

---

## XI. Technical Constraints Reminder

1. viewBox `0 0 1280 720`
2. 背景 `<rect>`；文本换行 `<tspan>`（禁 `<foreignObject>`）
3. 透明用 `fill-opacity` / `stroke-opacity`；禁 `rgba()`
4. 禁 `mask` / `<style>` / `class` / `foreignObject` / `textPath` / `animate*`
5. 字符：raw Unicode（— · → ⓘ ©），不用 HTML 实体；XML 保留字 `& < > " '` 必须转义
6. `<g opacity>` 禁；逐子元素设 opacity
7. 仅 inline style；禁外联 CSS / `@font-face`
