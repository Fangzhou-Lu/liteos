# exFAT Mount 开发流程实录（端到端 QEMU 通过）

本文档记录用 `specfs-port` 插件把 Linux exFAT mount 路径移植到 LiteOS-A 的
完整流程：初始输入、ask-first 决策、用户拍板、spec/code 模块依赖、跨阶段
契约演化、5 层防御反馈、构建接线与 QEMU 验证。

记录意图：作为一次"真实输入下端到端 plugin 跑通"的回放参考，便于后续 lookup /
read 等阶段直接套用同样的节奏。

---

## 1. 输入与目标

### 1.1 初始输入

| 项 | 值 |
|---|---|
| Linux 源码 | `/Users/kissa/Codebase/linux/fs/exfat/`（14 文件 7,399 行）|
| 目标 FS 名 | `exfat` |
| 目标产物路径 | `fs/exfat/`（LiteOS-A 端）|
| 范围 | 仅 mount 路径——`Mount` / `Unmount` / `Statfs` / `Sync` 与 root vnode 创建 |
| 板与构建目标 | `qemu_small_system_demo`，arm_virt LiteOS-A |
| 远程构建主机 | `192.168.1.15`，OH 树在 `/mnt/work/openharmony` |

### 1.2 出口标准

1. spec/code 比 < 1.0（论文生产力命题）。
2. `hb build` 通过，OHOS_Image 含 `VfsExfatMount` + `FSMAP_ENTRY(exfat_fsmap, "exfat", ...)`。
3. QEMU 启动后 `mount -t exfat /dev/mmcblk0p3 /mnt/exfat` 返 0。
4. `/proc/mounts` 与 `mount` 命令各自识别 exfat 类型。
5. 文件读取（`ls`/`cat`）允许 `-ENOSYS`—— 仅 mount 边界，读路径在后续版本。

---

## 2. 准备阶段（一次性基础设施）

### 2.1 插件安装 / 重命名 / 分支

```
git checkout -b feature/spec-port
git mv fs/exfat/spec/* spec/exfat/ # 把 spec 树升到顶层
# 旧手写实现已归档到仓库外的 backup/ 目录（不再纳入版本管理）
# 落 plugin 代码（25 文件 ~3700 LOC）.claude/plugins/specfs-port/
git commit -m "chore(fs/exfat): spec 树移至顶层 spec/，旧实现归档到 backup/"
git commit -m "feat(.claude/plugins/specfs-port): HITL 规范优先 ..."
```

### 2.2 LSP 配置（Layer 0 关）

| 文件 | 作用 |
|---|---|
| `cclsp.json` | 路由 `*.c, *.h` → `clangd --background-index --clang-tidy=false --header-insertion=never` |
| `.clangd` | 19 个 `-I` + 6 个 `-D` + `--target=arm-linux-gnueabi`（避 Mach-O 段名）+ `-include stdarg.h`（补 los_printf.h va_list）|
| `.mcp.json` | 项目级注册 cclsp MCP server |

**踩坑顺序**：`exfat.h` not found（`--check` cwd 在 .c 目录，相对 -I 错位）→ 改绝对路径；
`bits/alltypes.h` not found（musl 模板未生成）→ 不用 `-nostdinc`；`los_config.h`/`los_vm_zone.h`
等 7 个头依次找补；`va_list` undeclared → `-include stdarg.h`；Mach-O 拒绝
`.liteos.table.fsmap.data` 段名 → `--target=arm-linux-gnueabi`。

最终 `clangd --check fs/exfat/exfat_super.c → 0 errors`。

---

## 3. Loop A × 7：ask-first 决策与 spec 起草

### 3.1 决策矩阵概览

