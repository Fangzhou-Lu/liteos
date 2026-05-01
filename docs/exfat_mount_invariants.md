# exFAT mount 全部 invariants 契约说明

本文档列举 7 个 stage 全部 **36 个 invariants** 的详细契约：含义、动机、
违反检测方法。这是后续 后续 修改的硬约束——任何破坏这些契约的 PR 必须开
对应 stage 的 `spec_gen_refine` 走显式协商，不能默默改实现。

---

## 1. 索引

按 stage 分组的 invariant ID 速查表：

| Stage | invariants 数 | IDs |
|---|---|---|
| chksum | 4 | purity / byte-order-independence / not-iee-crc / skip-positions-fixed |
| options | 5 | no-globals-no-io / defaults-preserved / strict-key-rejection / no-heap-leak / octal-mask-bit-width |
| dentry | 6 | parse-no-io / parse-byte-order / find-buf-leak-free / find-readonly / find-no-locks / fat-traversal-bounded |
| balloc | 6 | load-leak-free / free-idempotent / count-mem-only / bitmap-size-policy / no-write / bitmap-tail-mask |
| upcase | 5 | no-write / checksum-verified / byte-order / free-idempotent / leak-free（外加 strict-no-fallback）|
| vfs_ops_stub | 4 | symbol-stable / null-trap / no-runtime-init / loscfg-gated |
| mount | 5 | readonly-during-mount / rollback-lifo / mount-data-self-consistent / vnode-visible-after-init / fsmap-entry-name |

注：完整 ID 形如 `exfat-<stage>-<short>`。文档正文用短名+stage前缀。

---

## 2. chksum

### exfat-chksum-purity

**契约**：两函数无副作用，不读写全局变量，不调用 IO/分配器/锁。

**动机**：纯函数性质保证可在任何上下文调用——lookup 在持锁环境下也能用，
后续 写路径在 IRQ 关闭时也能用。

**违反检测**：grep `static.*=` / `LOS_MemAlloc` / `LOS_Mux` / `los_part_*` 在
`fs/exfat/util/exfat_chksum.c` 应返 0。

### exfat-chksum-byte-order-independence

**契约**：算法按字节迭代，不依赖宿主机字节序——同一字节缓冲在 LE/BE host 上得到相同结果。

**动机**：与 Microsoft exFAT spec 兼容性的硬约束。BE port 时不需重写。

**违反检测**：grep `__builtin_bswap` / `LE32_TO_HOST` 在 chksum.c 应返 0。

### exfat-chksum-not-iee-crc

**契约**：**不得**实现为 IEEE 802.3 多项式 0x04C11DB7 的标准 CRC32（即不得调用
`LOS_Crc32` 或等价库函数）。算法固定为 1-bit ROR + 8-bit byte add。

**动机**：违反此约束会通不过 boot region 校验，**格式合规的 exFAT 卷无法挂载**。
评测集 eval-3 (EROFS) 中 baseline 就漏检 CRC32 vs CRC32c 不兼容。

**违反检测**：grep `LOS_Crc32` 或 `0x04C11DB7` 在 chksum.c 应返 0。

### exfat-chksum-skip-positions-fixed

**契约**：跳过位置由 Microsoft spec 严定：
- chksum32 + CS_BOOT_SECTOR：byte offsets {106, 107, 112}
- chksum16 + CS_DIR_ENTRY：byte offsets {2, 3}

**动机**：vol_flags（106-107）与 percent_in_use（112）是 boot sector 的可变
字段，必须从校验中排除；dentry chksum 字段（2-3）自身不参与校验。

**违反检测**：grep `i ==` 在 chksum.c 应只匹配 `106`/`107`/`112`/`2`/`3`。

---

## 3. options

### exfat-options-no-globals-no-io

**契约**：解析过程不读写任何全局变量、不调用 IO 接口、不进行内存分配（iocharset
设为静态字符串字面量地址）。

**动机**：mount 路径在 sbi 暴露前调用 parse_options——若分配堆内存，失败回滚
路径必须释放，复杂度上升。

**违反检测**：`g_iocharset_utf8` 应是 `static char` 数组（rodata），grep
`zalloc\|LOS_MemAlloc\|sb_bread\|los_part_read` 在 options.c 应返 0。

### exfat-options-defaults-preserved

**契约**：未在 `data` 中显式出现的 key，其对应字段在 opts 中保持调用方写入的
默认值不变。

**动机**：与 mount.spec Step 4 协作的硬约束——mount 已写好默认，parser 只覆盖显式项。

**违反检测**：单元测试 `exfat_parse_options(NULL, &opts)` 应返 0 且 opts 完全不变。

### exfat-options-strict-key-rejection

