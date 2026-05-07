# 端到端示例：`exfat_alloc_cluster` 从 Linux 源码到 LiteOS-A 代码

本示例以 Linux exFAT 的核心函数 `exfat_alloc_cluster`（`fs/exfat/fatent.c:322`）为对象，完整走一遍五阶段流水线。读完之后，对其它 FS 函数应能照猫画虎。

## 阶段 1 — Linux 源码摄取

`exfat_alloc_cluster` 的职责：在簇位图（`fs/exfat/balloc.c`）和 FAT 表（`fs/exfat/fatent.c`）中分配 `num_alloc` 个簇，把它们串到 `p_chain` 指向的簇链上。

```c
/* Linux fs/exfat/fatent.c:322 (摘录关键逻辑) */
int exfat_alloc_cluster(struct inode *inode, unsigned int num_alloc,
    struct exfat_chain *p_chain, bool sync_bmap)
{
    int ret = -ENOSPC;
    unsigned int num_clusters = 0, total_cnt;
    unsigned int hint_clu, new_clu, last_clu = EXFAT_EOF_CLUSTER;
    struct super_block *sb = inode->i_sb;
    struct exfat_sb_info *sbi = EXFAT_SB(sb);

    total_cnt = EXFAT_DATA_CLUSTER_COUNT(sbi);
    if (unlikely(total_cnt < sbi->used_clusters))
    return -EIO;
    if (num_alloc > total_cnt - sbi->used_clusters)
    return -ENOSPC;

    mutex_lock(&sbi->bitmap_lock);

    hint_clu = p_chain->dir;
    if (hint_clu == EXFAT_EOF_CLUSTER) {
    hint_clu = exfat_find_free_bitmap(sb, sbi->clu_srch_ptr);
    if (hint_clu == EXFAT_EOF_CLUSTER) { ret = -ENOSPC; goto unlock; }
    }
    /* ... 校验 hint_clu / 处理 ALLOC_NO_FAT_CHAIN ... */
    p_chain->dir = EXFAT_EOF_CLUSTER;

    while ((new_clu = exfat_find_free_bitmap(sb, hint_clu)) != EXFAT_EOF_CLUSTER) {
    if (exfat_set_bitmap(inode, new_clu, sync_bmap)) {
    ret = -EIO; goto free_cluster;
    }
    num_clusters++;
    if (p_chain->flags == ALLOC_FAT_CHAIN) {
    if (exfat_ent_set(sb, new_clu, EXFAT_EOF_CLUSTER)) { ret = -EIO; goto free_cluster; }
    }
    if (p_chain->dir == EXFAT_EOF_CLUSTER) p_chain->dir = new_clu;
    else if (p_chain->flags == ALLOC_FAT_CHAIN) {
    if (exfat_ent_set(sb, last_clu, new_clu)) { ret = -EIO; goto free_cluster; }
    }
    last_clu = new_clu;
    if (--num_alloc == 0) {
    sbi->clu_srch_ptr = hint_clu;
    sbi->used_clusters += num_clusters;
    p_chain->size += num_clusters;
    mutex_unlock(&sbi->bitmap_lock);
    return 0;
    }
    hint_clu = new_clu + 1;
    /* ... 环回处理 ... */
    }
free_cluster:
    if (num_clusters) __exfat_free_cluster(inode, p_chain);
unlock:
    mutex_unlock(&sbi->bitmap_lock);
    return ret;
}
```

依赖关系：
- 上调（提供给上层）：`namei.c::exfat_create_upcase`、`file.c::exfat_extend_data`、`inode.c::exfat_get_block`。
- 下调（依赖的辅助）：`exfat_find_free_bitmap`、`exfat_set_bitmap`、`exfat_ent_set`、`__exfat_free_cluster`。
- Linux 基础设施依赖：`mutex_lock`（→ `LOS_MuxLock`）、`sb` / `sbi`（→ `Mount->data`）、`unlikely`（→ 直接删除或自定义为空宏）。