| Stage | ask-first 题数 | 用户拍板 |
|---|---|---|
| mount | 4 | RW、严格 CRC32、盘上 upcase、接受选项串解析 |
| chksum | 1 | chksum32 + chksum16 一并出 |
| options | 1 | 子集（10 key），不接受 deprecated |
| dentry | 2 | 全 FAT 链走 + 检查 logical_sector_size 一致 |
| balloc | 0 | spec 里直接定 Linux 兼容语义（need>actual = -EIO，need<actual = warn）|
| upcase | 1 | 严格策略——chksum mismatch → -EINVAL，不 fallback 内置表 |
| vfs_ops_stub | 1 | 单一 `exfat_ops.c` 文件（不拆 inode/file）|

### 3.2 mount — 决定 边界的 4 个关键选择

LLM 读完 `super.c::exfat_fill_super` + 依赖链后，触发 ask-first 弹出 4 个二选一/三选一：

**Q1：mount 是否锁定为只读？**
- 选项：只读 / **开放可写** ← 用户选
- 影响：删去 `read_only=1` 字段与" 永不写盘"硬 invariant；mount 路径自身仍只读
 IO，但写路径符号在 后续 时无需 spec_gen_refine 重做 mount。

**Q2：boot region 11+1 扇区 CRC32 校验如何处理？**
- 选项：宽容 PRINT_WARN / **严格 -EINVAL** / 完全跳过
- 用户选严格——`exfat_calc_chksum32` 进 [RELY]，触发 chksum 阶段。

**Q3：upcase 表来源？**
- 选项：内置默认压缩表 / **从盘上读取**
- 用户选盘上——触发 upcase 阶段，依赖 dentry + chksum。

**Q4：Linux mount 选项串怎么处理？**
- 选项：硬编码默认（ 简）/ **接受字符串解析** / 全集兼容
- 用户选解析子集——触发 options 阶段。

四答合力把 mount 范围从"5 行参数挂载"扩到"含 strict CRC + 盘上 upcase +
options parser 的真实 mount 路径"。**插件价值**：四问钉死契约，下游每个 stage
的 [RELY] 段不再有歧义。

mount.spec 起草完毕：301 行，5 invariants（rollback-lifo / mount-data-self-consistent /
vnode-visible-after-init / fsmap-entry-name / readonly-during-mount），用户 review 后
approve。

### 3.3 chksum — 简单纯函数，1 个题

**Q：本轮包含 chksum16 吗？**
- chksum32 only / **chksum32 + chksum16 一并出** ← 用户选

理由：算法对称（1-bit ROR + add，跳位偏移不同），一并出避免后续 lookup/readdir
阶段重开。spec 101 行，4 invariants，关键是 **`exfat-chksum-not-iee-crc`**——
钉死"不得用 LOS_Crc32"。

### 3.4 options — 选项 key scope

**Q： 接受哪些 key？**
- 最小子集（iocharset + errors）/ **10-key 子集（推荐）** ← 用户选 / 全集（含 4 个 deprecated）

10 key：iocharset、errors、uid、gid、umask、fmask、dmask、allow_utime、discard、time_offset。
spec 129 行，5 invariants，关键 **`exfat-options-no-heap-leak`**——iocharset 指
静态字符串字面量，避免堆分配 / 释放责任。

### 3.5 dentry — 带 IO 的第一个真 helper

两个题：

**Q1：find_root_dentry 扫描策略？**
- 仅扫第一簇 / **全 FAT 链走（与 Linux 等价）** ← 用户选

**Q2：parse_boot_sector 是否检查 logical_sector_size 与 bs->sect_size_bits 一致？**
- **检查一致** ← 用户选 / 信任调用者

spec 217 行，6 invariants。引入内部 `ReadFatEntry` 静态 helper，并在 invariant
`exfat-dentry-fat-traversal-bounded` 里钉死"max_iter = num_clusters"防恶意
FAT 环。

### 3.6 balloc — 直接定 Linux 兼容语义，无 ask-first

3 函数：load_bitmap / free_bitmap / count_used_clusters。192 行 spec，6 invariants。
`exfat-balloc-bitmap-size-policy` 写明"need_map_size > size = -EIO，<size = warn"
与 Linux balloc.c 完全一致。`depends_on: dentry`。

