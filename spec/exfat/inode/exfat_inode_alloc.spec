[PROMPT]
exfat_inode_alloc —— `exfat_inode_info` 内存生命周期 helper。把 mount 流程
内联的 root-inode init 提炼为公共三件套，让后续 lookup / readdir / open
直接复用，避免散落的 `LOS_MemAlloc + LOS_MuxInit` 序列：

1. `exfat_inode_alloc(out)` —— `zalloc(sizeof(exfat_inode_info))` 后初始化
   `inode_lock` (PRIO_INHERIT 协议，避免单 inode 锁优先级反转)；其余字段
   保持 zalloc 的 0 初始状态，由具体场景调用方再填 (root mount / lookup / open)。
2. `exfat_inode_free(ei)` —— 销毁 `inode_lock` 后释放堆。**幂等且 NULL-safe**：
   `ei == NULL` 直接返回；多次调用同一指针只生效一次（语义为 "可信调用"，
   不强制运行期 double-free 检测，与 `exfat_free_bitmap` / `exfat_free_upcase_table`
   同策略）。
3. `exfat_inode_init_dir_chain(ei, start_clu)` —— 仅写 `ei->dir.dir = start_clu;
   ei->dir.flags = ALLOC_FAT_CHAIN; ei->dir.size = 0; ei->type = TYPE_DIR;
   ei->start_clu = start_clu;` 五个字段。lookup/readdir 在落到 vnode->data 前
   集中调用一次，避免散点 init。

锁用 `LOS_MuxInit` (LosMux，可睡眠路径)；协议 `LOS_MUX_PRIO_INHERIT` (用户
约定)。无 IO，无盘 read/write。返回负 POSIX errno (VFS 边界规约)。

[RELY]
```c
/* common.header 已声明的类型与常量 (exfat_inode_info、exfat_chain) 此处不重列 */

/* 内核内存 (kernel/include/los_memory.h) */
extern VOID  *zalloc(UINT32 size);
extern UINT32 LOS_MemFree(VOID *pool, VOID *ptr);
extern UINT8 *m_aucSysMem0;

/* 锁 (kernel/include/los_mux.h) — PRIO_INHERIT 协议 */
typedef enum {
    LOS_MUX_PRIO_NONE       = 0,
    LOS_MUX_PRIO_INHERIT    = 1,
    LOS_MUX_PRIO_PROTECT    = 2,
} LosMuxProtocol;
typedef struct {
    UINT8 protocol;       /* LOS_MUX_PRIO_* */
    UINT8 prioceiling;
    UINT8 type;
    UINT8 reserved;
} LosMuxAttr;
extern UINT32 LOS_MuxAttrInit(LosMuxAttr *attr);
extern UINT32 LOS_MuxAttrSetProtocol(LosMuxAttr *attr, INT32 protocol);
extern UINT32 LOS_MuxAttrDestroy(LosMuxAttr *attr);
extern UINT32 LOS_MuxInit(LosMux *mutex, const LosMuxAttr *attr);
extern UINT32 LOS_MuxDestroy(LosMux *mutex);
#define LOS_OK  0u

/* 日志 */
extern void PRINT_ERR(const char *fmt, ...);

/* exfat_raw.h */
#define ALLOC_FAT_CHAIN     1u
#define ALLOC_NO_FAT_CHAIN  3u

/* TYPE_* 来自 exfat.h (TYPE_DIR、TYPE_FILE) — 已在 frozen contract */
```

[GUARANTEE]
```c
/*
 * 调用约定:
 * - 无 IO；仅 zalloc + LOS_MuxAttr* + LOS_MuxInit。可能睡眠 (zalloc 路径)；
 *   **禁止在持自旋锁时调用** (与 fat_chain 同等级)。
 * - 调用方传入 `out` 用于接收新分配指针；成功 → *out 写入；失败 → *out 不写
 *   (调用方传入的 *out 不被污染)。
 * - 成功 (=0) 后字段状态：
 *     ei->inode_lock 已 LOS_MuxInit (PRIO_INHERIT 协议)，可立即 LOS_MuxLock；
 *     其余所有字段为 0 (zalloc) — 包括 entry=0、type=0、attr=0、start_clu=0
 *     等；调用方 (lookup/mount-root) 须在 vnode->data 暴露前完成填充。
 * - 失败：返回 -ENOMEM (zalloc 失败) 或 -EIO (LOS_MuxInit 失败，理论不应发生
 *   但守规约)；任一失败路径下不留已分配 heap (Invariant exfat-inode-alloc-leak-free)。
 */
int exfat_inode_alloc(exfat_inode_info **out);

/*
 * 调用约定:
 * - 无 IO；调 LOS_MuxDestroy 后 LOS_MemFree。可能睡眠；**禁止在持自旋锁时调用**。
 * - **NULL-safe**: ei == NULL 直接返回，无副作用。
 * - 成功仅返回 (无错误码)；调用方不需检查返回。
 * - 副作用：销毁 ei->inode_lock；释放 ei 所占堆。调用方应在调用后将持有该指针
 *   的所有变量置 NULL (本函数内不操作调用方变量)。
 * - 安全要求：调用方必须保证 ei->inode_lock 当前**未被任何线程持有**——本函数
 *   不验证该前置。违反引发 LiteOS 调度异常 (与 fat / jffs2 mux 释放同语义)。
 */
void exfat_inode_free(exfat_inode_info *ei);

/*
 * 调用约定:
 * - 纯赋值，无 IO/无 alloc/无锁。spinlock-safe (与 fat_chain 的 clu_to_sector
 *   同等级)。
 * - 调用方 (mount root / lookup dir entry) 已经持有 ei，且本对象**尚未** vnode-
 *   visible。因此无并发；无须持锁。
 * - start_clu ≥ EXFAT_FIRST_CLUSTER；调用方负责该范围校验 (本函数不校验)。
 * - 仅修改 dir.dir / dir.flags / dir.size / type / start_clu 五个字段；其它
 *   字段不动。
 */
void exfat_inode_init_dir_chain(exfat_inode_info *ei, uint32_t start_clu);
```

