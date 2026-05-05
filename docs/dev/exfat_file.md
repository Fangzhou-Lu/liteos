# exFAT Wave B 文件 IO + Truncate 开发流程实录

本文档记录 Wave B 中文件数据读写（Stage 1 write）与截断操作（Stages 2d/2e/3
truncate）的 spec-first 移植：从 in-place 覆盖写到 truncate-extend / truncate-shrink
双向截断，再到暴露给 VFS 的 Truncate VOP 入口。沿用
`docs/dev/exfat_mount.md` / `docs/dev/exfat_read.md` 的"输入 / ask-first /
模块依赖 / 多层防御 / 量化清单"框架；本文专注文件 IO 层独有的关注点：
**in-place 写的 RMW 粒度与 inode_lock 折映、truncate 的 vol_flags 括起与
FAT→bitmap 操作顺序、extend 与 shrink 的对称差异**。

---

## 1. 输入与目标

### 1.1 起始基线

| 项 | 值 |
|---|---|
| 上一波末态 | Wave A read 路径完成（mount → ls → cat → umount 全过）|
| Wave B Module 层 | Stage 2a vol_flags / Stage 2b free_cluster / Stage 2c alloc_cluster 全部 approved（`d326bc46` / `f450e5d3` / `3e5af29c`）|
| 已就绪 Fop 槽 | open / close / read / seek（Wave A Stage 8/9）；write 为 NULL |
| 已就绪 Vop 槽 | Lookup / Reclaim / Opendir / Readdir / Closedir / Rewinddir / Getattr；Truncate/Truncate64 为 NULL |
| 实现文件分布 | 2026-05-05 重构后：readdir / read / write / truncate 全部合并入 `fs/exfat/exfat_file.c`（1,536 行）|
| 当前分支 | `feature/spec-port` |
| 远端构建主机 | `192.168.1.15` `/mnt/work/openharmony` |

### 1.2 出口标准

1. `VfsExfatWrite` 注入 `g_exfatFops.write`；QEMU 验证 in-place 覆写持久化。
2. `exfat_truncate_extend` / `exfat_truncate_shrink` 两个 helper spec/code/tests 三层各自闭环。
3. `VfsExfatTruncate` / `VfsExfatTruncate64` 接入 `g_exfatVops`；QEMU 验证
   `O_TRUNC` 写空文件返回 0。
4. spec/code 比 ≤ 1.5（全 4 stage 总体）。
5. cmocka host 全套 0 failure（本 doc 涉及 4 stage 共 77 个 testpoint，全套累计 352）。
6. Layer 1 cclsp + Layer 2 OHOS clang kernel build 双通。

---

## 2. Stage 序列与依赖

| # | Stage | Linux 对照 | 主要输出 | commit |
|---|---|---|---|---|
| B1 | **write** | `generic_file_write_iter` → `exfat_get_block(create=0)` | `VfsExfatWrite`（in-place overwrite）| `8e3622f8` |
| B2d | **truncate-extend** | `exfat_cont_expand` + `exfat_map_new_buffer` lazy-alloc | `exfat_truncate_extend(sbi, ei, new_size)` | `128ebde1` |
| B2e | **truncate-shrink** | `__exfat_truncate` shrink 路径 | `exfat_truncate_shrink(sbi, ei, new_size)` | `dea5226c` |
| B3 | **truncate-vop** | `__exfat_truncate` + `exfat_setattr` dispatcher | `VfsExfatTruncate` / `VfsExfatTruncate64` | `36fe4a10` |

**依赖关系**：

- B1 write 依赖 Wave A 的 `exfat_get_next_cluster`（FAT 链走查）和 `exfat_inode_info`
  的 `inode_lock`（Wave A Stage 2 inode_alloc）。
- B2d truncate-extend 依赖 B2c `exfat_alloc_cluster` + B2a `exfat_set_volume_dirty` /
  `exfat_clear_volume_dirty` + B2a `exfat_ent_set`（链接旧尾到新头）。
