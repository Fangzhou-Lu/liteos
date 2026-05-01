# specfs-port 插件：设计、实现与使用

本文档详细描述 `.claude/plugins/specfs-port/` 插件——一条把 Linux 内核文件系统
"先抽规范，再生代码" 移植到 OpenHarmony LiteOS-A 的 HITL（Human-In-The-Loop）
流水线。

文档面向需要理解、扩展或运维该插件的工程师；想直接用的看 §6 即可。

---

## 1. 概览

### 1.1 解决什么问题

直接把 Linux 文件系统逐行翻译进 LiteOS-A 是高失败率工作：

- Linux FS 携带 **page cache / buffer_head / RCU / jbd2 / dcache** 等十多年沉淀的
 Linux 假设；LiteOS-A 都没有。
- 直接翻译产生"能编译、会泄漏、行为微妙错乱"的代码——回归测试又不全，bug 隐藏深。
- 大模型一次性生成十几 KB 移植代码不可控，且没有可追溯单元。

`specfs-port` 把这条路径**强制经由形式化中间表示**——SYSSPEC 风格的规范——
让用户在每一步对模型的行为（spec 抽取、Linux→LiteOS 映射、代码生成）做精细
review 与 refine。

### 1.2 与上游 SpecFS 论文的关系

插件**保留** FAST'26 论文 *Sharpen the Spec, Cut the Code*（arXiv:2512.13047）
的核心思想：

- 规范是事实来源，不是注释。
- 代码由规范驱动生成，不是反过来文档化。
- DAG 描述阶段间依赖；跨阶段不变量 (invariant) 必须传递。

但**重写**了执行层与控制流以适配两类约束：

1. LiteOS-A 内核移植没有论文假设的回归测试套件，无法用作生成质量的自动验证关。
2. 用户拒绝把生成结果完全交给模型决策——必须在每个关键节点保留人工 review
 与 refine 能力。

§2-3 给出对应关系矩阵；§4-5 给出执行层细节。

---

## 2. 论文核心思想回顾

| 元素 | 论文中的角色 | 插件的对应物 |
|---|---|---|
| **SYSSPEC**（结构化规范）| `[PROMPT]` `[RELY]` `[GUARANTEE]` `[SPECIFICATION]` 四段约束 | 同；扩为 `## Refine Prompt` 子段处理多锁场景 |
| **`gen.py`**（批处理生成器）| 调 OpenAI API 把 spec → code，最多 8 轮迭代 | 改成"上下文注入"，由 Claude 在用户会话中即时生成；MCP server 装配 prompt |
| **`SpecEvaluator`**（自审）| LLM-judge 自检生成代码与 spec 一致 | 保留为 Layer 3，可选开启（HITL 默认关）|
| **DAG 阶段图** | `evolvefs` 增量演化模式记录阶段依赖 | 升级为正交概念：每个 FS port 都有 DAG，spec 与 code 双层批准 |
| **回归测试关** | gen 失败重试触发条件 | LiteOS-A 没有；用 Layer 0 LSP + Layer 4 用户 review 替代 |

---

## 3. 插件 vs 论文：8 项关键差异