**契约**：未识别的 key 一律返回 -EINVAL，绝不"宽容跳过"。即使 Linux 历史
deprecated 项（utf8/debug/namecase/codepage）也不静默接受。

**动机**： 简化兼容面；后续若需要 deprecated key no-op，必须开 spec_gen_refine。

**违反检测**：单元测试 `exfat_parse_options("foo=1", &opts)` 应返 -EINVAL。

### exfat-options-no-heap-leak

**契约**：失败路径上不留下任何已分配的堆内存。 解析全程不调用 zalloc/LOS_MemAlloc——
iocharset 直接指向静态字符串 "utf8" 的地址（在 .rodata 段）。

**动机**：调用方在 sbi 释放时**不**调用 LOS_MemFree——简化错误回滚。

**违反检测**：`opts->iocharset` 应是 `&g_iocharset_utf8[0]`，从不指向堆。

### exfat-options-octal-mask-bit-width

**契约**：所有 mask 字段（umask/fmask/dmask/allow_utime）按 9-bit 解析（最大 0777）。

**动机**：与 POSIX 文件权限位一致，拒绝高位设置避免误用。

**违反检测**：单元测试 `exfat_parse_options("umask=01000", &opts)` 应返 -ERANGE。

---

## 4. dentry

### exfat-dentry-parse-no-io

**契约**：parse_boot_sector 不调用任何 IO 接口。仅读 *bs，写 sbi。

**动机**：mount step 7 调用前已经读了 boot sector；parse 是纯解析阶段。

**违反检测**：grep `los_part_read` 在 parse_boot_sector 函数体应返 0。

### exfat-dentry-parse-byte-order

**契约**：所有从 `bs` 读取的多字节字段都经 `LE*_TO_HOST` 转换。直接 `bs->vol_length`
等用法被禁止。

**动机**：BE host 兼容性的硬约束。

**违反检测**：parse_boot_sector 函数体内只允许 `bs->X` 模式访问 `__u8` 字段；
多字节字段必须 `LE16_TO_HOST(bs->X)`、`LE32_TO_HOST(...)` 等。

### exfat-dentry-find-buf-leak-free

**契约**：find_root_dentry 在所有返回路径（成功 + 4 个失败 case）下，由本函数
LOS_MemAlloc 分配的临时缓冲（cluster buf + fat sector buf）都已 LOS_MemFree。
**严格反 LIFO**：先释放 fat_buf，再释放 clu_buf。

**动机**：mount 路径分配较多临时缓冲，遗漏一次释放就持续泄漏。

**违反检测**：valgrind / sanitizer / 代码审查；每个 `LOS_MemAlloc` 必须在所有
出口路径有对应 `LOS_MemFree`。

### exfat-dentry-find-readonly

**契约**：find_root_dentry 不调用 los_part_write/los_disk_write，不修改 sbi 任何字段。
只读语义。

**动机**：mount path 的 invariant 链要求子函数纯读——破坏会让 mount 失败回滚困难。

**违解检测**：grep `los_part_write\|los_disk_write\|sbi->.*=` 在 find_root_dentry
应返 0（除局部变量初始化外）。

### exfat-dentry-find-no-locks

**契约**：find_root_dentry 不获取/释放/检查任何锁。调用方（mount 路径）保证本函数
被调用时 sbi 尚未对外可见，没有锁竞争问题。

**动机**：后续 中 lookup 也会用 find dentry 类函数，那时需要锁；本 的
find_root_dentry 是 mount 专用版本，不引入锁。

**违反检测**：grep `LOS_MuxLock\|LOS_SpinLock` 在 find_root_dentry 应返 0。

### exfat-dentry-fat-traversal-bounded

**契约**：FAT 链遍历有上界保护：`max_iter = sbi->num_clusters`；超过即返回 -EIO。

**动机**：防止恶意/损坏卷构造 FAT 环导致内核无限循环。

**违反检测**：find_root_dentry 函数体内 `while` 循环必须有 `iter++` 与
`iter < num_clusters` 检查。

---

## 5. balloc

### exfat-balloc-load-leak-free

**契约**：load_bitmap 失败路径下不留下任何已分配 heap。具体：vol_amap 在分配后若
IO 失败必须 LOS_MemFree 并置 NULL。

**动机**：mount 失败回滚依赖 vol_amap == NULL 判定是否需要释放。

**违反检测**：load_bitmap 的所有失败 return 路径前必须有 `vol_amap = NULL`。

### exfat-balloc-free-idempotent

**契约**：free_bitmap 可重复调用、可对未加载 sbi 调用，结果一致：vol_amap → NULL。

**动机**：mount 失败回滚 + umount 双调用安全。

**违反检测**：`free_bitmap(NULL)` 与 `free_bitmap(sbi); free_bitmap(sbi)` 都不崩。