- B2e truncate-shrink 依赖 B2b `exfat_free_cluster` + B2a vol_flags + B2a `exfat_ent_set`
  （截链）。
- B3 truncate-vop 依赖 B2d + B2e，是纯 dispatcher，无新 IO 逻辑。
- B1 与 B2d/B2e **互不依赖**——write 不分配簇，truncate-extend 不覆写数据块。

---

## 3. Loop A × 4：ask-first 决策与 spec 设计

### 3.1 Stage B1 write — RMW 粒度与锁模型

**ask-first 核心题**（plugin 在 read spec 已有基线，B1 弹出 3 个具化问题）：

**Q1：越过 ei->size 的写怎么处理？**
- 选项：(a) 分配新簇扩展文件 / **(b) clamp 到 ei->size，短写返回 N** ← 用户选 /
  (c) 返回 -EFBIG
- 答 (b)：B1 只做 in-place；越过 size 的 byte 不分配、不归零、不写盘。
  Invariant `exfat-write-clamp-by-size` + `exfat-write-no-extend`。
  Wave B2 truncate-extend 解除本约束。

**Q2：IO 单元是 sector 还是 cluster？**
- 选项：(a) **cluster-level RMW（推荐，简单）** ← 用户选 / (b) sector-level RMW
  （性能更优）
- 答 (a)：`los_part_write` 以扇区为单位，写边界很可能不对齐扇区；
  cluster-level RMW 是最简单正确的对齐方案。Invariant
  `exfat-write-rmw-cluster-granularity`：每轮 RMW = `los_part_read` 整簇 +
  `memcpy_s` 改目标 byte 范围 + `los_part_write` 整簇，不跨簇。后续可优化到
  sector-level（Wave B 后期）。

**Q3：ExfatGetClusterAt 跨 TU 复用还是各自独立 static？**
- 选项：(a) **B1 直接复制一份 static（短期重复）** ← 用户选 / (b) 立刻提到
  fat_chain util 公共导出
- 答 (a)：B1 接受短期重复以避免跨 stage 接口暴露。Invariant
  `exfat-write-helper-duplication-temp`：标明 Wave B2 引入 truncate 时一并
  重构为 `exfat_pos_to_cluster` 公共导出。

**关键 invariant（19 个）**：

| invariant | 含义 |
|---|---|
| `exfat-write-linux-i-rwsem-faithful` | 全程持 ei->inode_lock（折映 Linux vfs_write 路径 inode->i_rwsem 独占）|
| `exfat-write-no-s-lock` | 不取 sbi->s_lock（Linux exfat_get_block create=0 路径同样不取）|
| `exfat-write-no-bitmap-lock` | 不取 sbi->bitmap_lock（B1 不分配/释放簇）|
| `exfat-write-clamp-by-size` | to_write = min(len, ei->size - f_pos)，绝不写越过 ei->size |
| `exfat-write-no-extend` | f_pos >= ei->size 时返回 0，不分配新簇 |
| `exfat-write-rmw-cluster-granularity` | 每次 IO 整簇：part_read → memcpy_s → part_write |
| `exfat-write-no-mutate-ei-on-success` | 成功路径 ei 任何字段都不变（含 size / valid_size）|
| `exfat-write-no-vol-flags-touch` | 不翻转 VOLUME_DIRTY（Wave B6 fsync 接管）|
| `exfat-write-short-write-on-mid-failure` | 中途 IO 失败且 written>0 时返回 written（短写）|
| `exfat-write-helper-duplication-temp` | ExfatWriteGetClusterAt 是 ExfatGetClusterAt 的字面副本，B2 时合并 |

### 3.2 Stage B2d truncate-extend — 分配 + 链接，不零填充

**ask-first 核心题**：

**Q1：新分配的簇是否需要物理零填充？**
- 选项：(a) **不零填充，依赖 valid_size 在读路径上虚拟实现** ← 用户选 /
  (b) los_part_write 逐簇写零
