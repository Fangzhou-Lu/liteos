[PROMPT]
LiteOS-A 的 `chmod(2)` / `chown(2)` 通过 VFS `vop->Chattr(vnode, IATTR *)`
回调进入文件系统层（公共层入口在 `fs/vfs/operation/vfs_chattr.c`、用户态
封装在 `fs/vfs/operation/vfs_other.c::chmod`）。`g_exfatVops.Chattr` 在 Wave A
保持 NULL 的"v1 invariant exfat-vfs-stub-null-trap"导致任何 chmod/chown 调用
被 VFS 翻译为 -ENOSYS，进而 LTP framework 与 chmod01/unlink05/open01 等用例
直接被 TBROK/TFAIL，无法继续。

行为锚点参考 `/Users/kissa/Codebase/linux/fs/exfat/file.c::exfat_setattr`：
exFAT 盘上 dentry 不存储 POSIX 权限位 / uid / gid（这与 FAT 系列一致；
mount 时 `fs_uid` / `fs_gid` / `fs_fmask` / `fs_dmask` 决定每个文件的初始
mode/uid/gid，存活于 in-memory `struct Vnode` 字段）。因此 chmod/chown
本质是 **in-memory 元数据更新**，不触发任何 `los_part_write` / FAT / dentry
变更。Linux exfat `setattr_copy` 只把 inode->i_mode/i_uid/i_gid 等字段
copy 进 in-memory inode，再 `mark_inode_dirty` — LiteOS-A 端没有 page-cache
writeback queue，且盘上无对应槽位，所以省略 mark dirty，直接更新 vnode
字段即可。

本路径 `VfsExfatChattr` 是 Wave B 后期补的 stage，原因：mmap/munmap 修复后
LTP 用例可以正常进入 cleanup 阶段，cleanup 路径在 `tst_test.c:119` /
`tst_tmpdir.c:291` 调 `chmod(..., 0666)`、`chmod(..., 0777)`。LTP framework
对 ENOSYS 已经做了"warn 后继续"的兼容处理；但 `chmod01.c:65` 等用例**本身**
就在测试 chmod 行为，必须返回 0 才能继续后续断言。

Out of scope（本 stage 显式不做）：
- `attr_chg_size` 路径：尺寸变化由 truncate VOP 独立承担（spec
  exfat_truncate_vop.spec 已批准）。本 chattr 路径若收到 CHG_SIZE bit，
  忽略它不处理 size（POSIX `chmod` 不会带 CHG_SIZE；`truncate(2)` 走
  `vop->Truncate` 而非 `Chattr`，公共层路径分离）。
- atime/mtime/ctime 持久化：v1 chattr 接收 CHG_ATIME / CHG_MTIME / CHG_CTIME
  时只更新 `exfat_inode_info` 内存字段（`ei->atime_sec` 等），不写盘。
  这与 exfat_parent_metadata_sync 的 best-effort 策略一致。
- on-disk dentry mark dirty：v1 不实现 deferred dentry sync；如未来 Wave B
  补 fsync 路径，可在 `Chattr` 末尾追加 `exfat_sync_parent_dir_metadata`，
  但这是 evolve 后续。
- POSIX permission policy check：v1 chattr 不做 setuid/setgid 等高权位过滤
  （Linux exfat 也只过滤 ATTR_MODE 的非 0777 位以避免 setuid/sticky 持久化
  到不存在的盘上位）；LiteOS-A `vop->Chattr` 调用方公共层未做此过滤，留
  v2 处理。

[RELY]
```c
/* common.header 已冻结类型 */
typedef struct exfat_sb_info exfat_sb_info;
typedef struct exfat_inode_info exfat_inode_info;

/* LiteOS-A VFS 类型 */
struct Vnode;
struct IATTR;                       /* fs/vfs/include/vnode.h */

/* IATTR 字段位 — 由 fs/vfs/include/vnode.h 定义 */
#define CHG_MODE   1u
#define CHG_UID    2u
#define CHG_GID    4u
#define CHG_SIZE   8u
#define CHG_ATIME  16u
#define CHG_MTIME  32u
#define CHG_CTIME  64u

/* Vnode 中由本路径可改的字段（in-memory only）：
 *   vp->mode  : 完整 mode_t（含 S_IF* + perm 位）
 *   vp->uid   : uint32_t
 *   vp->gid   : uint32_t
 * Vnode 中的 type 字段（VNODE_TYPE_REG / DIR / ...）不允许被 chattr 修改。
 */

/* exfat 时间锚点（由 inode_metadata_model stage 提供，可选调用）*/
extern void exfat_inode_touch_ctime(exfat_inode_info *ei);

/* errno（POSIX 负值边界返回）*/
#define EINVAL 22
```