分层归属：操作了位图和 FAT 表 → 属于 `bitmap/` 与 `inode/` 中间层，**不在 `interface/`**。本技能将其规范放在 `fs/exfat/spec/inode/inode_alloc_cluster.spec`（`bitmap/` 内放纯位图操作如 `set_bitmap`、`find_free_bitmap`）。

## 阶段 2 — 规范

写到 `fs/exfat/spec/inode/inode_alloc_cluster.spec`：

```
[PROMPT]
Provide complete `inode_alloc_cluster.c` file that implement `exfat_alloc_cluster` operation.
You can use information first from [RELY], [GUARANTEE] and [SPECIFICATION] in the first phase,
then more information in the refine phase as described below. Only provide the implementation
of that single function and only include `inode.h` as the header. Your output is wrapped in a
C code block, and no other unrelevant code should be given.

## First Prompt

[RELY]
```c
#define EXFAT_EOF_CLUSTER 0xFFFFFFFFu
#define EXFAT_FIRST_CLUSTER 2u
#define ALLOC_FAT_CHAIN 0x01
#define ALLOC_NO_FAT_CHAIN 0x03
```
```c
struct exfat_chain {
 unsigned int dir; /* 链首簇号；EXFAT_EOF_CLUSTER 表示空 */
 unsigned int size; /* 链上簇数 */
 unsigned int flags; /* ALLOC_FAT_CHAIN 或 ALLOC_NO_FAT_CHAIN */
};
```
```c
struct exfat_sb_info {
 unsigned int num_clusters;
 unsigned int used_clusters;
 unsigned int clu_srch_ptr;
 LosMux bitmap_lock;
};
```
```c
struct exfat_sb_info *exfat_get_sbi(struct Vnode *vn); /* 取所属 Mount 的私有数据 */
unsigned int exfat_data_cluster_count(struct exfat_sb_info *sbi);
unsigned int exfat_find_free_bitmap(struct exfat_sb_info *sbi, unsigned int hint);
int exfat_set_bitmap(struct Vnode *vn, unsigned int clu, bool sync);
int exfat_ent_set(struct exfat_sb_info *sbi, unsigned int clu, unsigned int next);
int exfat_free_chain_partial(struct Vnode *vn, struct exfat_chain *chain);
int exfat_chain_cont_cluster(struct exfat_sb_info *sbi, unsigned int dir,
 unsigned int num);
bool exfat_is_valid_cluster(struct exfat_sb_info *sbi, unsigned int clu);
```

[GUARANTEE]
```c
int exfat_alloc_cluster(struct Vnode *vn, unsigned int num_alloc,
 struct exfat_chain *p_chain, bool sync_bmap);