### 3.7 upcase — 严格 vs fallback

**Q：chksum mismatch 如何处理？**
- **严格 -EINVAL（推荐 ）** ← 用户选 / fallback 内置 65536-entry 默认表（128KB）

129 行 spec，5 invariants。`exfat-upcase-strict-no-fallback` 钉死"不留 128KB 静态表
体积"。`depends_on: dentry, chksum`——这是依赖图里**唯一一个 2 上游的 stage**。

### 3.8 vfs_ops_stub — 占位表

**Q：单文件还是拆 inode/file 两文件？**
- **单一 fs/exfat/exfat_ops.c** ← 用户选 / 拆到 exfat_inode.c + exfat_file.c

90 行 spec，4 invariants。`exfat-vfs-stub-symbol-stable` 钉死"后续阶段填字段，不改
符号身份"。代码文件 43 行——两行核心：

```c
struct VnodeOps g_exfatVops = { 0 };
struct file_operations_vfs g_exfatFops = { 0 };
```

NULL 字段保证 mount 之后 lookup/read 自动返 -ENOSYS（VFS 框架既定行为）。

---

## 4. Loop B × 7：codegen + Layer 0 LSP 反馈

每个 stage 走相同流水线：

```
prompts.assemble_codegen_prompt()
    ↓ 装配（41-44k 字符 prompt，含 spec + frozen header + 继承 invariants + 已生成代码接口 + 8 段规则）
LLM 生成 .c
    ↓
mcp__cclsp__get_diagnostics
    ↓ Layer 0
0 diagnostics → commit code 层
```

### 4.1 跨阶段契约演化（reconcile 事件 × 2）

LSP 在两次"上游 [RELY] 与下游 spec 不同步"时立即抓到——这正是插件 Layer 0
的设计价值。

**事件 1：dentry 收紧 sbi 为 const**

mount spec [RELY] 写：
```c
int exfat_find_root_dentry(struct exfat_sb_info *sbi, ...);
```

dentry spec [GUARANTEE] 写：
```c
int exfat_find_root_dentry(const exfat_sb_info *sbi, ...);
```

生成 `exfat_dentry.c` 后 LSP 报：

```
exfat_dentry.c:212: error: conflicting types for 'exfat_find_root_dentry'
exfat.h:178: note: previous declaration is 'int exfat_find_root_dentry(exfat_sb_info *, ...)'
```

修法：`exfat.h` 加 `const`。理想做法是 `reconcile_spec` 自动开 mount
spec_gen_refine——暂未实现。

**事件 2：balloc 收紧 count_used_clusters 为 const**

同 pattern，同手工修复。

### 4.2 真编译错误检出（Werror）

构建到 `hb build` 时 Werror 抓到 2 个 LSP 漏过的错（clangd flag 集合不含
对应 -W）：

**事件 3：注释嵌套**
```c
/* ---- public exports (defined in fs/exfat/*.c, externed for mount glue) */
```
`fs/exfat/*.c` 中 `t/*` 子串是 `/*` token → `-Wcomment` 抓到。修：`*.c`
改成 "translation units"。

**事件 4：API 不存在**
```c
(void)ClearDiskPartName(part); // 想象的对称 API
```
disk_pri.h 里只有 `SetDiskPartName`。fs/fat/fatfs.c:1226 用的是：
```c
if (part->part_name) { free(part->part_name); part->part_name = NULL; }
```
修 mount 实现匹配 fatfs 的成熟模式。

### 4.3 LSP 修复后均 0 diagnostics

7 个 .c 文件最终全部通过 cclsp `get_diagnostics`：

| 文件 | 行数 | LSP 状态 |
|---|---|---|
| fs/exfat/include/exfat.h | 188 | ✓ |
| fs/exfat/include/exfat_raw.h | 192 | ✓ |
| fs/exfat/exfat_super.c | 473 | ✓ |
| fs/exfat/exfat_dentry.c | 258 | ✓ |
| fs/exfat/exfat_balloc.c | 190 | ✓ |
| fs/exfat/exfat_ops.c | 43 | ✓ |
| fs/exfat/util/exfat_chksum.c | 70 | ✓ |
| fs/exfat/util/exfat_options.c | 306 | ✓ |
| fs/exfat/util/exfat_upcase.c | 136 | ✓ |