[GUARANTEE]
```c
/*
 * 调用约定：
 *   - VnodeOps.Chattr 回调，由公共层 `vfs_chattr.c::chattr` 派发；
 *     上游 chmod(2) / chown(2) / utimes(2) 共享同一入口。
 *   - 调用方未持 exfat 锁；本函数不取 sbi->s_lock / sbi->bitmap_lock；
 *     不调 IO；不分配堆；不写盘。
 *   - 锁序：可选 ei->inode_lock 短暂持有以序列化跨 chattr/getattr/read/write
 *     的 in-memory 字段读写；本 v1 选择不取锁 — 字段是单字赋值，LiteOS-A
 *     ARMv7 / ARMv8 在对齐 32 位字写入上原子；竞态最坏结果是读到旧 mode
 *     值，不引发崩溃。该决策与 Linux setattr_copy 内部不取 i_rwsem
 *     (由公共上层 vfs_setattr 持) 的对称取舍一致。
 *
 * 返回值：
 *   - 0 ：成功。in-memory 字段已按 attr_chg_valid 掩码更新。
 *   - <0：负 POSIX errno（仅 -EINVAL；其他 errno 由公共层处理）。
 *
 * 副作用：
 *   - 当 (CHG_MODE) bit 置位：vp->mode 更新为 attr->attr_chg_mode，
 *     **保留** vp->mode 中原本的 S_IFMT 位（type 不允许被 chattr 改变；
 *     mode 入参的 S_IFMT 位被丢弃）。
 *   - 当 (CHG_UID) bit 置位：vp->uid = attr->attr_chg_uid。
 *   - 当 (CHG_GID) bit 置位：vp->gid = attr->attr_chg_gid。
 *   - 当 (CHG_SIZE) bit 置位：忽略（v1 不在 Chattr 路径走 size；
 *     公共层 chsize / truncate 走 vop->Truncate）。
 *   - 当 (CHG_ATIME / CHG_MTIME / CHG_CTIME) bit 置位：更新
 *     ei->atime_sec / mtime_sec / ctime_sec（in-memory only，不写盘）。
 *   - 不修改 sbi 任何字段；不修改 ei->dir / entry / start_clu / flags /
 *     size / valid_size / i_size_ondisk / type / attr / num_subdirs。
 *
 * Pre：vnode != NULL && vnode->data != NULL && vnode->originMount != NULL；
 *      attr != NULL；attr_chg_valid 是 CHG_* 位的或组合。
 */
extern int VfsExfatChattr(struct Vnode *vnode, struct IATTR *attr);
```

[SPECIFICATION]

**Pre-Condition**：
1. `vnode != NULL` 且 `vnode->originMount != NULL` 且 `vnode->data` 是有效
   `exfat_inode_info`。
2. `attr != NULL`。
3. `attr->attr_chg_valid` 是若干 `CHG_*` 位的按位或；未识别的位被忽略。

**Post-Condition (Case 1: Success — full or noop)**：
- 按 `attr_chg_valid` 中的位逐项处理：
  - CHG_MODE：`vp->mode = (vp->mode & S_IFMT) | (attr->attr_chg_mode & 0777u)`
    （保留 type 位、只用权限位；setuid/setgid/sticky 静默丢弃，**不**报错）。
  - CHG_UID：若 `attr->attr_chg_uid == vp->uid` → no-op success；否则见 Case 4。
  - CHG_GID：若 `attr->attr_chg_gid == vp->gid` → no-op success；否则见 Case 4。
  - CHG_ATIME：`ei->atime_sec = attr->attr_chg_atime`。
  - CHG_MTIME：`ei->mtime_sec = attr->attr_chg_mtime`。
  - CHG_CTIME：`ei->ctime_sec = attr->attr_chg_ctime`；否则若有任何字段被
    修改则调 `exfat_inode_touch_ctime(ei)` 推进 ctime（mirror Linux
    setattr_copy 在 ATTR_MODE/UID/GID 变化时 inode 的 i_ctime 自动推进）。