- 答 (a)：Linux 通过 page cache lazy-zeroing 完成；LiteOS 无 page cache，
  `valid_size` 标记真实数据边界——读路径在 `[valid_size, size)` 区段直接返回零，
  与盘上无论是什么字节无关。Invariant `exfat-truncate-extend-no-physical-zerofill`
  钉住该语义，省去对每个新簇的 cluster-level write。

**Q2：extend 时 valid_size 是否随 size 一并推进？**
- 选项：(a) **valid_size 保持不变（推荐）** ← 用户选 / (b) valid_size = new_size
- 答 (a)：新分配的区域未被写过，valid_size 不推进——下次读这段会按"虚拟零"
  返回，符合 POSIX ftruncate 对扩展区域返回零的语义。
  Invariant `exfat-truncate-extend-valid-size-preserved`。

**Q3：extend 到空文件（start_clu == EOF）时怎么链？**
- 由 spec 直接定义：`ei->start_clu = new_chain.dir`，无需走 FAT 链（空链无尾）。
  Invariant `exfat-truncate-extend-fat-chain-only`（v1 仅支持 ALLOC_FAT_CHAIN）。

**关键 invariant（9 个）**：

| invariant | 含义 |
|---|---|
| `exfat-truncate-extend-monotone-size` | 成功后 ei->size == new_size > old_size；失败后 ei->size 保持 |
| `exfat-truncate-extend-valid-size-preserved` | ei->valid_size 任何路径不变 |
| `exfat-truncate-extend-i-size-ondisk-aligned` | 成功后 i_size_ondisk 是 cluster_size 整数倍且 >= new_size |
| `exfat-truncate-extend-vol-flags-bracketed` | set_volume_dirty 在最外层，退出前尝试 clear（best-effort）|
| `exfat-truncate-extend-rollback-on-link-failure` | alloc 成功但 link 失败时必须 exfat_free_cluster 释放新链 |
| `exfat-truncate-extend-no-physical-zerofill` | 不对新簇做 los_part_write 零填充 |
| `exfat-truncate-extend-bounded-walk` | 找链尾的 FAT 遍历上限 EXFAT_MAX_CHAIN_LEN |
| `exfat-truncate-extend-no-dentry-write` | 不写盘上 dentry（sync 路径责任）|
| `exfat-truncate-extend-fat-chain-only` | v1 仅支持 ALLOC_FAT_CHAIN；NO_FAT_CHAIN → -EINVAL |

### 3.3 Stage B2e truncate-shrink — 截链顺序与 valid_size 夹紧

**ask-first 核心题**：

**Q1：截链 FAT 顺序——先 ent_set(EOF) 截链还是先 free_cluster 清 bitmap？**
- 选项：**(a) 先 ent_set(EOF) 截链，再 free_cluster 清 bitmap** ← 用户选 /
  (b) 先清 bitmap，再截链
- 答 (a)：与 `exfat_module.md` §3.3 `exfat-free-cluster-fat-then-bitmap` 一致。
  反向操作会在 crash 窗口产生"链尾仍指向已 free 簇"的悬挂引用——比位图泄漏
  更严重，因为下次 alloc 会把该簇分配出去造成 cross-link。Invariant
  `exfat-truncate-shrink-fat-then-bitmap`。

**Q2：valid_size 收缩时怎么处理？**
- 选项：**(a) 夹紧到 min(old_valid_size, new_size)** ← 用户选 / (b) 保持不变
- 答 (a)：与 extend 的"valid_size 保留"对称但方向相反——shrink 后逻辑越界
  的脏数据必须被夹掉，否则下次读越界段会按"已写过"处理，返回盘上旧内容。
  Invariant `exfat-truncate-shrink-valid-size-clamped`。

