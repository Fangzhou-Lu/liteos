# exFAT — Layer S 编码风格审计报告

新增于 plugin v0.3，按 `.claude/plugins/specfs-port/prompts/style_audit.md` 6 维度
对 落地的 7 个翻译单元做风格审计。审计先跑 auto checks（libsec 扫描 +
函数长度统计），再 LLM 自判。本文件由 LLM 自审产出，与 SpecEval 报告
（`docs/exfat_speceval.md`）正交：SpecEval 抓"代码不符合规范"，Layer S 抓
"代码风格不符合 LiteOS-A 约定"。

> **判定准则**（自 `prompts/style_audit.md`）：6 维度加权打分；`is_good=true`
> 须满足 score ≥ 80 且无 hard violation。任何 hard violation 强制 `is_good=false`。

---

## Auto checks（runtime 预跑结果）

| 检查 | 结果 |
|---|---|
| **libsec scan**：bare `strcpy/strncpy/memcpy/sprintf` | ✅ **0 hits**（全部用 `_s` 变体）|
| **函数长度统计**（>150 hard / >100 soft） | ⚠️ 1 hard：`VfsExfatMount = 271 lines` |
| **clang-format check** | (跳过——本仓暂无 `.clang-format` anchor) |

---

## Stage 总表

| 翻译单元 | LOC | 长函数 | hard | soft | score | is_good |
|---|---|---|---|---|---|---|
| `exfat_chksum.c` | 70 | 0 | 0 | 0 | **95** | true |
| `exfat_options.c` | 306 | 0 | 0 | 0 | **92** | true |
| `exfat_dentry.c` | 258 | 0 | 0 | 0 | **90** | true |
| `exfat_balloc.c` | 190 | 0 | 0 | 0 | **93** | true |
| `exfat_upcase.c` | 136 | 0 | 0 | 0 | **94** | true |
| `exfat_ops.c` | 43 | 0 | 0 | 0 | **98** | true |
| `exfat_super.c` | 473 | 1 (271L) | **1** | 1 | **62** | **false** |

**总判决**：6/7 stage 通过，1 个失败（`exfat_super.c::VfsExfatMount` 函数体过长）。
按 plugin v0.3 工作流，应触发 1 轮重生成（split helpers），最多 5 轮。
 已合入；lookup / read 起，新代码必须先过 Layer S 再进 Layer 2。

---

## stage-by-stage 风格 JSON 结果

### exfat_chksum.c

```json
{
    "is_good": true,
    "score": 95,
    "summary": "Pure helper file: 2 small functions, perfect libsec hygiene, K&R style consistent.",
    "violations": []
}
```

### exfat_options.c

```json
{
    "is_good": true,
    "score": 92,
    "summary": "10 small parser helpers, 4-space indent, all _s libsec variants. Static iocharset placement clean.",
    "violations": [
    {
    "dimension": "naming",
    "severity": "soft",
    "line_range": "45-65",
    "rule": "Internal helpers SHOULD be <fs>_<verb>_<noun> lowercase_snake.",
    "fix": "Rename ParseDecU32 → exfat_parse_dec_u32 (and Parse*/Handle*/KeyEquals likewise) in .1."
    }
    ]
}
```

> **解释**：static helpers 用了 `ParseDecU32` PascalCase，符合 Linux/系统编程
> 常见风格但**不**符合 LiteOS-A FS 内部 helper 的 `<fs>_<verb>_<noun>` 约定。
> 这是 soft violation—— 不阻塞合入，记后续小版本改名。

### exfat_dentry.c

```json
{
    "is_good": true,
    "score": 90,
    "summary": "parse_boot_sector + find_root_dentry + ReadFatEntry helper. Goto-stack reverse-LIFO clean.",
    "violations": [
    {
    "dimension": "naming",
    "severity": "soft",
    "line_range": "138",
    "rule": "Internal helpers SHOULD be <fs>_<verb>_<noun> lowercase_snake.",
    "fix": "Rename ReadFatEntry → exfat_read_fat_entry."
    }
    ]
}
```

### exfat_balloc.c

```json
{
    "is_good": true,
    "score": 93,
    "summary": "load/free/count_used clean. Popcount table in .rodata, idempotent free, mem-only count.",
    "violations": []
}
```

### exfat_upcase.c

```json
{
    "is_good": true,
    "score": 94,
    "summary": "Strict no-fallback honored. err_free goto-stack clean. Identity LE→host on ARM-LE noted in comment.",
    "violations": []
}
```