- 返回 0。

**Post-Condition (Case 2: Args 校验失败)**：
- `vnode == NULL` 或 `attr == NULL` 或 `vnode->data == NULL`：返回 `-EINVAL`；
  不修改任何状态。

**Post-Condition (Case 3: No-op)**：
- `attr_chg_valid == 0` 或仅 CHG_SIZE：返回 0；不修改任何字段
  （CHG_SIZE 由 truncate VOP 处理，本路径忽略）。

**Post-Condition (Case 4: UID / GID mismatch)**：
- `CHG_UID` 置位且 `attr->attr_chg_uid != vp->uid`：返回 `-EPERM`；
  **不修改任何字段**（mode / atime / mtime / ctime 也保持原值）。
- `CHG_GID` 置位且 `attr->attr_chg_gid != vp->gid`：同理返回 `-EPERM`。
- 该行为镜像 Linux `fs/exfat/file.c::exfat_setattr` 在 `attr->ia_uid !=
  sbi->options.fs_uid` 时的 -EPERM 分支；LiteOS-A 以 `vp->uid/gid` 表示
  in-memory 当前 owner（mount 初始化为 fs_uid/fs_gid，本路径不演进）。
- 检查顺序：先于 Phase 2 任何字段写入，确保失败时副作用为零。

**Invariant** (id=exfat-chattr-no-io)：
本函数不调用 `los_part_read` / `los_part_write` / `los_disk_*`；不读 / 不写
任何分区扇区；不修改 sbi 任何字段。盘上无对应槽位，purely in-memory。

**Invariant** (id=exfat-chattr-no-locks)：
本函数不取 `sbi->s_lock` / `sbi->bitmap_lock` / `sbi->inode_hash_lock` /
`ei->inode_lock`。in-memory 字段写是单字原子，竞态最坏后果是观察方读旧值，
不引发崩溃。如未来要加锁，应只加 `ei->inode_lock`（与 getattr/read/write
对称）。

**Invariant** (id=exfat-chattr-mode-preserves-type)：
CHG_MODE 路径**必须**保留 `vp->mode & S_IFMT` 中的文件类型位；
attr->attr_chg_mode 入参的高位（S_IFREG / S_IFDIR / ...）一律丢弃。
chmod 永远不会改变 inode 类型（这是 POSIX 硬约束）。

**Invariant** (id=exfat-chattr-mode-perm-mask)：
CHG_MODE 路径只写入 attr->attr_chg_mode 的低 9 位（`& 0777u`）。
不持久化 setuid/setgid/sticky 位（exFAT 盘上无对应位）。Linux 在
`exfat_setattr` 内对 `ia_mode & ~(S_IFREG|S_IFLNK|S_IFDIR|0777)` 含位
**返回 -EPERM**，但随即又在 `exfat_sanitize_mode` 分支静默清掉这些位
（注释 "Yes, strange, but this is too old behavior"）。LiteOS-A v1
采用静默清掉路径，不在 CHG_MODE 高位上额外返 -EPERM —— 因为 LTP
chmod01 实测在 Linux native lane 也只到 TFAIL（RC=1）而非 TBROK，
两种解释均能通过 access01/chmod01 的 framework setup。

**Invariant** (id=exfat-chattr-uid-gid-eperm-on-mismatch)：
CHG_UID / CHG_GID 路径**仅在 new value 与 vp->uid / vp->gid 不等时**
返回 -EPERM；相等（"no-op chown"）视为成功并完成其他位的更新。该行为
直接镜像 Linux `exfat_setattr` 的 uid/gid 校验分支，并且是 LTP framework
`tst_tmpdir.c:289` 调 `chown(path, -1, getgid())` 能成功的根因。
**这是修复 Wave 4 三向对比中 LiteOS-A vs Linux 在 access01/chmod01 上
分歧的关键 invariant。**

**Invariant** (id=exfat-chattr-no-disk-mutation)：
本路径任何分支都不会修改 dentry / FAT / bitmap / boot sector / vol_flags。
失败重试 / 系统崩溃前后 chattr 操作的可观测效应仅限于 in-memory；
重启后所有文件的 mode/uid/gid 回到 mount 时由 fs_uid/fs_gid/fs_fmask/
fs_dmask 决定的初值。这与 FAT 家族（fatfs / FatFs）的行为完全一致。