| # | 主题 | 论文做法 | 插件改进 | 动机 |
|---|---|---|---|---|
| 1 | **生成的执行层** | OpenAI API 调用 | Claude Code 上下文注入 | 节省 API 费用；LLM 直接复用 Read/Edit/Bash 等工具，反馈环更短 |
| 2 | **验证关** | 编译 + 回归测试 | 5 层防御（LSP / 编译 / 构建+QEMU / SpecEval / 用户 review） | LiteOS-A 没有完整回归测试，引入分层防御 |
| 3 | **失败迭代** | 全量重试，不传 stderr | 编译错误截前 500 字反注入到下一轮 prompt 的 `[Modification suggestions]` 段，单 spec 局部重生 | 论文已知短板：信号丢失大 |
| 4 | **HITL 时机** | 仅最终结果 review | 每阶段双层（spec / code）approve；ask-first 在生成前澄清歧义 | 用户需求："不能由模型决定最终代码产物" |
| 5 | **演化模式** | `evolvefs` 用 `Follow the previous specification` 复用 | 同思想，但提前到 ：每个 helper 都是独立 stage，跨 stage refine 不重做祖先 | 同上，更细粒度的 review/refine |
| 6 | **`[RELY]` 严格度** | 部分允许抽象描述 | 强制每条都是真实 LiteOS-A C 声明（`LOS_MuxLock` 全签名等）| 防止 LLM 幻觉出 `kmalloc`/`mutex_lock` 等 Linux 接口 |
| 7 | **`[GUARANTEE]` 调用约定** | 仅函数签名 | 强制紧跟 `/* 调用约定: ... */` 注释块（持锁状态、副作用、错误返回）| 把契约固化进规范，下游消费者不必再读上游代码 |
| 8 | **格式陷阱主动扫描** | 无 | 阶段 3 末尾扫描 4 类陷阱（CRC 算法、字节序、字符集、对齐）| 评测集 eval-3 (EROFS) 中有 baseline 漏检的 CRC32c vs LOS_Crc32 不兼容问题 |

---

## 4. 架构与运行原理

### 4.1 整体数据流

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 一个 FS 移植任务 │
│ │
│ Linux 源码 ─ Loop A ─→ SYSSPEC spec ─ Loop B ─→ LiteOS-A 代码 │
│ ▲ ▲ │
│ │ AskUserQuestion │ 5-Layer 防御（按序） │
│ │ user approve │ Layer 0 LSP │
│ └─ DAG.spec.approved_at ──→│ Layer 1 gcc -fsyntax-only │
│ │ Layer 2 build + QEMU │
│ │ Layer 3 SpecEval（opt-in）│
│ │ Layer 4 user review │
│ └─→ DAG.code.approved_at │
└──────────────────────────────────────────────────────────────────────────┘
```

每个 FS（exfat、erofs、f2fs ...）有独立 `spec/<module>/.specfs.dag.json`
追踪 7-12 个 stage（mount、lookup、read、各 helper 等）的双层批准状态。

### 4.2 两个 HITL 循环

**Loop A — Linux 源码 → SYSSPEC 规范**

输入：Linux 源码路径 + 目标 stage 名（如 `mount`）。
LLM 的工作：

1. Read Linux 模块，从 `*_fs.h` `*_raw.h` 入手，再到 `super.c`/`<op>.c`。
2. 按 `prompts/ask_first_rules.md` 扫描 4 类歧义（多解释、缺信息、与祖先冲突、超范围）。
3. 经 `AskUserQuestion` 提交 ≤4 个二选一/多选关键决策给用户。
4. 起草 SYSSPEC 四段 + 必要的 `## Refine Prompt`（>2 锁场景）。
5. 写 `.draft` 后缀文件，调用 `AskUserQuestion(approve|edits|reject|cancel)`。
6. user approve → 去后缀，写 DAG `<stage>-vN.spec.approved_at = now`。

**Loop B — 规范 → LiteOS-A C 代码**

输入：批准的 spec 路径。
插件装配 prompt（见 §5.3），LLM 生成 .c 文件。然后按序走 5 层防御：

| Layer | 工具 | 触发条件 |
|---|---|---|
| 0 LSP | clangd via cclsp / OMC LSP | 写入即查；类型/包含路径错误立即可见 |
| 1 编译 | `gcc -fsyntax-only` | LSP 通过后跑一次完整语法/类型检查 |
| 2 构建 | 远程 `hb build` 或 kernel-only ninja | Layer 1 通过；可被 `--no-build` flag 跳过 |
| 3 SpecEval | 模型自审 spec/code 一致 | 默认关（HITL 模式下信噪比低）；`--speceval-on` 启用 |
| 4 用户 review | AskUserQuestion + 验证清单 | 始终运行 |

任一 layer 失败 → 截取 stderr 前 500 字符回注入 prompt 的
`[Modification suggestions]` 段，**单 spec 局部重生**（不全量重试，不丢信号）。