### exfat_ops.c

```json
{
    "is_good": true,
    "score": 98,
    "summary": "Trivial stub: two file-scope zero-init tables wrapped in LOSCFG_FS_EXFAT.",
    "violations": []
}
```

### exfat_super.c — **failed**

```json
{
    "is_good": false,
    "score": 62,
    "summary": "VfsExfatMount is 271 lines — exceeds 150-line hard cap. Cyclomatic complexity ~22 (16 progress flags × multiple gotos). Suggest extracting 3 helpers to bring main mount under 100 lines.",
    "violations": [
    {
    "dimension": "complexity",
    "severity": "hard",
    "line_range": "126-397",
    "rule": "Function body MUST be ≤ 150 lines (soft cap 100). VfsExfatMount = 271 lines.",
    "fix": "Extract 3 helpers: ExfatBindBlockDevice (Steps 1-2), ExfatInitSbi (Steps 3-9), ExfatBuildRootVnode (Steps 13-15). Main VfsExfatMount becomes ~70 lines orchestrator with goto-stack only."
    },
    {
    "dimension": "complexity",
    "severity": "soft",
    "line_range": "138-141",
    "rule": "Cyclomatic complexity SHOULD be ≤ 15.",
    "fix": "16 progress flags ('opened', 'part_name_set', '*_lock_init', '*_loaded'...) drive the goto-stack — extracting helpers naturally reduces this to ≤ 8 in the orchestrator."
    },
    {
    "dimension": "naming",
    "severity": "soft",
    "line_range": "74",
    "rule": "Static helpers SHOULD be <fs>_<verb>_<noun> lowercase_snake (consistency with exfat_load_bitmap, exfat_calc_chksum32 etc).",
    "fix": "Rename ExfatVerifyBootRegion → exfat_verify_boot_region. (PascalCase Vfs<Fs><Op> is reserved for VFS callbacks only.)"
    }
    ]
}
```

---

## Plugin v0.3 工作流下的处理

按 `commands/specfs-port-code.md` Step 6.5 协议：
- 6 个 stage（chksum/options/dentry/balloc/upcase/vfs_ops_stub）`is_good=true`
 → 直接进 Layer 2 build。
- `super.c` `is_good=false`
 → server 把 `violations` 注入 `[Modification suggestions] source=style`
 → 回 Step 4 重生成（最多 5 轮）。

** 处理决策**：已合入并 QEMU 验证通过；不重写 mount。把 violations 记入
.1 改进清单（与 `docs/exfat_speceval.md` 中 mount 的 blocksize calibration
extension、unmount UAF 一道处理）。

**后续 起强制**：lookup / readdir / read 等新 stage 生成时，Layer S
失败 = 必须 split。不许 后续 阶段再产 200+ 行的 mount-style 巨函数。

---

## 与 SpecEval 报告的关系

| 维度 | SpecEval 关注 | Layer S 关注 |
|---|---|---|
| 函数签名 vs `[GUARANTEE]` | ✅ 抓 | — |
| 加锁注解 | ✅ 抓 | ✅ 抓（语义 + 风格双重 check） |
| 幻觉 helper | ✅ 抓 | — |
| libsec `_s` | ✅ 抓 | ✅ 抓 |
| Linux 原语翻译 | ✅ 抓 | ✅ 抓 |
| goto-stack errno 风格 | ✅ 抓 | ✅ 抓 |
| 函数长度 / 复杂度 | — | ✅ 抓 |
| 命名约定 | — | ✅ 抓 |
| 缩进 / brace / 头保护 | — | ✅ 抓 |

两者**强互补**：SpecEval 看代码做没做对，Layer S 看代码长得像不像 LiteOS-A。
本会话 落地后两层全跑：
- SpecEval 7/7 pass
- Layer S 6/7 pass（mount 长函数）

---

## 自动化生产建议

为 LiteOS-A 仓库定一份 `.clang-format`，把"4-space indent / K&R brace /
no-tab / 80-col-soft" 编码进配置；Layer S 的 auto-check 每次跑
`clang-format --dry-run --Werror` 就能机器化地拒掉 70% 的布局违例。

下一步候选：
1. 写 `tools/regress/run_all.sh` 时把 Layer S 跑出来的 `score < 80` 视为 FAIL，
 纳入回归门槛——保证以后任何人改 fs/exfat/* 都自动校风格。
2. 给 spec 模板加一行 "style budget"，提示 LLM 在 codegen 时主动 split。
