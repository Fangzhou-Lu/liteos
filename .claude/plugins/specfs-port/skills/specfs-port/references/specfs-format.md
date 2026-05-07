# SYSSPEC 规范格式参考

本文档说明 SpecFS 项目（FAST'26 论文 *"Sharpen the Spec, Cut the Code"*）的 SYSSPEC 规范文件结构与各字段含义。本技能产出的规范树严格遵循本格式，以便 `~/workspace/projects/specfs/` 中的 `tools/spec2code.py` 可以直接消费。

## 目录约定

```
fs/<name>/spec/
├── common.header ── 全 FS 共享：头文件依赖、盘上常量、共享 typedef、全局符号
├── interface/
│ ├── interface.header ── 第 1 行列出本目录依赖的其它目录；其后是本目录函数声明
│ └── <op>.spec
├── inode/
├── file/
├── path/
├── bitmap/ (FAT 家族特有)
└── util/
```

每个子目录代表 FS 的一个**抽象层**，从外向内依次更接近盘上数据。`interface/` 是 VFS 可见层；`util/` 与 `common.header` 是叶层。

### 分层依赖原则

- 上层可调用下层；下层不得调用上层（无环依赖）。
- 同层之间不互相调用，复用部分下沉到 `util/` 或更深的层。
- `common.header` 是所有层共享的常量与类型，不含函数。

## `common.header` 文件

```
string, stdio, stdlib, ... # 第 1 行：依赖的标准 / 自定义头文件，逗号分隔
    # 系统头会被翻译为 #include <name.h>
    # 自定义头会被翻译为 #include "name.h"
    # 空行
#define EXFAT_EOF_CLUSTER 0xFFFFFFFFu # 之后任意 C 预处理 / typedef / extern
#define BITS_PER_BYTE 8

typedef struct exfat_chain {
    unsigned int dir;
    unsigned int size;
    unsigned int flags;
} exfat_chain;

extern struct Mount *g_<name>RootMount;
```

由 `tools/spec2code.py` 中的 `generate_header_file()` 自动加上 `#ifndef _COMMON_H` / `#define _COMMON_H` / `#endif` 包装。开发者只写裸内容。

## `<module>.header` 文件

每个子目录一份，例如 `inode/inode.header`：

```
common, util, bitmap # 第 1 行：导入的其它目录（写目录名，不写路径）
    # 转换为 #include "common.h" 等
    # 之后是本模块导出的函数原型，每行一个
unsigned int inode_alloc(struct exfat_chain *p_chain, unsigned int num);
int inode_truncate(struct Vnode *vn, off_t new_size);
```

工具读取 `.header` 推断每份 `.spec` 的可见类型与可调用函数；规范作者写 `.spec` 时无须显式重复 `[RELY]` 中的此类签名（但建议显式列出关键签名以便人审）。

## `<op>.spec` 文件（核心）

每份 `.spec` 描述**一个**函数。整体结构：

```
[PROMPT]
Provide complete `<op>.c` file that implement `<op>` operation.
You can use information first from [RELY], [GUARANTEE] and [SPECIFICATION]
in the first phase, then more information in the refine phase as described
below. Only provide the implementation of that single function and only
include `<module>.h` as the header. Your output is wrapped in a C code
block, and no other unrelevant code should be given.

## First Prompt

[RELY]
```c
typedef struct exfat_chain { ... } exfat_chain;
```
```c
unsigned int exfat_find_free_bitmap(struct exfat_chain *chain, unsigned int hint);
```
// 注释可写在代码块外，描述这个 RELY 函数的语义。

[GUARANTEE]
```c
int exfat_alloc_cluster(struct Vnode *vn, unsigned int num_alloc,
 struct exfat_chain *p_chain, bool sync_bmap);