### 4.3 DAG 状态模型

文件 `spec/<module>/.specfs.dag.json` 的结构（schema_version=1）：

```json
{
    "module": "exfat",
    "schema_version": 1,
    "stages": [
    {
    "id": "mount",
    "stage_name": "mount",
    "depends_on": [],
    "spec": { "path": "spec/exfat/interface/exfat_mount.spec",
    "approved_at": "2026-04-30T20:26:18+00:00",
    "common_header_path": "spec/exfat/common.header",
    "dirty": false },
    "code": { "path": "fs/exfat/exfat_super.c",
    "approved_at": "2026-04-30T21:04:19+00:00",
    "dirty": false },
    "invariants": [
    { "id": "exfat-mount-rollback-lifo",
    "text": "All failure branches release resources in reverse LIFO via goto-stack." },
    ...
    ],
    "exports": [
    { "symbol": "VfsExfatMount", "kind": "func" },
    { "symbol": "g_exfatMountOps", "kind": "var" },
    { "symbol": "exfat_fsmap", "kind": "fsmap" }
    ]
    }
    ]
}
```

关键不变性：

- **Invariant 跨阶段累加** — 子 stage 的 `[INHERITED INVARIANTS]` 段由
 `dag.collect_invariants(node_id)` 自动从所有祖先收集，保证下游不破坏上游契约。
- **Export 跨阶段汇总** — 子 stage 的 `[PRIOR CODE INTERFACE]` 段由
 `dag.collect_exports(node_id)` 收集，提示 LLM 哪些符号可以引用。
- **dirty cascade** — 修改某个 stage 的 spec 时，所有依赖它的 stage 被打 dirty，
 下次 review 必须重新批准。

### 4.4 5 层防御的实现位置

| Layer | 实现 | 触发点 |
|---|---|---|
| 0 LSP | `cclsp + clangd`（`cclsp.json` + `.clangd`）| 写入即诊断；通过 `mcp__cclsp__get_diagnostics` 调用 |
| 1 编译 | `gcc -fsyntax-only` 配合 stub 头集 | Loop B 末尾，Layer 0 之后 |
| 2 构建 | SSH 远端 `hb build -p qemu_small_system_demo` | 默认开启；可 `--no-build` 跳过 |
| 3 SpecEval | `prompts/speceval.md`（论文原文）| `--speceval-on` 启用 |
| 4 用户 review | `prompts/validation_checklist.md` + AskUserQuestion | 始终运行 |

---

## 5. 设计实现

### 5.1 文件布局

```
.claude/plugins/specfs-port/
├── .claude-plugin/plugin.json # Claude Code 插件清单
├── .mcp.json # MCP server 拉起配置
├── DESIGN.md # 设计规格（本文档的简略版）
├── README.md # 使用说明（用户视角）
├── prompts/ # 8 个 markdown 片段
│ ├── codegen.md # Loop B 代码生成（来自 specfs/tools/gencode.py:158）
│ ├── speceval.md # Layer 3 自审（来自 specfs/tools/gencode.py:200）
│ ├── linux_to_spec.md # Loop A 规范起草系统提示
│ ├── ask_first_rules.md # 4 类歧义扫描 + AskUserQuestion 模板
│ ├── style_rules.md # LiteOS-A 强制规则（libsec、版权头、命名）
│ ├── linux_to_liteos_table.md # Linux→LiteOS-A 原语映射表
│ ├── format_traps.md # CRC/字节序/字符集/对齐 4 类陷阱
│ └── validation_checklist.md # Layer 4 用户 review 辅助清单
├── commands/ # 3 个 slash command
│ ├── specfs-port.md # 入口：DAG 状态 + 推荐下一步
│ ├── specfs-port-spec.md # Loop A 驱动
│ └── specfs-port-code.md # Loop B 驱动（含 --no-build / --speceval-on / --prompt-override）
└── server/ # Python MCP server (FastMCP)
    ├── pyproject.toml
    ├── specfs_server.py # MCP entry，25 个 tool
    ├── state.py # Session/Clarification/FailureRecord 数据类
    ├── dag.py # DAG 加载/保存/walk
    ├── extract.py # 正则 C 接口提取（[PRIOR CODE INTERFACE] 段输入）
    ├── prompts.py # 模板加载 + 占位符替换 + 空段裁剪
    └── commit_node.py # 通用 DAG 节点提交 CLI（开发期 stand-in）

spec/<module>/ # 每 FS 一棵规范树
├── common.header # 共享 import + 类型 + extern 全局（frozen 后只增不减）
├── interface/ # VFS 顶层 op
├── inode/ file/ path/ ... # 各子层
└── .specfs.dag.json # DAG 状态（committed to git）
```