**Q3：shrink-to-zero 时 start_clu 复位为 EOF 还是 FREE？**
- 选项：**(a) EXFAT_EOF_CLUSTER（0xFFFFFFFF）** ← 用户选 / (b) EXFAT_FREE_CLUSTER (0)
- 答 (a)：与 Stage 2d extend "空文件 start_clu == EOF" 的 invariant 对称，
  保持 inode 层"无链 = EOF"的一致语义。Invariant
  `exfat-truncate-shrink-start-clu-on-empty`。

**关键 invariant（9 个）**：

| invariant | 含义 |
|---|---|
| `exfat-truncate-shrink-monotone-size` | 成功后 ei->size == new_size < old_size；失败（Case 1/5/6/7）ei->size 不变 |
| `exfat-truncate-shrink-valid-size-clamped` | 成功后 valid_size <= new_size（若原值更大则夹紧）|
| `exfat-truncate-shrink-i-size-ondisk-aligned` | 成功后 i_size_ondisk = num_new_clu * cluster_size |
| `exfat-truncate-shrink-fat-then-bitmap` | 必须先 ent_set(EOF) 截链，再 free_cluster 清 bitmap |
| `exfat-truncate-shrink-vol-flags-bracketed` | set_volume_dirty 在最外层，退出前尝试 clear（best-effort）|
| `exfat-truncate-shrink-start-clu-on-empty` | shrink-to-zero 后 ei->start_clu = EXFAT_EOF_CLUSTER |
| `exfat-truncate-shrink-bounded-walk` | 走链找 new_tail 的上限 EXFAT_MAX_CHAIN_LEN |
| `exfat-truncate-shrink-no-dentry-write` | 不写盘上 dentry（sync 路径责任）|
| `exfat-truncate-shrink-fat-chain-only` | v1 仅支持 ALLOC_FAT_CHAIN |

**Case 8/9 元数据泄漏语义**（shrink 独有）：free_cluster 中途失败时，
ent_set(EOF) 已经截链但部分簇位未能清 bitmap，属于孤儿簇（位图占用但链不引用）。
spec 显式接受这条 leak 路径并返回 -EIO，由 caller fsck 修复——与
`exfat-free-cluster-no-rollback`（Wave B Module §3.3）一致。

### 3.4 Stage B3 truncate-vop — 薄 dispatcher，无新 IO

**关键设计**：VfsExfatTruncate / VfsExfatTruncate64 本身不含任何 IO 逻辑；全部
逻辑已在 B2d/B2e helper 中。VOP 的职责是：

1. NULL 校验 + `len < 0` 拒绝（`off_t` 有符号；负值不能转为大正值调 extend，
   否则越过 `s_maxbytes` 触发 -EFBIG 而非 -EINVAL，语义归类错）。
2. `LOS_MuxLock(ei->inode_lock)` ——调用方不持任何锁；VOP 自取。
3. 三路分发：len == size → 直接返回 0；len > size → extend；len < size → shrink。
4. helper 失败码直接透传，不重映射。
5. `goto unlock_out` 单一出口确保解锁。

`VfsExfatTruncate` 与 `VfsExfatTruncate64` 在 B3 实现为完全等价：
Invariant `exfat-truncate-vop-truncate-equiv-truncate64`——对所有合法 off_t 值，
行为完全一致，不存在仅 32-bit 入口可达的边界条件。

**关键 invariant（6 个）**：

| invariant | 含义 |
|---|---|
| `exfat-truncate-vop-inode-lock-bracketed` | 非 -EINVAL 早退路径严格持锁/解锁成对；-EINVAL 早退不触锁 |
| `exfat-truncate-vop-no-self-recurse` | 锁序 inode_lock < s_lock < bitmap_lock；helper 内部自取内层锁 |
| `exfat-truncate-vop-no-dentry-write` | v1 不修改盘上 dentry；size 仅在内存层（ei->size）变更 |
| `exfat-truncate-vop-len-non-negative` | off_t 负值 → -EINVAL，不转 uint64_t |
| `exfat-truncate-vop-errno-passthrough` | helper errno 直接透传，不重映射 |
| `exfat-truncate-vop-truncate-equiv-truncate64` | 两个入口完全等价 |