---

## 5. 模块依赖图（Spec 与 Code 双层 DAG）

```
    ┌───────────────┐
    │ vfs_ops_stub │ (只声明 g_exfatVops, g_exfatFops 占位)
    │ - │
    └───────┬───────┘
    │
    ┌──────────┐ │ ┌──────────┐
    │ chksum │ │ │ options │ (mount 选项串解析)
    │ - │ │ │     │
    └─┬────────┘ │ └─┬────────┘
    │ │ │
    │ ┌──────────┐ │ │
    │ │ dentry │ │ │
    │ │ - │────┼──┐ │
    │ └─┬───┬────┘ │ │ │
    │ │ │ │ │ │
    │ │ └────────┐│ │ │
    │ ▼ ▼│ │ │
    │ ┌──────────┐ ┌──────────┐ │
    └─→ │ upcase │ │ balloc │ │
    │ - │ │     │ │
    └──────┬───┘ └──┬───────┘ │
    │ │ │
    ▼ ▼ ▼
    ┌───────────────────────────────┐
    │ mount │
    │ (interface/exfat_mount.spec) │
    │ → fs/exfat/exfat_super.c │
    │ → FSMAP_ENTRY("exfat", ...) │
    └───────────────────────────────┘
```

依赖语义：

- 实线：spec [RELY] 段引用上游 [GUARANTEE] 中的导出，且 DAG `depends_on` 字段记录。
- mount spec [RELY] 间接引用所有 helper，但 DAG `depends_on=[]`——因为
 mount.spec 的 [RELY] 是 forward-reference（声明 helper 必须存在），不要求生成顺序。
- 实际 codegen 顺序：bottom-up（叶子先生成），由 `commit_node.py` 在
 `depends_on` 节点已 approved 时才允许子节点 spec 提交。

---

## 6. 构建接线（task #20）

### 6.1 三件套生成

| 文件 | 行数 | 内容 |
|---|---|---|
| `fs/exfat/BUILD.gn` | 51 | `kernel_module(...)` + sources 列 7 个 .c + `include_dirs=["include"]` |
| `fs/exfat/Makefile` | 39 | `LOCAL_SRCS := $(wildcard *.c) $(wildcard util/*.c)` |
| `fs/exfat/Kconfig` | 8 | `config FS_EXFAT bool depends on FS_VFS default n` |

### 6.2 顶层接线（已存在 from prior backup work）

| 位置 | 修改 |
|---|---|
| `fs/BUILD.gn` | `deps` 已含 `"exfat"` + `public_configs` 含 `"exfat:public"` |
| `fs/Kconfig` | 已 `source "fs/exfat/Kconfig"` |
| `tools/build/mk/los_config.mk` | 已有 `ifeq ($(LOSCFG_FS_EXFAT), y) ... LITEOS_BASELIB += -lexfat` 块 |
| `vendor/ohemu/qemu_small_system_demo/kernel_configs/debug.config` | 已有 `LOSCFG_FS_EXFAT=y` |

### 6.3 hb build 结果

```
[OHOS INFO] qemu_small_system_demo build success
[OHOS INFO] Cost Time: 0:02:30
```

链接产物 `OHOS_Image.bin`（1273 KB）含 7 个 exfat 符号 + `exfat_fsmap` 在
`.liteos.table.fsmap.data` 段。

```
nm OHOS_Image | grep exfat
40010650 T exfat_load_bitmap
40010f00 T VfsExfatMount
40011708 T exfat_calc_chksum32
40011784 T exfat_parse_options
40011d8c T exfat_create_upcase_table
40134048 D g_exfatMountOps
40135320 D exfat_fsmap
```