### exfat-balloc-count-mem-only

**契约**：count_used_clusters 不调 IO，不获取锁，不修改 vol_amap 内容。纯计算。

**动机**：mount step 12 在 sbi 暴露前调用——避免提前引入锁需求。

**违反检测**：grep `los_part_read\|LOS_MuxLock\|sbi->vol_amap\[.*\]\s*=` 应返 0。

### exfat-balloc-bitmap-size-policy

**契约**：size 策略与 Linux 等价：
- `need_map_size > map_size`：硬错（-EIO）
- `need_map_size < map_size`：PRINT_WARN，继续
- 等于：直接成功

**动机**：允许格式化工具的 size padding 而拒绝结构上小到放不下的位图。

**违反检测**：单元测试三种 size 关系应返对应结果。

### exfat-balloc-no-write

**契约**：本层任何函数都不调用 los_part_write / los_disk_write。set_bitmap、
clear_bitmap 留待 后续 写路径阶段实现。

**动机**： 的 read-only 保证。

**违反检测**：grep `los_part_write\|los_disk_write` 在 exfat_balloc.c 应返 0。

### exfat-balloc-bitmap-tail-mask

**契约**：count_used_clusters 在最后一个字节用 `(1u << tail) - 1u` 掩码屏蔽超出
data_cluster_count 的位，避免计入 bitmap padding 的 set 位。

**动机**：bitmap 末尾常含 padding 位（来自格式化工具），不掩码会高估 used count。

**违反检测**：单元测试人为构造 bitmap 末尾全 1 的卷，count 应等于 data_cluster_count。

---

## 6. upcase

### exfat-upcase-no-write

**契约**：不调用 los_part_write / los_disk_write。upcase 在 mount 仅读盘。

**违反检测**：grep `los_part_write\|los_disk_write` 在 exfat_upcase.c 应返 0。

### exfat-upcase-checksum-verified

**契约**：任何写入 sbi->vol_utbl 之前必须 chksum 校验通过。chksum mismatch 路径
绝不保留盘数据到 sbi。

**动机**：upcase 表损坏会导致大小写比较错误，进而 lookup 查不到文件——尽早失败。

**违反检测**：mismatch 单元测试应返 -EINVAL，且 vol_utbl 保持 NULL。

### exfat-upcase-byte-order

**契约**：upcase 表每 entry 是 LE16；本 在 LE host 上 reinterpret cast 不动数据；
若未来端口到 BE host，本函数必须改为逐 entry `LE16_TO_HOST`。

**动机**：当前实现明确依赖 LE host 假设——通过 `__LITEOS_A__` 宏（ARM-LE）保证。

**违反检测**：BE host 上必须重做该函数（将来 后续+ 工作）。

### exfat-upcase-free-idempotent

**契约**：free_upcase_table 可重复调用、可对未加载 sbi 调用，结果一致：vol_utbl → NULL。

### exfat-upcase-leak-free

**契约**：失败路径下不留下任何已分配 heap。

### exfat-upcase-strict-no-fallback

**契约**： 不退回内置默认 upcase 表。chksum mismatch / dentry 缺失 / IO 失败都
直接返回错误，导致 mount 失败。

**动机**： 选择此严格策略以避免引入 128KB 静态默认表的体积代价。

**未来变更**：后续 可能引入 fallback 选项作为 mount option，但**不破坏此 invariant**——
应通过新 invariant `exfat-upcase-fallback-via-option` 表达，旧 invariant 升级为
"默认严格"。

---

## 7. vfs_ops_stub

### exfat-vfs-stub-symbol-stable

**契约**：后续阶段（lookup/read/...）通过 spec_gen_refine 修改字段初值，但绝
不重命名符号、不改变作用域、不拆分到多个变量。

**动机**：mount.spec exports `g_exfatVops, g_exfatFops` 的 ABI 锚点。

**违反检测**：每次 后续 完成 review 时确认 nm OHOS_Image 中两符号地址未变身份。

### exfat-vfs-stub-null-trap

**契约**： 阶段所有字段为 NULL；任一 vfs 回调被调用即陷入 -ENOSYS。

**动机**：mount 之后任何 lookup/read/write 调用都会优雅失败而非未定义行为。

**违反检测**：`exfat_ops.c` 中两表都是 `{ 0 }`。spec_gen_refine 可填具体字段，
但**填错字段名会编译错**（GCC 检测）。

### exfat-vfs-stub-no-runtime-init

**契约**：不需要在 mount 路径或其他位置调用任何 init 函数初始化这两个表；
它们是编译期常量。

**动机**：避免 mount 路径对 vfs_ops 表有依赖——保持 mount stage 与 ops stage 独立。