---

## 4. 跨 Stage invariant

### 4.1 write-then-truncate 排序与 inode_lock 统一

Wave B 的 write / truncate-vop 路径都在入口取 `ei->inode_lock`（全程持有），
helpers（extend/shrink）声明"caller 持 inode_lock 进入，helper 不再取"。
这与 Wave A read 路径完全一致——四个 Fop/Vop 回调共享同一把锁，同一文件的
read / write / truncate 完全串行，满足 POSIX 强一致要求。

| 路径 | ei->inode_lock | sbi->s_lock | sbi->bitmap_lock |
|---|---|---|---|
| VfsExfatRead（Wave A）| **持，全程** | 不取 | 不取 |
| VfsExfatWrite（B1）| **持，全程** | 不取 | 不取 |
| exfat_truncate_extend（B2d）| caller 持 | set/clear_volume_dirty 自取自释 | alloc_cluster 自取自释 |
| exfat_truncate_shrink（B2e）| caller 持 | set/clear_volume_dirty 自取自释 | free_cluster 自取自释 |
| VfsExfatTruncate（B3）| **持，全程** | 委托 helper | 委托 helper |

锁序硬约束：`inode_lock < s_lock < bitmap_lock`。在 helper 边界内层锁均已
释放，inode_lock 持有期间 s_lock 与 bitmap_lock 交替进出但**不同时嵌套**。

### 4.2 vol_flags 括起是写路径的普遍约定

所有"修改盘面"的 helper（extend / shrink / alloc_cluster / free_cluster）都
被 `exfat_set_volume_dirty` / `exfat_clear_volume_dirty` 括起。
这是 Wave B Module Stage 2a 建立的边界协议（Invariant `exfat-vol-flags-bracketed`），
本文档的 4 个 stage 全部继承：

- extend：set_dirty → alloc → ent_set(link) → update ei → clear_dirty。
- shrink：set_dirty → walk → ent_set(EOF) → free → update ei → clear_dirty。
- write（B1）：**例外**——B1 in-place overwrite 不分配/释放簇，`vol_flags`
  由 Wave B6 fsync 接管（Invariant `exfat-write-no-vol-flags-touch`）。

### 4.3 write 是 in-place only（v1 不扩展文件）

v1 的 write 路径严格 clamp 到 `[f_pos, ei->size)`。越过 `ei->size` 的写短写返回
已写字节数而非分配新簇。这是**显式选择**（用户确认），由
`exfat-write-clamp-by-size` + `exfat-write-no-extend` 两条 invariant 钉住，
Wave B2 truncate-extend 解除约束后才能实现 append write。

### 4.4 dentry 持久化留 Wave B6

write / extend / shrink / truncate-vop 全部不写盘上 dentry（`ei->dir` /
`ei->entry` 指向的 exfat_dentry_set）。size / valid_size / i_size_ondisk 等
元数据更新仅在内存层（`ei->*` 字段），重启 mount 看到的仍是旧 dentry 记录的值。
这是 v1 的已知限制，所有 4 个 stage 的 `*-no-dentry-write` invariant 显式
声明，Wave B6 fsync stage 统一持久化。

---

## 5. 多层防御反馈

### 5.1 Stage B1 write——第一条用户可见的写路径

B1 是整个 exFAT 移植中第一次真正向盘面写数据的路径，因此额外做了 QEMU
端到端验证：

```
# mksh 1<> 走 O_RDWR 不截断，是 B1 的最小 in-place 测试向量
OHOS:/mnt/exfat$ cat etc/hostname
Aurora
OHOS:/mnt/exfat$ echo BeefMe 1<>etc/hostname
OHOS:/$ echo WRITE_RC=$?
WRITE_RC=0
OHOS:/mnt/exfat$ cat etc/hostname
BeefMe               ← 覆写立即可见（inode_lock 串行）
OHOS:/$ umount /mnt/exfat && mount -t exfat /dev/mmcblk0p3 /mnt/exfat
OHOS:/mnt/exfat$ cat etc/hostname
BeefMe               ← los_part_write 同步落盘，umount+remount 后持久
```