```

[SPECIFICATION]
**Pre-Condition**:
- `vn` 是有效 inode vnode；`vn->originMount` 上挂载的是 exfat。
- `p_chain != NULL`；`p_chain->flags ∈ {ALLOC_FAT_CHAIN, ALLOC_NO_FAT_CHAIN}`。
- `num_alloc > 0`。
- 入参允许 `p_chain->dir == EXFAT_EOF_CLUSTER`（首次为该 inode 分配链）；
    也允许 `p_chain->dir` 是已有链尾簇号。

**Post-Condition**:

**Case 1（成功，返回 0）**:
- 已在位图中标记 `num_alloc` 个新簇为已用。
- 这些簇按分配顺序串成链：`p_chain->dir` 指向首簇；
    若 `p_chain->flags == ALLOC_FAT_CHAIN`，FAT 表中相邻新簇间用 `exfat_ent_set` 设值；
    若 `p_chain->flags == ALLOC_NO_FAT_CHAIN`，分配的簇必须连续，FAT 表不写入。
- `p_chain->size` 增加 `num_alloc`。
- `sbi->used_clusters` 增加 `num_alloc`。
- `sbi->clu_srch_ptr` 更新为最后一次成功分配的提示位置。

**Case 2（空间不足，返回 `-ENOSPC`）**:
- 满足以下任一即返回此码：
    a) `num_alloc > exfat_data_cluster_count(sbi) - sbi->used_clusters`；
    b) 第一次 `exfat_find_free_bitmap` 返回 `EXFAT_EOF_CLUSTER`。
- 位图、FAT 表、`sbi->used_clusters`、`p_chain` 内容均不变。

**Case 3（IO/损坏，返回 `-EIO`）**:
- 满足以下任一即返回此码：
    a) `total_cnt < sbi->used_clusters`（盘上数据自洽性损坏）；
    b) `exfat_set_bitmap` 或 `exfat_ent_set` 任一调用返回非零。
- 已分配的簇通过 `exfat_free_chain_partial` 回滚释放；位图与 FAT 表回到调用前状态。

**Invariant**:
- 调用前后 `sbi->used_clusters <= exfat_data_cluster_count(sbi)`。
- 调用前后位图中置位的 bit 数 == `sbi->used_clusters`。
- 调用前后 `p_chain->size` 等于 `p_chain->dir` 起始链上的簇数。

## Refine Prompt

[RELY]
```c
void LOS_MuxLock(LosMux *m, unsigned int timeout);
void LOS_MuxUnlock(LosMux *m);
#define LOS_WAIT_FOREVER 0xFFFFFFFFu
```

[SPECIFICATION of exfat_alloc_cluster]
**Pre-Condition**:
进入时不持有 `sbi->bitmap_lock`。

**Post-Condition**:
返回时（任意 Case 下）不持有 `sbi->bitmap_lock`。所有对位图、FAT 表、
`sbi->used_clusters`、`sbi->clu_srch_ptr`、`p_chain` 的写入都在持锁期间进行。
```

规范 LOC ≈ 60 行；下面 LiteOS-A 实现 ≈ 100 行——满足 SpecFS 生产力门槛。

## 阶段 3 — Linux→LiteOS-A 映射

| Linux | LiteOS-A | 理由 |
|---|---|---|
| `struct inode *inode` | `struct Vnode *vn` | LiteOS-A 把 inode 与 dentry 合到 Vnode |
| `inode->i_sb` | `vn->originMount` | Mount 关联挂载实例 |
| `EXFAT_SB(sb)`（即 `sb->s_fs_info`） | `(struct exfat_sb_info *)vn->originMount->data` | Mount->data 是私有指针 |
| `mutex_lock(&sbi->bitmap_lock)` | `LOS_MuxLock(&sbi->bitmap_lock, LOS_WAIT_FOREVER)` | |
| `mutex_unlock(&sbi->bitmap_lock)` | `LOS_MuxUnlock(&sbi->bitmap_lock)` | |
| `exfat_fs_error_ratelimit(...)` | `PRINT_ERR(...)` | 简化为普通错误打印 |
| `exfat_err(sb, ...)` | `PRINT_ERR(...)` | |
| `unlikely(x)` | （删除） | 不影响语义；分支预测交给编译器 |
| `__exfat_free_cluster` | `exfat_free_chain_partial` | 重命名以清晰表达"部分释放" |
| `cpu_to_le32` / `le32_to_cpu` | `LOS_LE32_TO_HOST` / `LOS_HOST_TO_LE32` | 见 `kernel/include/los_endian.h`；exFAT 是小端盘上格式 |

`sbi->bitmap_lock` 类型从 `struct mutex` 改为 `LosMux`，初始化在 `Os<Name>Mount` 中调 `LOS_MuxInit(&sbi->bitmap_lock, NULL)`。

## 阶段 4 — LiteOS-A 实现

落到 `fs/exfat/exfat_balloc.c`（取自下面规范，核心循环 ≈ 上游的精简翻译，剥除 Linux 假设）：

