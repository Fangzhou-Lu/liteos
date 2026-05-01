[PROMPT]
LiteOS-A 实现 exFAT 的 VFS 操作表占位定义：

1. **`g_exfatVops`**（`struct VnodeOps`）—— vnode 级回调表（lookup/create/unlink/
   getattr/setattr/...）。
2. **`g_exfatFops`**（`struct file_operations_vfs`）—— file 级回调表（open/close/
   read/write/seek/...）。

`v1 mount` 路径仅依赖这两个符号的**地址**：
- `VnodeAlloc(&g_exfatVops, ...)` 把 root vnode 绑到 vops；
- `vp->fop = &g_exfatFops;` 写入 file ops 指针。

所有具体回调函数实现归属后续阶段（lookup-v1、readdir-v1、open-v1、read-v1
等独立的 .spec 与 .c）；本阶段把表项**统一初始化为 NULL**——LiteOS-A VFS 框架
对 NULL 回调自动返回 `-ENOSYS`，达到"v1 mount 能挂、后续操作失败但安全"的状态。

依赖：无（结构体定义来自 fs/vfs/include/vnode.h 与 fs/include/fs/file.h，已
被 common.header 间接覆盖；本阶段不引入新 [RELY]）。

## First Prompt

[RELY]
```c
/* —— common.header 已经过 vnode、mount、file 等头的导入，提供
 *    struct VnodeOps、struct file_operations_vfs 完整定义 ——
 *
 * 本阶段不再 [RELY] 任何额外接口——它的工作仅仅是符号绑定。
 */
```

[GUARANTEE]
```c
/*
 * g_exfatVops — vnode operation table for exFAT.
 *
 * 调用约定（变量级）：
 *   - 文件作用域（非 static）；地址在链接期对外可见。
 *   - 由 VnodeAlloc(&g_exfatVops, ...) 在 mount 路径绑定到 root vnode。
 *   - 后续阶段（lookup-v1 等）会通过 spec_gen_refine 增加新字段，但**不改名**、
 *     **不分裂** —— 此 invariant 由 mount.spec 的 exports 列表锚定。
 *
 * 副作用：链接期把全 NULL 的 struct VnodeOps 实例放到 .data 段；运行时框架
 *        在调用任一 NULL 回调时返回 -ENOSYS。
 */
extern struct VnodeOps g_exfatVops;

/*
 * g_exfatFops — file operation table for exFAT.
 *
 * 调用约定与 g_exfatVops 同；由 VfsExfatMount 写入 root vp->fop。
 */
extern struct file_operations_vfs g_exfatFops;
```

[SPECIFICATION]

**Pre-Condition**:
- 编译期符号；无运行时前提。

**Post-Condition (link-time)**:

**Case 1（成功）**：
- 翻译单元（fs/exfat/exfat_inode.c 与 fs/exfat/exfat_file.c，或合并为单一
  fs/exfat/exfat_ops.c）以 file scope 定义两个全局变量：
  ```c
  struct VnodeOps             g_exfatVops = { 0 };
  struct file_operations_vfs  g_exfatFops = { 0 };
  ```
  全 NULL 初始化保证：
    - VFS 调用 vops->Lookup / vops->Create 等任一字段时拿到 NULL；
    - LiteOS-A VFS 框架对 NULL 回调返回 `-ENOSYS`（这是框架既定行为，
      不是本规范要求实现的逻辑）。

**Invariant** (id=exfat-vfs-stub-symbol-stable):
后续阶段（lookup/read/...）通过 spec_gen_refine 在该翻译单元内**修改字段
初值**，但绝**不重命名**符号、**不改变作用域**、**不拆分到多个变量**。
这是 mount.spec exports `g_exfatVops, g_exfatFops` 的 ABI 锚点。

**Invariant** (id=exfat-vfs-stub-null-trap):
v1 阶段所有字段为 NULL；任一 vfs 回调被调用即陷入 -ENOSYS。本约束意味着
v1 mount 之后的任何 lookup/read/write 调用都会优雅失败而非未定义行为。

**Invariant** (id=exfat-vfs-stub-no-runtime-init):
不需要在 mount 路径或其他位置调用任何 init 函数初始化这两个表；它们是
**编译期常量**（最初版本——后续 refine 仍保持 .data 静态初始化语义）。

**Invariant** (id=exfat-vfs-stub-loscfg-gated):
两表的定义包在 `#ifdef LOSCFG_FS_EXFAT ... #endif` 内，与 mount.spec
exfat-mount-fsmap-entry-name invariant 配套——LOSCFG 关闭时整个 fs/exfat/
不参与链接，符号也不存在。