### 5.2 MCP Server 工具表（25 个）

按职责分 7 组。命名遵循 `<层>_<动作>` 模式。

**会话生命周期（4）**：`session_start`、`session_status`、`session_pause`、
`session_end`。

**Loop A（5）**：`spec_gen_start`（装配 Loop A prompt 并返）、`spec_gen_submit`
（接收 LLM 起草的 spec 文本）、`spec_gen_refine`（带 user_suggestion 重装 prompt）、
`spec_gen_approve`（写入 final + DAG）、`spec_gen_diff`（draft vs prior 对比）。

**Loop B（4）**：`code_gen_start`（装配 codegen prompt）、`code_gen_submit`、
`code_gen_refine`、`code_gen_approve`。

**5 层防御（5）**：`layer0_lsp`、`layer1_compile`、`layer2_build_qemu`、
`layer3_speceval`、`layer4_user_review_input`。

**Ask-first（2）**：`askfirst_scan`（运行 prompts/ask_first_rules 扫描）、
`askfirst_record_clarification`（保存用户答复进 session）。

**DAG 维护（3）**：`dag_load`（无副作用读）、`dag_check_node_complete`、
`dag_mark_dirty_cascade`。

**Prompt artifact（2）**：`prompt_export`（导出当前装配好的 prompt 给
`--prompt-override`）、`prompt_apply_override`（用户编辑后回灌）。

> **当前状态**：MCP server 代码完整，但 Claude Code runtime 把它注册成功后
> 工具索引未含 `mcp__specfs__*` 命名空间。开发期使用 `commit_node.py` 直接驱动
> `dag.py` 与 `prompts.py` 作为 stand-in。下一步：调试 `.mcp.json` 配置或
> 切换到 OMC MCP runtime。

### 5.3 8 个 prompt 片段如何装配

`prompts.py::assemble_codegen_prompt` 把 8 个片段按以下槽位拼成单一字符串：