**Invariant** (id=exfat-chattr-no-spinlock-callsite)：
本函数无 IO / 无 alloc / 无 mutex 操作；可在持 LiteOS spinlock 时调用。
（与同 stage 的其它纯计算函数如 exfat_inode_touch_* 同精神。）

**Invariant** (id=exfat-chattr-size-ignored-here)：
CHG_SIZE bit 在本路径**忽略**（不报错，因为公共层组合调用 chmod+truncate
可能在同一 IATTR 中带上 size）；尺寸变更由公共层独立调度
`vop->Truncate` 完成。本不变量避免与 truncate VOP 重复扩展 / 收缩
导致双写。

**Invariant** (id=exfat-chattr-best-effort-ctime)：
若任一字段被修改且 attr_chg_valid 不显式包含 CHG_CTIME，则调用
exfat_inode_touch_ctime 推进 ctime（与 Linux setattr_copy 同语义）。
该调用是 in-memory only，与 invariant exfat-chattr-no-io 不冲突。

**System Algorithm**

Phase 1 — Argument validation
  - Goal: 拒绝 NULL/损坏入参，确保 vnode/ei/attr 有效。
  - Algorithm:
      1. if (vnode == NULL || attr == NULL) return -EINVAL;
      2. if (vnode->data == NULL || vnode->originMount == NULL) return -EINVAL;
  - Pre-Post: 失败路径不修改任何状态。
  - Error Handling: 直接返回 -EINVAL，无 PRINT_ERR（这是参数错误，不是
    文件系统错误，公共层会按需 set_errno）。

Phase 2 — UID / GID mismatch gate（pre-check before any mutation）
  - Goal: 阻止"换 owner"语义；only no-op chown 通过。
  - Algorithm:
      1. if ((valid & CHG_UID) && attr->attr_chg_uid != vnode->uid)
             return -EPERM;
      2. if ((valid & CHG_GID) && attr->attr_chg_gid != vnode->gid)
             return -EPERM;
  - Pre-Post: 任一失败返回前不修改任何 vnode/ei 字段（atomicity）。
  - Error Handling: 直接 -EPERM；无 PRINT_ERR。

Phase 3 — Apply CHG_MODE / CHG_UID / CHG_GID（in-memory）
  - Goal: 按位更新 vnode 上的权限相关字段，保留 type 位。CHG_UID/CHG_GID
    走到这里时一定是 no-op（值已 match），写回保持幂等。
  - Algorithm:
      1. if (valid & CHG_MODE) vnode->mode =
             (vnode->mode & S_IFMT) | (attr->attr_chg_mode & 0777u);
      2. if (valid & CHG_UID)  vnode->uid = attr->attr_chg_uid;  /* identity write */
      3. if (valid & CHG_GID)  vnode->gid = attr->attr_chg_gid;  /* identity write */
  - Pre-Post: vnode->type 不变；vnode->mode 的 S_IFMT 位不变。
  - Error Handling: 无失败路径（in-memory 写不会失败）。

Phase 4 — Apply CHG_{ATIME, MTIME, CTIME}（in-memory ei）
  - Goal: 按位更新 exfat_inode_info 时间字段，不写盘。
  - Algorithm:
      1. ei = vnode->data;
      2. if (valid & CHG_ATIME) ei->atime_sec = attr->attr_chg_atime;
      3. if (valid & CHG_MTIME) ei->mtime_sec = attr->attr_chg_mtime;
      4. if (valid & CHG_CTIME) ei->ctime_sec = attr->attr_chg_ctime;
         else if (Phase 2/3 任一字段被改) exfat_inode_touch_ctime(ei);
  - Pre-Post: 盘上 dentry 时间字段不变；in-memory ei 字段已更新。
  - Error Handling: 无失败路径。

Phase 5 — Return success
  - Goal: 表明 chattr 已完成（in-memory only 语义对 LTP / POSIX 调用方
    透明）。
  - Algorithm: return 0;
  - Pre-Post: vnode->mode/uid/gid 与 ei->*time_sec 已就绪；其余字段不变。
  - Error Handling: 不适用。