---

## 7. QEMU 端到端验证

### 7.1 disk image 制作

`smallmmc.img` 150 MiB，4 个 MBR primary partition：

| 分区 | 物理范围 | 内容 | 内核映射 |
|---|---|---|---|
| p1 | 10-30 MiB | vfat（rootfs/* 内容）| /dev/mmcblk0p0 → / |
| p2 | 30-80 MiB | vfat（userfs/* 内容）| /dev/mmcblk0p1 → /storage |
| p3 | 80-100 MiB | vfat（空）| /dev/mmcblk0p2 → /userdata |
| **p4** | **100-150 MiB** | **exfat**（payload：/etc/* + EXFAT_HELLO.txt）| **/dev/mmcblk0p3 → 测试 mount 目标** |

bootargs 写入 image 偏移 512K：

```
bootargs=root=emmc fstype=vfat rootaddr=10M rootsize=20M useraddr=30M usersize=50M exfataddr=100M\0
```

`exfataddr=100M` 触发 `los_rootfs.c::AddEmmcParts` 创建第 4 分区。

### 7.2 QEMU 启动参数

```
qemu-system-arm -M virt,gic-version=2,secure=on -cpu cortex-a7 -smp cpus=1 -m 1G \
    -bios out/arm_virt/qemu_small_system_demo/OHOS_Image.bin \
    -global virtio-mmio.force-legacy=false -nographic \
    -drive if=none,file=out/smallmmc.img,format=raw,id=mmc \
    -device virtio-blk-device,drive=mmc \
    -device virtio-rng-device
```

关键点：

- `-nographic`（不是 `-display none`）—— qemu 11.0+ 否则开 VNC ::1:5900。
- 单 drive（早期失败尝试用过双 drive `smallmmc + exfat32m`）。
- 无 GPU/tablet—— qemu 11.0 已删 transitional name。

### 7.3 测试编排（FIFO 驱动）

```bash
LOG=/tmp/qemu_serial.log; FIFO=/tmp/qemu_in
> $LOG; rm -f $FIFO; mkfifo $FIFO
( exec 3<>$FIFO
    qemu-system-arm ... < $FIFO > $LOG 2>&1 ) &
QPID=$!

# 轮询 shell prompt
for i in $(seq 1 60); do grep -q 'OHOS:/' $LOG && break; sleep 1; done

# 发命令
{ printf 'mount -t exfat /dev/mmcblk0p3 /mnt/exfat\n'; sleep 2
    printf 'echo MOUNT_RC=$?\n'; sleep 0.5
    printf 'mount\n'; sleep 1
    printf 'echo TEST_DONE_MARKER\n'
} > $FIFO

# 等结束 marker
for i in $(seq 1 60); do grep -q TEST_DONE_MARKER $LOG && break; sleep 1; done
kill -9 $QPID
```

### 7.4 实际输出（截选）

```
OHOS:/$ ls /dev
__parameters__ hilog mmcblk0 mmcblk0p2 random uartdev-0
console1 lite_ipc mmcblk0p0 mmcblk0p3 serial unix
hdf mem mmcblk0p1 mmz trace urandom
OHOS:/$ mount -t exfat /dev/mmcblk0p3 /mnt/exfat
OHOS:/$ echo MOUNT_RC=$?
MOUNT_RC=0 ← ✅ mount 成功
OHOS:/$ mount
/dev/mmcblk0p3 on /mnt/exfat type exfat ← ✅ /proc/mounts 识别 exfat
/dev/mmcblk0p2 on /userdata type vfat
/dev/mmcblk0p1 on /storage type vfat
/dev/mmcblk0p0 on / type vfat
proc on /proc type proc
OHOS:/$ ls -la /mnt/exfat
ls: /mnt/exfat: Function not implemented ← ⚠️ 边界（lookup未实现）
```

---

## 8. 测试评估总结

### 8.1 量化指标