[SPECIFICATION]

**Pre-Condition (exfat_inode_alloc)**:
out != NULL；调用方未持任何 LiteOS spinlock。

**Post-Condition (Case 1: success)**:
返回 0；*out 写入新分配的 exfat_inode_info 指针；该结构体满足：
- 全部字段初始为 0 (zalloc)。
- inode_lock 经 LOS_MuxInit 初始化，protocol == LOS_MUX_PRIO_INHERIT。
- 任意线程可立即 LOS_MuxLock 该锁，行为符合 PRIO_INHERIT 协议。

**Post-Condition (Case 2: zalloc failure)**:
zalloc 返回 NULL → 返回 -ENOMEM；*out 不写；无 LOS_MuxInit 调用 (短路)。

**Post-Condition (Case 3: LosMuxAttrInit / SetProtocol failure)**:
LOS_MuxAttrInit 或 LOS_MuxAttrSetProtocol 返回非 LOS_OK → 返回 -EIO；
zalloc 出来的内存已 LOS_MemFree；*out 不写。该路径在 LiteOS 当前实现下
不会触发，但规约预留。

**Post-Condition (Case 4: LOS_MuxInit failure)**:
LOS_MuxInit 返回非 LOS_OK → 返回 -EIO；zalloc 出来的内存已 LOS_MemFree；
attr 已通过 LOS_MuxAttrDestroy 销毁；*out 不写。

**Pre-Condition (exfat_inode_free)**:
ei == NULL，或 ei 由 exfat_inode_alloc 分配且 inode_lock 当前未被持有；
调用方未持任何 LiteOS spinlock。

**Post-Condition (Case 1: NULL input)**:
ei == NULL → 直接返回；无副作用；无 IO/无 alloc/无锁动作。

**Post-Condition (Case 2: normal free)**:
ei != NULL → LOS_MuxDestroy(&ei->inode_lock) 调用一次 (返回值忽略)，随后
LOS_MemFree(m_aucSysMem0, ei) 释放堆。本函数不读 ei 任何字段除 inode_lock
之外；不写 ei 任何字段。

**Pre-Condition (exfat_inode_init_dir_chain)**:
ei != NULL；start_clu ≥ EXFAT_FIRST_CLUSTER。

**Post-Condition**:
ei->dir.dir = start_clu；
ei->dir.flags = ALLOC_FAT_CHAIN；
ei->dir.size = 0；
ei->type = TYPE_DIR；
ei->start_clu = start_clu；
其余字段保持调用前值。

**Invariant** (id=exfat-inode-alloc-leak-free):
exfat_inode_alloc 任一失败路径 (Case 2/3/4) 下不留已分配 heap。具体：
- Case 2 (zalloc 失败)：无 alloc，自然无残留。
- Case 3 (Attr 阶段失败)：zalloc 出来的内存已 LOS_MemFree。
- Case 4 (MuxInit 失败)：zalloc 出来的内存已 LOS_MemFree；attr 已 destroy。

**Invariant** (id=exfat-inode-alloc-prio-inherit):
LosMuxAttr 必须用 LOS_MuxAttrInit 创建后调用 LOS_MuxAttrSetProtocol
(LOS_MUX_PRIO_INHERIT) 显式设置。**绝不**直接对 attr 字段赋值；**绝不**
传 NULL 给 LOS_MuxInit。该 invariant 由 user clarification 直接驱动
(优先级反转防御策略)。

**Invariant** (id=exfat-inode-alloc-zero-init):
exfat_inode_info 内除 inode_lock 外的所有字段在 exfat_inode_alloc 返回时
保证为 0 (由 zalloc 提供)。调用方读取未填字段 (例如 size / start_clu)
读到 0 是 well-defined 行为，而非 use-of-uninitialized。

**Invariant** (id=exfat-inode-alloc-no-io):
本层任何函数都不调用 los_part_read / los_part_write / los_disk_read /
los_disk_write。纯内存生命周期模块。

**Invariant** (id=exfat-inode-free-idempotent-null-safe):
exfat_inode_free(NULL) 是 well-defined no-op；不引发崩溃；不读写任何全局。
**注意**: 对同一非 NULL 指针多次调用是 use-after-free，**不**被本 invariant
保护——调用方负责析构后置 NULL。

**Invariant** (id=exfat-inode-init-dir-chain-pure):
exfat_inode_init_dir_chain 不调 IO/不获取锁/不分配/释放堆。仅 5 个字段写入；
spinlock-safe。

**Invariant** (id=exfat-inode-no-spinlock-callsite-alloc-free):
exfat_inode_alloc 内含 zalloc + LOS_MuxInit (内部可能动态分配等待队列)，
exfat_inode_free 内含 LOS_MemFree + LOS_MuxDestroy。两者**禁止在持自旋锁
时调用** (与 fat_chain 的 no-spinlock-callsite invariant 同精神)。
exfat_inode_init_dir_chain 例外，纯赋值。

**Invariant** (id=exfat-inode-alloc-vnode-not-visible):
exfat_inode_alloc 返回的 ei 在调用方将其挂到 vnode->data 之前是**私有**的；
本函数本身不操作 vnode 任何字段；不参与 hash 链；不与 inode_hash_lock
交互。这是 mount 阶段 invariant exfat-mount-vnode-visible-after-init
的延伸——helper 层不暴露 inode，暴露责任在 interface/ 层。