```
[ROLE] linux_to_spec.md / codegen.md 的角色定义
[INPUTS] 模块、stage、Linux 路径
[FROZEN CONTRACT] common.header 内容（首阶段为 "(empty)"）
[INHERITED INVARIANTS] dag.collect_invariants 输出
[PRIOR CODE INTERFACE] extract.render_interface_summary 输出
[STYLE RULES] style_rules.md
[LINUX→LITEOS PRIMITIVE MAP] linux_to_liteos_table.md
[FORMAT-COMPATIBILITY TRAPS] format_traps.md
[ASK-FIRST RULES] ask_first_rules.md
[USER CLARIFICATIONS] session 累积的 ask-first 答复
[USER SUGGESTIONS] 当前 round 的自由文本反馈（refine 时）
[Previously generated spec] 仅 refine round 出现
[Previously generated code] 仅 refine round 出现（Loop B）
[Modification suggestions] 失败 layer 的 stderr 摘要（多 source 用 <source: lsp/compile/build/qemu/speceval/user>）
[OUTPUT] 格式约定（spec 四段 / 单一 ```c 代码块）
```

空槽位由 `_drop_empty_sections` 自动剪掉，避免 prompt 噪声。

### 5.4 跨阶段 reconcile 机制

下游 stage spec 收紧上游契约（典型：把 sbi 改成 const 指针）会触发 LSP 类型不匹配。
插件原计划的 `reconcile_spec` MCP 工具自动检出并触发上游 `spec_gen_refine`，
但 暂未实现——目前由 Layer 0 LSP 在 `mcp__cclsp__get_diagnostics` 抓到
`conflicting_types` 错误，开发者人工修头文件统一。

实测在 exFAT 端口中触发 2 次（dentry 与 balloc 的 sbi const 收紧），LSP
立即抓到，单次修复 < 1 分钟。

---

## 6. 使用方式

### 6.1 安装与依赖

```
Python 3.11+
uv (https://docs.astral.sh/uv/)
clangd 15+（LSP）
mcp Python 包（uv 自动装）
gcc（Layer 1 fsyntax-only）
SSH 通到远端 build host（Layer 2，可选）
```

插件本身位于 `.claude/plugins/specfs-port/`，是项目级 Claude Code 插件——
进入 `kernel/liteos_a/` 启动 Claude Code 即自动加载（首次需 `/reload-plugins`）。

### 6.2 启动一个 FS 端口

```
$ claude
> /specfs-port exfat # 查看 DAG 状态、推荐下一步
```

无现存 DAG 时输出：

```
DAG: spec/exfat/.specfs.dag.json (not yet created)
推荐第一步：
    /specfs-port-spec /Users/.../linux/fs/exfat mount
```

### 6.3 Loop A 走查（mount 阶段）

```
> /specfs-port-spec /Users/kissa/Codebase/linux/fs/exfat mount
```

LLM 顺序执行：

1. 读 Linux exfat super.c、exfat_fs.h、exfat_raw.h
2. 触发 ask-first 扫描，弹出 4 个二选一关键决策（RO？严格 CRC？盘上 upcase？选项串解析？）
3. 用户答案保存进 session
4. LLM 起草 mount.spec.draft（含 [PROMPT][RELY][GUARANTEE][SPECIFICATION]+`## Refine Prompt`）
5. 自动 sanity check（[RELY] 全是真实 C 声明？[GUARANTEE] 有调用约定块？至少一个 invariant？）
6. AskUserQuestion(approve | edits | reject | cancel)
7. approve → 去 .draft 后缀；commit_node.py 写 DAG `mount.spec.approved_at`

### 6.4 Loop B 走查（mount 阶段）

```
> /specfs-port-code spec/exfat/interface/exfat_mount.spec
```

执行：

1. 装配 codegen prompt（含 frozen common.header + 已批准祖先 invariants + 已生成代码接口）
2. LLM 生成 fs/exfat/exfat_super.c
3. Layer 0 LSP（cclsp/clangd）即时诊断；通过则继续
4. Layer 1 `gcc -fsyntax-only`；失败则把 stderr 注入 prompt 重生
5. Layer 2（默认）远端 `hb build`；通过则继续
6. Layer 3 SpecEval（默认关）
7. Layer 4 user review，AskUserQuestion approve；commit DAG code 层

任一 layer 失败：错误摘要回灌 `[Modification suggestions]` 段，重跑 step 2-N。
最多 8 轮（论文上限），超出后改求用户介入或拆分 stage。

### 6.5 多 helper 的依赖管理

`mount.spec` 依赖 `exfat_parse_options`、`exfat_calc_chksum32` 等 helper。
插件强制 helper 先有自己的 spec。例如 exFAT 共 7 个 stage，依赖图：

```
chksum vfs_ops_stub
    │ │
    └─→ upcase ──────┐ │
    │ │
    options │ │
    │ │ │
    dentry ──→ balloc │
    │ │ │
    └──→ mount ◄─┴──────┘
```

`commit_node.py spec exfat <id> <stage> <path> --depends-on=…` 自动维护 DAG。

### 6.6 DAG 维护与历史

```
$ uv run python commit_node.py show exfat
{ "module": "exfat", "schema_version": 1, "stages": [ ... ] }

$ uv run python commit_node.py show exfat mount
{ "id": "mount", "spec": {...}, "code": {...}, "invariants": [...] }

# Approve spec layer
$ uv run python commit_node.py spec exfat mount mount \
    spec/exfat/interface/exfat_mount.spec \
    --depends-on=dentry,balloc,upcase,vfs_ops_stub \
    --exports="VfsExfatMount:func,g_exfatMountOps:var,exfat_fsmap:fsmap"

# Approve code layer
$ uv run python commit_node.py code exfat mount fs/exfat/exfat_super.c
```

Invariants 由 `commit_node.py` 自动从 spec 文件正则提取（`**Invariant** (id=...)` 模式）。

---

## 7. 真实端到端案例：exFAT mount

### 7.1 输入与产物

| 类别 | 体量 |
|---|---|
| Linux 源码 | `/Users/kissa/Codebase/linux/fs/exfat/` 14 文件 7,399 行 |
| **生成的 spec 树** | `spec/exfat/` 11 文件 1,344 行 |
| **生成的 C 代码** | `fs/exfat/` 9 文件 1,856 行（仅 .c：1,476 行） |
| 构建文件 | BUILD.gn / Makefile / Kconfig 共 98 行 |
| **spec/code 比** | **1344 / 1476 = 0.91** ✓（论文生产力命题）|

### 7.2 7 个 DAG 节点

| stage | depends_on | invariants | exports | spec 行数 | code 行数 |
|---|---|---|---|---|---|
| chksum | ∅ | 4 | 2 | 101 | 70 |
| options | ∅ | 5 | 1 | 129 | 306 |
| dentry | ∅ | 6 | 2 | 217 | 258 |
| balloc | dentry | 6 | 3 | 192 | 190 |
| upcase | dentry, chksum | 6 | 2 | 159 | 136 |
| vfs_ops_stub | ∅ | 4 | 2 | 90 | 43 |
| mount | ∅（声明依赖在 [RELY]）| 5 | 5 | 301 | 473 |

合计 **36 个 invariants**、**17 个 exports**、共 7 stage 双层 approved。

### 7.3 LSP 检出的跨阶段冲突案例

事件 1：dentry spec 把 `exfat_find_root_dentry(sbi, ...)` 收紧为
`const exfat_sb_info *sbi`。生成 dentry.c 后 Layer 0 LSP 报：

```
exfat_dentry.c:212: error: conflicting types for 'exfat_find_root_dentry'
exfat.h:178: note: previous declaration was 'int exfat_find_root_dentry(exfat_sb_info *, ...)'
```

`exfat.h` 的声明随 mount [RELY] 写出（非 const）。手工修 exfat.h 加 const，
LSP 立即清。预计 后续 由 `reconcile_spec` 工具自动触发 mount spec_gen_refine。

事件 2：同样模式发生在 balloc 的 `exfat_count_used_clusters`。

### 7.4 真实编译错误检出（Werror）

事件 3：`exfat.h` 注释 `/* fs/exfat/*.c, externed for mount glue */` 含
`/*` 子串（在 `t/*` 处）。`gcc -Werror -Wcomment` 抓到，clangd 因 flag 集合
不含 `-Wcomment` 漏过。修复：把 `*.c` 改成"translation units"。

事件 4：mount spec [RELY] 假设 `ClearDiskPartName(part)` 存在；编译时
`undeclared function`。fs/fat/fatfs.c:1226 用的是 `free(part->part_name);
part->part_name = NULL` 模式（`SetDiskPartName` 内部 strdup）。改 mount 实现
匹配 fatfs 模式。

### 7.5 QEMU 端到端验证

`smallmmc.img` 4 分区（10-30M / 30-80M / 80-100M vfat + 100M-end exfat）+
bootargs `exfataddr=100M` → kernel 创建 mmcblk0p0..p3 + auto-mount /storage 与
/userdata + 用户 `mount -t exfat /dev/mmcblk0p3 /mnt/exfat`：

```
OHOS:/$ mount -t exfat /dev/mmcblk0p3 /mnt/exfat
OHOS:/$ echo MOUNT_RC=$?
MOUNT_RC=0
OHOS:/$ mount
/dev/mmcblk0p3 on /mnt/exfat type exfat
/dev/mmcblk0p2 on /userdata type vfat
/dev/mmcblk0p1 on /storage type vfat
/dev/mmcblk0p0 on / type vfat
```

`ls /mnt/exfat` 返 `Function not implemented`—— 仅实现 mount/unmount/statfs，
`g_exfatVops` 全 NULL，VFS 框架对 NULL op 返 -ENOSYS。读文件需要 lookup /
readdir / read 等下一轮 Loop A+B（后续 范围）。

### 7.6 已知边界

- `umount /mnt/exfat` 在 toybox 进程中触发 NULL deref（VfsExfatUnmount 释放
 `mount->vnodeCovered` 后 VFS 框架后续访问空指针）。需要 mount
 spec_gen_refine 修复释放序。
- 读路径：lookup/readdir/open/read/close 均需 Loop。
- 写路径：在后续版本 之后；FAT 表写、bitmap 修改、vol_flags 持久化、journal 全部缺失。

---

## 8. 已知边界与未来改进

### 8.1 已知短板

| 编号 | 问题 | 影响 | 计划修复版本 |
|---|---|---|---|
| L-1 | MCP server 的 `mcp__specfs__*` 工具未注册到 Claude Code 工具索引 | 当前用 `commit_node.py` CLI stand-in；slash command 走 inline 上下文 | 后续 |
| L-2 | `reconcile_spec` 工具未实现 | 跨阶段契约冲突靠 LSP 抓 + 人工修头文件 | 后续 |
| L-3 | 自动 spec_gen_refine 触发 | 失败 layer 的 stderr 回灌目前手工调用 `spec_gen_refine`；应自动 | 后续 |
| L-4 | `--prompt-override` 仅在 spec 写完才支持 | 用户想编辑装配好的 prompt 但 prompts.py 装配是单向的 | 后续 |
| L-5 | DAG 不支持分支（fork） | 无法并行尝试两个 mount 草稿 | 后续 |

### 8.2 设计层未来改进

- **LLM 后端可选** — 当前耦合 Claude Code 上下文；将来加一层 adapter，
 能切换到 OpenAI/Anthropic API 后端做夜间无人值守批跑（仅在测试齐全的
 FS 上启用，HITL 仍是默认）。
- **多 FS 横向比较** — 同一 spec 在多 LLM 跑，三选一最优。论文 § 4 也提到。
- **形式化校验** — Invariant 经 SMT 翻译送 Z3 检查，捕捉 spec 内部矛盾。
 当前 invariant 是自然语言。

---

## 9. 引用与背景

- Sharpen the Spec, Cut the Code，FAST'26，arXiv:2512.13047。
- 本仓 `liteos-fs-port` skill（`.claude/skills/liteos-fs-port/SKILL.md`）——
 插件化之前的 5 阶段流水线说明书，本插件吸收其全部规则进 `prompts/style_rules.md`。
- LiteOS-A 工程上下文：`CLAUDE.md`（构建/测试 / two-build-system 陷阱 /
 filesystem 注册 / QEMU 端到端 testing recipe）。
- `.claude/plugins/specfs-port/DESIGN.md` — 插件实现规格的精简版，本文档的源材料之一。

---

附录 A：MCP server stand-in CLI（`commit_node.py`）的常用命令一览

```
# 看全 DAG
uv run python commit_node.py show exfat

# 看单 stage
uv run python commit_node.py show exfat mount

# 批准 spec 层（自动从 .spec 文件提取 invariants）
uv run python commit_node.py spec <module> <id> <stage> <spec_path> \
    [--depends-on=a,b,c] [--exports=sym:kind,...]

# 批准 code 层
uv run python commit_node.py code <module> <id> <code_path>
```

`--exports` 取值：`func` / `var` / `fsmap`（FSMAP_ENTRY 链接表项）。