| 类别 | 数值 | 备注 |
|---|---|---|
| Linux 输入源码 | 7,399 行（14 文件）| 仅参照范围 |
| **生成 spec** | **1,344 行（11 文件）** | 4 个 .header + 7 个 .spec |
| **生成 C 代码** | **1,856 行（9 文件，含 .h）** | 仅 .c：1,476 行 |
| 构建脚手架 | 98 行（BUILD.gn + Makefile + Kconfig）| 模板化 |
| **spec/code 比** | **1344 / 1476 = 0.91** | 论文生产力命题 ✓ |
| 总 invariants | 36 个 | 全 unique ID |
| 总 exports | 17 个 | func/var/fsmap |
| ask-first 题数 | 10 题 | 跨 7 stage |
| user approve 次数 | 14 次（7 spec + 7 code） | 双层 gate |
| **Layer 0 LSP 检出错误** | **2 跨阶段契约 + 0 类型错** | const sbi 收紧 |
| **Layer 1+2 编译检出错误** | **2 真错** | 注释嵌套、不存在 API |
| 构建 wall-clock | 2:30 (`hb build` 完整) | 远端 192.168.1.15 |
| QEMU 启动到 shell | 2-3s | `-nographic` 单 drive |
| QEMU mount RC | 0 | /dev/mmcblk0p3 |

### 8.2 qualitative 评估

**插件价值实证（与不用插件直翻对比）**：

1. **Linux 假设主动剥离** —— 4 个 ask-first 在 mount 阶段就钉死 范围
 （RW、严格 CRC、盘上 upcase、选项串解析），下游 6 个 helper 无歧义传播。
 不用插件直翻容易顺手把 page cache / RCU / kmem_cache 等 Linux 假设
 带进 LiteOS 实现。

2. **跨阶段契约一致** —— `[RELY]` 段强制真实 LiteOS C 声明（如 `LosMux` /
 `los_part_read` 全签名），加上 `[GUARANTEE]` 调用约定块，下游 spec 直接
 引用上游 export 不会幻觉。

3. **真 bug 早期截获** —— 2 类编译错误（注释嵌套、不存在 API）和 2 类跨阶段
 契约漂移（const 收紧）都在 LSP/Werror 自动抓到，平均修复 < 1 分钟。

4. **可追溯单元** —— 36 个 invariants 全部有 unique ID，DAG approved_at 时间戳
 完整。任何后续 后续修改若违反某 invariant，可直接定位最早 approved 的 stage。

### 8.3 已知边界 / 后续路线图

| 边界 | 现状 | 后续 修复方式 |
|---|---|---|
| `ls /mnt/exfat` 返 -ENOSYS | g_exfatVops 全 NULL | lookup / readdir spec round |
| `cat <file>` 返 -ENOSYS | g_exfatFops 全 NULL | open / read spec round |
| ~~`umount /mnt/exfat` 触发 NULL deref~~ **已修（2026-05-01）** | ~~VfsExfatUnmount 释放 vnodeCovered 后 VFS 后续访问~~ 真因：`g_exfatVops.Reclaim==NULL` 让 VFS `VnodeFreeAll` 在 path_cache 走查时崩溃 | 在 `VfsExfatMount` lazy-init `g_exfatVops.Reclaim = VfsExfatReclaim`；Unmount 不再触碰 root vnode |
| 写路径 | 全部缺失 | 后续：FAT 写、bitmap 修改、vol_flags 持久化 |
| 跨阶段 reconcile 自动化 | 手工修头文件 | 实现 `reconcile_spec` MCP 工具 |

---

## 9. 时间线（粗略）

| 阶段 | 耗时 |
|---|---|
| 准备（rename + plugin 落定）| 1 day（前置）|
| LSP 配置（cclsp + clangd）| ~30 min |
| Loop A × 7（spec 起草 + ask-first + approve）| ~2 hr |
| Loop B × 7（codegen + LSP + approve）| ~1 hr |
| 构建接线（BUILD.gn/Makefile/Kconfig + hb build 调试）| ~30 min |
| QEMU 镜像制作（150MB / 4 分区 / mkfs.exfat / payload / bootargs NUL fix）| ~45 min（含踩坑）|
| QEMU 启动调试（`-nographic` vs `-display none`、qemu 11.0 GPU/tablet 移除、bootargs printf 转义）| ~30 min |
| 端到端验证 mount RC=0 | < 5 min |