```

[SPECIFICATION]
**Pre-Condition**:
- `vn` 是有效的 inode vnode；`vn->data` 指向已加载的 `exfat_inode_info`。
- `num_alloc > 0`。
- `p_chain` 已分配；调用前 `p_chain->dir == EXFAT_EOF_CLUSTER` 表示无起始簇。

**Post-Condition**:
**Case 1**（成功）：返回 0。`p_chain->dir` 指向首个新分配簇；`p_chain->size` 增加 `num_alloc`；
位图中对应 bit 全部置位；若 `p_chain->flags == ALLOC_FAT_CHAIN`，FAT 表对应表项串成链表，
末项指向 `EXFAT_EOF_CLUSTER`；`sbi->used_clusters` 增加 `num_alloc`。

**Case 2**（空间不足）：返回 `-ENOSPC`。位图、FAT 表、`sbi->used_clusters`、`p_chain` 内容均不变。

**Case 3**（IO 错误）：返回 `-EIO`。已经分配的簇被 `__exfat_free_cluster()` 释放回去，
位图与 FAT 表回到调用前状态。

**Invariant**:
- 任何时刻 `sbi->used_clusters <= EXFAT_DATA_CLUSTER_COUNT(sbi)`。
- 位图中置位的 bit 数等于 `sbi->used_clusters`。

## Refine Prompt
[RELY]
```c
void LOS_MuxLock(LosMux *m, UINT32 timeout);
void LOS_MuxUnlock(LosMux *m);
```

[SPECIFICATION of exfat_alloc_cluster]
**Pre-Condition**:
进入时不持有 `sbi->bitmap_lock`。

**Post-Condition**:
返回时不持有 `sbi->bitmap_lock`。函数体内对位图、FAT 表、`sbi->used_clusters`、
`p_chain` 的所有改动都在持锁期间发生。
```

## 各字段含义

### `[PROMPT]`
LLM 的元指令。一般写：固定格式即可，唯一变量是函数名与所在模块名。

### `[RELY]`
**调用方/上游已经保证**的事实，包含：
- 类型定义（结构体、typedef、枚举）；
- 全局变量（用 `extern` 声明）；
- 可调用的辅助函数签名；
- 常量宏。

每个 RELY 项用一个 ` ```c ... ``` ` 代码块包裹。代码块**外**可附自然语言注释解释含义。

### `[GUARANTEE]`
本规范要落地实现的函数签名，**只有一行**。

### `[SPECIFICATION]`
按以下子段组织：

- `**Pre-Condition**:`——调用前必须满足的条件，列点。
- `**Post-Condition**:`——调用后必须满足的条件。如果存在多种正常返回情形，用 `**Case 1**:` / `**Case 2**:` / `**Case 3**:` 拆开，每种情形描述其独立后置条件。
- `**Invariant**:`——函数执行前后都必须为真的全局不变式。

约定：
- 用半结构化自然语言描述，不强制谓词逻辑形式，但要"机械可读"——避免 "appropriately"、"suitably"、"if needed" 这类含糊词。
- 遇到边界（NULL 指针、零长度、最大值）必须写出。
- 错误返回必须写明哪个 `errno` 以及是否回滚。

### `## Refine Prompt`（可选）
第二轮规范，专门描述并发与加锁约束。该轮：
- 重写 `[RELY]`：列出可调用的同步原语（`LOS_MuxLock` 等）。
- 重写本函数与每个被调辅助函数的 `[SPECIFICATION]`，但**只描述锁状态**：
 - Pre-Condition：进入时持有/不持有哪些锁。
 - Post-Condition：返回时持有/不持有哪些锁。
- 不重复功能行为。

将加锁分离到 refine 轮次有两个好处：
1. 第一轮规范容易对照 Linux 行为推导，不被锁噪声干扰；
2. 第二轮可以单独审查锁序，避免死锁。

## 规范化质量门槛

- **规范 LOC < 实现 LOC**——SpecFS 论文 §6.4 的核心命题。规范若超过实现行数，多半在重复实现细节。
- **每个 Case 都对应一条独立返回路径**。Linux 实现里 `goto unlock` / `goto free_cluster` 等共用尾巴在规范里要拆开。
- **NULL / 零长 / 最大值**三类边界都必须显式写出。
- **不变式不可省略**——它是 LLM 自检的钩子，缺失时生成的代码常出现 "悄悄破坏全局状态"。
- **Refine Prompt 是默认增项**，不是可选项；只有完全无并发的纯函数（如 NLS 转换）才能省略。

## 与 Linux 实现的对照原则

抽取 Pre/Post 时**读** Linux 实现，但**写** 时剥离 Linux 假设：

| Linux 表述 | 规范应改写为 |
|---|---|
| "the page is uptodate" | "偏移 X 处长度 Y 的缓冲区与盘上数据一致" |
| "buffer_head is locked" | "对应块的 bcache 入口已加锁" |
| "RCU read-side critical section" | （删除——LiteOS-A 无 RCU；改写为普通持锁） |
| "i_size_read(inode)" | "vn->size" |
| "filemap_write_and_wait_range" | "调用 vfs_write 后调用 vfs_fsync" |
| "ALLOC_FAT_CHAIN flag" | （保留——这是盘上语义，不是 Linux 假设） |

判定准则：**这条表述描述的是盘上语义还是 Linux 内核基础设施？** 前者保留，后者改写或删除。