**违反检测**：grep `g_exfatVops\.*=\|g_exfatFops\.*=` 在 exfat_super.c 应返 0。

### exfat-vfs-stub-loscfg-gated

**契约**：两表的定义包在 `#ifdef LOSCFG_FS_EXFAT ... #endif` 内。

**动机**：与 mount.spec exfat-mount-fsmap-entry-name 配套——LOSCFG 关闭时整个
fs/exfat/ 不参与链接。

---

## 8. mount

### exfat-mount-readonly-during-mount

**契约**：mount 路径自身**永不调用** `los_part_write` / `los_disk_write`。
即使卷被以可写方式挂载，写路径在 Mount 之外的回调中触发。

**动机**：保护 mount 失败回滚时不留下脏数据；后续阶段若新增 mount-time 写入需
开新阶段重审。

**违反检测**：grep `los_part_write\|los_disk_write` 在 VfsExfatMount/VfsExfatUnmount
函数体应返 0。

### exfat-mount-rollback-lifo

**契约**：所有失败分支都按反 LIFO 顺序释放：

```
ERROR_HASH → VnodeFree → ERROR_VNODE
ERROR_VNODE → MuxDestroy(inode_lock) → ERROR_INODE_LOCK
ERROR_INODE_LOCK → MemFree(inode_info) → ERROR_INODE_ALLOC
... 一直反推到 ERROR_OPTS
最后 return -ret
```

**动机**：内部用正值 errno、最终 `return -ret` —— 与 fatfs_mount 一致的 goto-stack
风格。LIFO 保证不会跨过某资源直接释放更深层。

**违反检测**：审查 exfat_super.c::VfsExfatMount 末尾标签序列与资源分配顺序的对应。

### exfat-mount-mount-data-self-consistent

**契约**：成功路径下：

```c
mount->data == sbi
&& mount->vnodeCovered != NULL
&& mount->vnodeCovered->originMount == mount
```

**动机**：双向自洽是 umount 路径的前提——可从 mount 反查 sbi，从 vnode 反查 mount。

**违反检测**：mount 后立即 `assert(mount->data && mount->vnodeCovered &&
mount->vnodeCovered->originMount == mount)`。

### exfat-mount-vnode-visible-after-init

**契约**：vnode 经 `VfsHashInsert` 暴露之前，其 `vp->fop`、`vp->data`、
`vp->data->inode_lock`、sbi 的所有锁均已 `LOS_MuxInit`。读 vnode 的并发线程不会
观察到半初始化状态。

**动机**：避免半初始化窗口——一旦 vnode 经 hash 表暴露，任何线程都可能持锁访问；
此前所有锁必须已 init。

**违反检测**：审查 VfsExfatMount 中 VfsHashInsert 调用前的 `LOS_MuxInit` 序。

### exfat-mount-fsmap-entry-name

**契约**：生成的 exfat_super.c 在文件作用域包含

```c
FSMAP_ENTRY(exfat_fsmap, "exfat", g_exfatMountOps, FALSE, TRUE);
```

**动机**：mount 命名空间将字符串 "exfat" 映射到 g_exfatMountOps。零运行时注册——
链接器表机制（CLAUDE.md "Link-time tables" 一节）。

**违反检测**：`nm OHOS_Image | grep exfat_fsmap` 必须有输出，且符号在
`.liteos.table.fsmap.data` 段。

---

## 9. invariant 维护规约

### 9.1 修改 invariant

**只允许**：
- 添加新 invariant（新 unique ID）
- 标记旧 invariant deprecated（但保留 ID 与文本，加 `**Deprecated since vN**` 注释）
- 在不变 ID 下扩充契约描述（细化）

**绝对禁止**：
- 删除旧 invariant
- 改名旧 invariant ID
- 在旧 ID 下重新定义不兼容契约

### 9.2 后续 添加 invariant 时

须在本文档对应 stage 节追加新 ID，并在 `commit_node.py spec` 时通过 `--exports`
更新 DAG。

### 9.3 PR 审查 checklist

- [ ] 所有 mount invariant 在 PR 后**仍然成立**
- [ ] 若违反某 invariant，必须有显式 spec_gen_refine 决议（DAG 中 stage 标
 dirty + 新版本 vN 启用）
- [ ] 新增 invariant 在本文档登记

---

## 10. 引用

- 各 stage spec：`spec/exfat/<sub>/<stage>.spec`
- DAG 状态：`spec/exfat/.specfs.dag.json`
- 实现：`fs/exfat/*.c`、`fs/exfat/include/*.h`
- 演化策略：`docs/exfat_roadmap.md`
- 设计原理：`docs/specfs_plugin_design.md` §4.3 "DAG 状态模型"