```c
/*
    * Copyright (c) 2024-2026 Huawei Device Co., Ltd. All rights reserved.
    * ... 完整 BSD-3-Clause 头 ...
    */

#include "exfat.h"
#ifdef LOSCFG_FS_EXFAT

#include <errno.h>
#include "securec.h"
#include "los_mux.h"
#include "los_printf.h"
#include "exfat_pri.h"

int exfat_alloc_cluster(struct Vnode *vn, unsigned int num_alloc,
    struct exfat_chain *p_chain, bool sync_bmap)
{
    struct exfat_sb_info *sbi = exfat_get_sbi(vn);
    unsigned int total_cnt = exfat_data_cluster_count(sbi);
    unsigned int num_clusters = 0;
    unsigned int hint_clu, new_clu;
    unsigned int last_clu = EXFAT_EOF_CLUSTER;
    int ret = -ENOSPC;

    if (sbi == NULL || p_chain == NULL || num_alloc == 0) {
    return -EINVAL;
    }
    if (total_cnt < sbi->used_clusters) {
    PRINT_ERR("exfat: invalid used clusters (t:%u u:%u)\n",
    total_cnt, sbi->used_clusters);
    return -EIO;
    }
    if (num_alloc > total_cnt - sbi->used_clusters) {
    return -ENOSPC;
    }

    LOS_MuxLock(&sbi->bitmap_lock, LOS_WAIT_FOREVER);

    hint_clu = p_chain->dir;
    if (hint_clu == EXFAT_EOF_CLUSTER) {
    if (sbi->clu_srch_ptr < EXFAT_FIRST_CLUSTER) {
    sbi->clu_srch_ptr = EXFAT_FIRST_CLUSTER;
    }
    hint_clu = exfat_find_free_bitmap(sbi, sbi->clu_srch_ptr);
    if (hint_clu == EXFAT_EOF_CLUSTER) {
    ret = -ENOSPC;
    goto unlock;
    }
    } else if (!exfat_is_valid_cluster(sbi, hint_clu)) {
    hint_clu = EXFAT_FIRST_CLUSTER;
    if (p_chain->flags == ALLOC_NO_FAT_CHAIN) {
    if (exfat_chain_cont_cluster(sbi, p_chain->dir, num_clusters) != 0) {
    ret = -EIO;
    goto unlock;
    }
    p_chain->flags = ALLOC_FAT_CHAIN;
    }
    }

    p_chain->dir = EXFAT_EOF_CLUSTER;

    while ((new_clu = exfat_find_free_bitmap(sbi, hint_clu)) != EXFAT_EOF_CLUSTER) {
    if (new_clu != hint_clu && p_chain->flags == ALLOC_NO_FAT_CHAIN) {
    if (exfat_chain_cont_cluster(sbi, p_chain->dir, num_clusters) != 0) {
    ret = -EIO;
    goto rollback;
    }
    p_chain->flags = ALLOC_FAT_CHAIN;
    }
    if (exfat_set_bitmap(vn, new_clu, sync_bmap) != 0) {
    ret = -EIO;
    goto rollback;
    }
    num_clusters++;
    if (p_chain->flags == ALLOC_FAT_CHAIN) {
    if (exfat_ent_set(sbi, new_clu, EXFAT_EOF_CLUSTER) != 0) {
    ret = -EIO;
    goto rollback;
    }
    }
    if (p_chain->dir == EXFAT_EOF_CLUSTER) {
    p_chain->dir = new_clu;
    } else if (p_chain->flags == ALLOC_FAT_CHAIN) {
    if (exfat_ent_set(sbi, last_clu, new_clu) != 0) {
    ret = -EIO;
    goto rollback;
    }
    }
    last_clu = new_clu;

    if (--num_alloc == 0) {
    sbi->clu_srch_ptr = hint_clu;
    sbi->used_clusters += num_clusters;
    p_chain->size += num_clusters;
    LOS_MuxUnlock(&sbi->bitmap_lock);
    return 0;
    }
    hint_clu = new_clu + 1;
    if (hint_clu >= sbi->num_clusters) {
    hint_clu = EXFAT_FIRST_CLUSTER;
    if (p_chain->flags == ALLOC_NO_FAT_CHAIN) {
    if (exfat_chain_cont_cluster(sbi, p_chain->dir, num_clusters) != 0) {
    ret = -EIO;
    goto rollback;
    }
    p_chain->flags = ALLOC_FAT_CHAIN;
    }
    }
    }

rollback:
    if (num_clusters > 0) {
    (void)exfat_free_chain_partial(vn, p_chain);
    }
unlock:
    LOS_MuxUnlock(&sbi->bitmap_lock);
    return ret;
}

#endif /* LOSCFG_FS_EXFAT */
```