**关键瓶颈**：QEMU 镜像/启动调试占了一半时间。源于环境差异（开发/远端 qemu
版本、host 缺 exfat-fuse 等）。这部分已沉淀进 `CLAUDE.md` §QEMU end-to-end
testing recipe，下次 lookup 走 后续 应可压缩到 < 10 分钟。

---

## 10. 复盘要点

### 做得好的

- **ask-first 提前钉死边界** — 4 个 mount 决策一次性把 范围确定，下游 6
 个 helper 直接套用，无回头修改。
- **真实 [RELY] 签名** — 始终强制完整 C 签名（`LosMux *`、`INT32 part_id`），
 避免 LLM 幻觉 Linux 接口。
- **invariant 跨阶段累加机制** — `dag.collect_invariants` 在 prompt 装配时
 自动注入，下游 spec 受上游契约约束。
- **commit_node.py 通用化** — 一开始为每个 stage 写了一次性脚本
 (_commit_chksum_spec.py、_commit_mount_spec.py)，及时统一为通用 CLI。

### 可改进的

- **MCP server 工具未注册成功** — Claude Code runtime 报"2 plugin MCP servers 加载"
 但 `mcp__specfs__*` 没出现在工具索引里。当前用 `commit_node.py` stand-in。
 后续 调试 `.mcp.json` 配置或切 OMC MCP runtime。
- **跨阶段 reconcile 手工** — 2 次 const 收紧靠 LSP 抓 + 手工修头文件。
 后续 实现 `reconcile_spec` 工具自动 trigger 上游 `spec_gen_refine`。
- **QEMU 调试踩坑** — `printf '\\0'` vs `printf '\0'` 转义、qemu 11.0 GPU 名称
 变更、远端缺 exfat-fuse 等，每个独立耗时 5-10 分钟。已沉淀进 CLAUDE.md。

### 一次到位的关键洞察

1. **必须把 rootfs/userfs 内容拷到 p1/p2** —— 否则 /storage mount 失败 errno 13。
 会话日志里早期搜索找到的， 没有该信息会卡 30+ 分钟。
2. **`-nographic` ≠ `-display none`** —— qemu 11.0 在后者下仍开 VNC。
3. **`exfataddr=NMM` 触发第 4 分区** —— 不是 MBR 数量决定的，是
 `los_rootfs.c::AddEmmcParts` 的 bootarg 检测。
4. **mkfs.exfat 标签 flag** —— legacy `exfat-utils` 用 `-n`，新 `exfatprogs` 用 `-L`。
 两者互斥不能共存。

---

## 11. 引用

- `docs/specfs_plugin_design.md` — 插件设计文档
- `.claude/plugins/specfs-port/DESIGN.md` — 插件实现规格
- `CLAUDE.md` §QEMU end-to-end testing recipe — 镜像制作与 QEMU 启动详细步骤
- `spec/exfat/.specfs.dag.json` — 7 stage 双层批准状态
- 论文 *Sharpen the Spec, Cut the Code*（FAST'26，arXiv:2512.13047）

---

## 12. 回归测试基础设施

mount 落地后追加的两层回归套件：

| 层 | 跑在哪 | 入口 | 测什么 |
|---|---|---|---|
| **Layer A: cmocka host** | dev/CI host (Linux x86_64) | `make` in `testsuites/unittest/exfat/` | 5 个 helper 翻译单元的 49 个测点（chksum 8 / options 18 / dentry 10 / balloc 8 / upcase 5）|
| **Layer B: QEMU LTP smoke** | QEMU virt + LiteOS-A | `tools/regress/qemu_ltp_run.sh` | 6 个 LTP 用例 (creat/open/read/write/unlink/stat) 真挂 exFAT 跑 |