Layer 1（cclsp）：0 错误。
Layer 2（OHOS clang kernel build）：第一次报 `exfat_calc_chksum16 implicit
declaration`——`exfat.h` 补声明后通过（与 Wave A Layer 2 同类错误）。

### 5.2 Stage B2d/B2e/B3——cmocka 三层覆盖

三个 stage 全部走 cmocka host 验证（无需 QEMU），因为 helpers 的 IO 路径完全
由 `mock_disk` 路由到内存镜像。Layer 验证策略：

**Layer 1（cclsp）**：

- B2d：首次报 `exfat_clear_volume_dirty` 未声明——vol_flags helpers 初次跨 TU
  引用，`exfat.h` 补 extern 声明后通过。
- B2e / B3：后续 stage 延续同样模式，无新增错误。

**Layer 2（cmocka build + run）**：

| Stage | 新增 testpoints | 累计总 testpoints | failures |
|---|---|---|---|
| B1 write | 16 | 312 | 0 |
| B2d truncate-extend | 21 | 312 → 333 | 0 |
| B2e truncate-shrink | 23 | 333 → 356 | 0（实际累计 335，见 commit）|
| B3 truncate-vop | 17 | 352 | 0 |

**B2d 关键测试设计**——`mock_disk_set_write_fail_at(N)` 序列精确匹配
helper 内部的写顺序：write 1 = vol_flags set → write 2 = bitmap → write 3 =
ent_set(EOF) → write 4 = ent_set link old tail。每个失败点独立测试，覆盖
Case 7/8/9（-EIO during vol_flags / alloc / link）。

**B2e 关键 invariant 测试**——`fat-then-bitmap` 顺序测试通过
`mock_disk_set_write_fail_at` + write 序列断言，验证 ent_set(EOF) 必须
先于 bitmap clear 出现在 mock 的写记录中。

**B3 host_stubs 适配**——`VfsExfatTruncate64` 用 `off64_t`；macOS 不暴露
`off64_t`（Linux musl/glibc 在 `_LARGEFILE64_SOURCE` 下提供）。修：
`testsuites/unittest/exfat/host_stubs/vnode.h` 加 `__APPLE__` 路径的
`typedef int64_t off64_t`，不影响远端 Linux x86_64 cmocka。

---

## 6. 落地清单

### 6.1 commit 与文件变更

| commit | Stage | 主要新增文件 | 行数 |
|---|---|---|---|
| `8e3622f8` | B1 write | `fs/exfat/exfat_write.c` | 254 |
| `128ebde1` | B2d truncate-extend | `fs/exfat/exfat_truncate_extend.c` | 172 |
| `dea5226c` | B2e truncate-shrink | `fs/exfat/exfat_truncate_shrink.c` | 213 |
| `36fe4a10` | B3 truncate-vop | `fs/exfat/exfat_truncate_vop.c` | 84 |

（2026-05-05 重构后，上述 4 个 .c 已合并进 `fs/exfat/exfat_file.c`，总计 1,536 行。）

### 6.2 spec / code LOC 与比例

| Stage | spec LOC | code LOC（原始）| spec/code 比 | invariant 数 |
|---|---|---|---|---|
| B1 write | 329 | 254 | 1.30 | 19 |
| B2d truncate-extend | 216 | 172 | 1.26 | 9 |
| B2e truncate-shrink | 226 | 213 | 1.06 | 9 |
| B3 truncate-vop | 159 | 84 | 1.89 | 6 |
| **合计** | **930** | **723** | **1.29** | **43** |

### 6.3 测试 LOC

| Stage | test LOC | testpoints |
|---|---|---|
| B1 write | 643 | 16 |
| B2d truncate-extend | 446 | 21 |
| B2e truncate-shrink | 531 | 23 |
| B3 truncate-vop | 377 | 17 |
| **合计** | **1,997** | **77** |