## 阶段 5 — 接线（同此函数相关的最小子集）

### `fs/exfat/include/exfat.h`（节选）

```c
#ifdef LOSCFG_FS_EXFAT

#define EXFAT_EOF_CLUSTER 0xFFFFFFFFu
#define EXFAT_FIRST_CLUSTER 2u
#define ALLOC_FAT_CHAIN 0x01
#define ALLOC_NO_FAT_CHAIN 0x03

struct exfat_chain {
    unsigned int dir;
    unsigned int size;
    unsigned int flags;
};

int exfat_alloc_cluster(struct Vnode *vn, unsigned int num_alloc,
    struct exfat_chain *p_chain, bool sync_bmap);

#endif
```

### `fs/exfat/BUILD.gn`（仅与本函数相关条目）

```gn
sources = [
    /* ... */
    "exfat_balloc.c",
    "exfat_fatent.c",
    /* ... */
]
```

### 单元测试（`testsuites/unittest/fs/exfat/It_exfat_alloc_cluster_001.cpp` 雏形）

```cpp
static UINT32 testCase(VOID)
{
    struct Vnode *vn = TestExfatGetMountedVnode();
    struct exfat_chain chain = { EXFAT_EOF_CLUSTER, 0, ALLOC_FAT_CHAIN };
    int ret;

    ret = exfat_alloc_cluster(vn, 4, &chain, true);
    ICUNIT_GOTO_EQUAL(ret, 0, ret, EXIT);
    ICUNIT_GOTO_NOT_EQUAL(chain.dir, EXFAT_EOF_CLUSTER, chain.dir, EXIT);
    ICUNIT_GOTO_EQUAL(chain.size, 4u, chain.size, EXIT);

    /* 释放 */
    (void)exfat_free_chain_partial(vn, &chain);
    return LOS_OK;
EXIT:
    return LOS_NOK;
}
```

## 关键复盘

1. **规范行数 ≈ 60 行；实现行数 ≈ 100 行**——保持了 SpecFS 的生产力命题。
2. **加锁单独 refine**——第一轮规范完全不提锁，第二轮锁规范只描述边界状态，没有重复功能行为。
3. **三种返回路径在规范中显式列出**（成功 / -ENOSPC / -EIO），实现里 `unlock` 与 `rollback` 标签分别对应。
4. **`p_chain->size` 不变式**写在 Invariant 段；实现里在最后一刻才更新 `p_chain->size`，这是规范驱动出来的——上游 Linux 也是同样写法，但 LiteOS-A 实现不是因为照搬而是因为不变式要求。
5. **错误回滚是规范要求**——Linux 实现里 `goto free_cluster` 调 `__exfat_free_cluster`；LiteOS-A 端依规范保留了同样的回滚（命名为 `exfat_free_chain_partial`），不是"觉得应该回滚"的随手补丁。

照本范式处理 `exfat_set_bitmap`、`exfat_clear_bitmap`、`exfat_find_free_bitmap` 等位图原语，填满 `fs/exfat/spec/bitmap/` 即可完成 bitmap 层；其它层同理。