### cmocka 设计要点

`testsuites/unittest/exfat/` 里 `mock_disk.{h,c}` 把 `los_part_read` /
`los_part_write` 路由到内存中的合成 exFAT 镜像（`exfat_image_builder.c` 现场拼，
6 KiB，自带 boot+FAT+3 簇 data）。`mock_part_write` 只改 RAM 不回写磁盘文件，
保证镜像跨测试可重复使用。

`mock_disk_set_read_fail_at(N)` 注入第 N 次读失败，用于覆盖 -EIO 路径。

5 个负向变体的 image：bad signature / bad fs_name / no bitmap dentry /
corrupt upcase chksum / bitmap too small。

不在 cmocka 范围：`exfat_super.c::VfsExfatMount`（依赖整个 VFS 框架）、
`exfat_ops.c`（无逻辑）—— 这两块由 Layer B 在真机上覆盖。

### LTP 子集裁剪

远程 `/mnt/work/test/ltp_pack.tar.gz` 是 153 MB 的全量打包（13 个子模块、67 个 ELF）；
`qemu_ltp_run.sh` 在执行前先做 smoke 子集裁剪到 ~30 MB（删 runtest/, libltp.a, 非 smoke ELF），
塞进 p2 userfs (50 MB) 后挂 /storage 跑。

LTP smoke 跑完打 `LTP_DONE` marker；qemu_ltp_run.sh 用 FIFO + log polling 抓
serial stdout，watch `panic | Oops | Kernel BUG | TEST_DONE_MARKER` 任一命中前
3 项立刻 kill QEMU 报 panic（exit 2），TIMEOUT_S（默认 600s）超时报 hang（exit 2）。

### 聚合脚本与报告

`tools/regress/run_all.sh` 顺序跑两层，写时间戳化的 markdown 到
`docs/test/exfat_regression_<ts>.md`，并维护 `docs/test/exfat_regression_latest.md` symlink（`docs/test/` 已加入 `.gitignore`，仅本地留档）。
退出码语义：0 = 全过、1 = 有失败、2 = panic/hang。
若 qemu_ltp_run.sh 缺失：cmocka 通过即视为 PARTIAL（exit 0），便于 bootstrap。

### 与 Layer S / SpecEvaluator 的关系

插件 v0.3 起，五层防御扩为六层：Layer 0 LSP / Layer 1 compile / **Layer S
style audit** / Layer 2 build+QEMU / Layer 3 SpecEvaluator / Layer 4 user。

| 自审层 | 对应报告 | 抓什么 |
|---|---|---|
| **Layer S 风格审计** | `docs/exfat_style_audit.md`（6/7 通过；super.c 长函数 hard 违例） | 命名 / 函数复杂度 / 代码布局 / libsec / 加锁 / 错误路径 |
| **Layer 3 SpecEvaluator** | `docs/exfat_speceval.md`（7/7 通过） | 代码与规范的语义等价（签名、加锁、helper、libsec、Linux 原语翻译、goto-stack） |
| **Layer A cmocka + Layer B LTP** | `docs/test/exfat_regression_latest.md` | 实测行为：49 cmocka 测点 + 6 LTP smoke 用例 |

三者**正交互补**：Layer S 看代码"长得像不像"；SpecEval 看代码"做没做对"；
回归看代码"跑起来对不对"。

### 后续 增量

新加 stage（如 lookup、read）按本节模板落地：
- 在 `testsuites/unittest/exfat/` 加对应 `test_<subsystem>.c`，更新 Makefile `PROD_SRCS` 与 `main.c` 的 `extern` + `run_suite()`。
- 在 LTP smoke 集合追加 `readdir01 / chmod01` 等新覆盖的用例（裁剪脚本里改一行）。
- 跑一次 `tools/regress/run_all.sh`，把新报告 commit 进 PR。