### 6.4 DAG 节点状态（落地时）

```
write          spec✓  code✓  tests✓  depends_on: [fat_chain, inode_alloc]
truncate_extend spec✓  code✓  tests✓  depends_on: [alloc_cluster, vol_flags, ent_set]
truncate_shrink spec✓  code✓  tests✓  depends_on: [free_cluster, vol_flags, ent_set, truncate_extend]
truncate_vop   spec✓  code✓  tests✓  depends_on: [truncate_extend, truncate_shrink]
```

### 6.5 g_exfatFops / g_exfatVops 最终状态（B3 后）

```c
struct file_operations_vfs g_exfatFops = {
    .open  = VfsExfatOpen,
    .close = VfsExfatClose,
    .read  = VfsExfatRead,
    .seek  = VfsExfatSeek,
    .write = VfsExfatWrite,       /* B1 新增 */
};

struct VnodeOps g_exfatVops = {
    .Lookup    = VfsExfatLookup,
    .Reclaim   = VfsExfatReclaim,
    .Opendir   = VfsExfatOpendir,
    .Readdir   = VfsExfatReaddir,
    .Closedir  = VfsExfatClosedir,
    .Rewinddir = VfsExfatRewinddir,
    .Getattr   = VfsExfatGetattr,
    .Truncate  = VfsExfatTruncate,   /* B3 新增 */
    .Truncate64 = VfsExfatTruncate64, /* B3 新增 */
};
```

---

## 7. 下一波预告

Wave B 文件 IO 层完成后，仍有以下路径未实现（均为 v1 已知边界）：

| 功能 | 现状 | 后续 stage |
|---|---|---|
| append write（O_APPEND / f_pos >= ei->size 扩展）| write 截短返回 0 | Wave B2 extend 路径与 write 整合 |
| O_TRUNC 语义（open 时截零）| 需要 Truncate VOP 被 VFS open 路径调用，B3 已接入槽位 | 自动生效（VFS 调 .Truncate）|
| dentry 持久化（ValidDataLength / FileSize 写回）| 内存 only | Wave B6 fsync |
| VOLUME_DIRTY 翻转（write / truncate 路径）| B1/B2d/B2e 显式不做 | Wave B6 fsync |
| sparse write / 非连续 f_pos | v1 连续 in-place only | v2 删除（不在路线图）|
| mmap / page cache 路径 | LiteOS-A 无 page cache | v2 删除（不在路线图）|
| fallocate | Linux 专有，exFAT 无盘上语义 | v2 删除（不在路线图）|
| ExfatGetClusterAt 合并公共导出 | B1/Wave A 各有 static 副本 | B2 truncate 引入时重构为 `exfat_pos_to_cluster`（fat_chain util）|

v2 明确删除：sparse write、mmap、fallocate 三项不在 LiteOS-A exFAT v1 路线图，
不开 spec stage，不预留槽位。

---

## 8. 引用

- `docs/dev/exfat_mount.md` — Wave 0 mount 流程实录
- `docs/dev/exfat_read.md` — Wave A read 路径（锁矩阵基线）
- `docs/dev/exfat_module.md` — Wave B Module 层（vol_flags / ent_set / free_cluster / alloc_cluster）
- `spec/exfat/interface/exfat_write.spec` — B1 write spec（329 行，19 invariants）
- `spec/exfat/interface/exfat_truncate_extend.spec` — B2d spec（216 行，9 invariants）
- `spec/exfat/interface/exfat_truncate_shrink.spec` — B2e spec（226 行，9 invariants）
- `spec/exfat/interface/exfat_truncate_vop.spec` — B3 spec（159 行，6 invariants）
- `spec/exfat/.specfs.dag.json` — 全 stage 双层批准状态
- 论文 *Sharpen the Spec, Cut the Code*（FAST'26，arXiv:2512.13047）
