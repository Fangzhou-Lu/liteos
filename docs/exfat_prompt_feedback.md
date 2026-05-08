# exfat — Prompt Feedback Log

Linux↔generated functional-comparison findings, captured by Loop C (`linux_compare_submit`). This file is HITL-curated — the plugin never auto-rewrites prompt templates from these recommendations. Promote useful items into `prompts/linux_to_spec.md` or `prompts/codegen.md` (or their fragments) by hand after review.


## unlink — 2026-05-07T18:22:00Z

- **is_equivalent**: `False`
- **summary**: Generated unlink covers the on-disk dentry-set mutation, volume-dirty bracket, in-memory tombstone, and synchronous cluster free — all matching Linux semantically. The two semantically-meaningful gaps are: (1) parent-directory metadata refresh (mtime/atime/iversion + dirty mark) is performed in Linux but absent in spec & code; (2) target-inode hash-table eviction (Linux exfat_unhash_inode) has no LiteOS-A counterpart in either spec or code, so a concurrent VfsHashGet could still hand out the dying inode between Phase 1 unlock and Reclaim. 5 gaps total: 2 high, 2 med, 1 low. Prompt-tuning recommendations focus on enforcing inode-metadata enumeration in spec, and adding a post-tombstone hash-eviction reminder to LITEOS_DIGEST.
- **counts**: spec_gaps=5, code_gaps=3

### spec_gaps
- **[high/missing_invariant]** Spec has no invariant or post-condition requiring eviction of the target inode from any name-based lookup table. Linux removes the inode from its hash via exfat_unhash_inode so future lookups cannot resolve to a tombstoned inode; LiteOS-A's path_cache / VfsHash equivalent is the analog and must be required by spec.
  - Linux: `fs/exfat/namei.c:830 - `exfat_unhash_inode(inode);``
  - Spec Location: `[SPECIFICATION] Post-Condition Case 1 lists tombstone + cluster free but is silent on hash eviction.`
  - Fix: Add Invariant (id=exfat-unlink-name-cache-evicted): on success, the target Vnode is no longer reachable via path_cache lookup of (parent, fileName); the FS must explicitly evict before Phase 1 unlock or document the LiteOS-A hook that performs it.
- **[high/under_specified_post]** Spec is silent on parent-directory metadata mutation. Linux mutates dir->i_mtime / i_atime, calls inode_inc_iversion(dir), and mark_inode_dirty(dir) so observers see a directory change. Without this in the spec, codegen had no obligation to produce it, and a userspace observer reading the parent dir's stat() will not see a refreshed mtime after unlink.
  - Linux: `fs/exfat/namei.c:819-825 - inode_inc_iversion(dir); dir->i_mtime = dir->i_atime = current_time(dir); ... mark_inode_dirty(dir);`
  - Spec Location: `[SPECIFICATION] Post-Condition Case 1 only enumerates target dentry-set + cluster free.`
  - Fix: Add a Post-Condition bullet under Case 1: parent directory's modification timestamp is refreshed and the directory dentry is queued for write-back. Either add an Invariant pinning the parent's i_mtime monotonic-update, or explicitly carve it out as 'deferred to a future stage' if v1 won't ship that path.
- **[med/under_specified_post]** Spec omits target-inode link-count / mtime updates. Linux calls clear_nlink(inode) so subsequent stat() on a still-open fd returns nlink=0, and updates i_mtime/i_atime. The spec's 'Reclaim handles freeing' note conflates lifecycle (Reclaim) with metadata refresh (which Linux does inline in unlink).
  - Linux: `fs/exfat/namei.c:827-829 - clear_nlink(inode); inode->i_mtime = inode->i_atime = current_time(inode);`
  - Spec Location: `[SPECIFICATION] Pre/Post discusses target_ei->dir.dir + start_clu only.`
  - Fix: Add a Post-Condition Case 1 line: 'target inode's link count is zeroed and its mtime/atime updated, observable via stat() on a still-open fd.' Or add an Invariant exfat-unlink-target-nlink-cleared.
- **[med/missing_invariant]** Spec's System Algorithm Phase 2 builds chain = {target_ei->start_clu, ...} and frees, but does NOT require resetting target_ei->start_clu to EXFAT_FREE_CLUSTER (or equivalent sentinel) after free. If a buggy Reclaim or a future evolve-stage ever re-derives the chain from target_ei after unlink, it could re-free the same clusters. Linux sidesteps this because it defers free to truncate-on-iput which itself resets the chain via exfat_chain_set.
  - Linux: `fs/exfat/inode.c:exfat_evict_inode and fs/exfat/super.c:exfat_truncate - chain reset via exfat_chain_set(&ei->dir, EXFAT_FREE_CLUSTER, 0, ALLOC_NO_FAT_CHAIN).`
  - Spec Location: `[SPECIFICATION] Phase 2 step 2 ends after exfat_free_cluster; no chain-reset step.`
  - Fix: Add Phase 2 step 3: 'reset target_ei->start_clu = EXFAT_FREE_CLUSTER and target_ei->flags to ALLOC_NO_FAT_CHAIN to prevent double-free in any future evolve-stage Reclaim path.' Add Invariant exfat-unlink-chain-reset-after-free.
- **[low/under_specified_pre]** Pre-Condition does not specify the input domain of target_ei->start_clu. If start_clu == EXFAT_FREE_CLUSTER (an empty file with no allocated chain), Phase 2 still calls exfat_free_cluster unconditionally. Linux's path implicitly handles this via the truncate-on-iput path. Spec should either guard or document that exfat_free_cluster must be a no-op on EXFAT_FREE_CLUSTER input.
  - Linux: `fs/exfat/balloc.c:exfat_free_cluster - first action is to skip if p_chain->dir is EXFAT_FREE_CLUSTER.`
  - Spec Location: `[SPECIFICATION] Pre-Condition does not enumerate empty-file case.`
  - Fix: Add a Pre-Condition bullet: 'target_ei->start_clu may be EXFAT_FREE_CLUSTER for an empty file; exfat_free_cluster MUST treat that as a no-op'. Or add a guard branch in Phase 2 step 1.

### code_gaps
- **[high/missing_step]** Generated VfsExfatUnlink does not evict the target Vnode from any name-based cache. Linux exfat_unhash_inode runs inline in unlink. Without the LiteOS-A equivalent, a concurrent VfsHashGet on (parent_mount, target_inode_hash) can still resolve to the dying inode between Phase 1 unlock and the eventual Reclaim trigger.
  - Linux: `fs/exfat/namei.c:830 - exfat_unhash_inode(inode);`
  - Code Location: `fs/exfat/exfat_inode.c:1601 - tombstone is set, but no VfsHashRemove / path_cache eviction follows.`
  - Root cause: `spec_under_specified`
  - Fix: Fix at spec layer first via spec_fine to add the eviction Invariant; then code adds a `VfsHashRemove(target_vp)` (or path_cache equivalent) immediately after the tombstone, before Phase 1 unlock.
- **[high/missing_step]** Generated VfsExfatUnlink does not refresh parent_vp's metadata. Linux updates dir->i_mtime/i_atime + inode_inc_iversion(dir) + mark_inode_dirty(dir). After our unlink, a stat() on the parent directory shows stale mtime — visible userspace bug.
  - Linux: `fs/exfat/namei.c:819-825 - parent dir mtime/atime/iversion update + mark_inode_dirty(dir).`
  - Code Location: `fs/exfat/exfat_inode.c:1612 - phase1_unlock falls straight through to Phase 2; no parent metadata block.`
  - Root cause: `spec_under_specified`
  - Fix: Fix at spec layer first; then code adds — between Phase 1 unlock and Phase 2 — an update of parent_ei's mtime + a path_cache or vnode-dirty mark on parent_vp.
- **[med/missing_step]** Generated VfsExfatUnlink does not reset target_ei->start_clu after exfat_free_cluster, leaving the inode_info in a state where start_clu still points at freed bitmap bits. Reclaim does not currently re-free, but any future evolve-stage that does will see a stale chain and re-free the same range.
  - Linux: `fs/exfat/inode.c:exfat_evict_inode + fs/exfat/super.c:exfat_truncate together ensure ei->start_clu is reset via exfat_chain_set after free.`
  - Code Location: `fs/exfat/exfat_inode.c:1617 - exfat_free_cluster is called, then function returns 0 without touching target_ei->start_clu.`
  - Root cause: `spec_under_specified`
  - Fix: Fix at spec layer first; then code appends after the free: target_ei->start_clu = EXFAT_FREE_CLUSTER; target_ei->flags = ALLOC_NO_FAT_CHAIN;

### spec_prompt_recommendations (additive)
- Add to [SCOPE GUARDRAILS] of prompts/linux_to_spec.md: when porting a VFS callback, enumerate EVERY in-memory mutation Linux performs on (a) the parent inode, (b) the target inode, (c) the parent's containing super block, (d) the dentry hash / name cache. Each mutation must produce either an Invariant or an explicit `# OUT-OF-SCOPE: <reason>` line in the spec — generic 'VFS / Reclaim handles it' is forbidden.
- Add to prompts/linux_to_spec.md [SPECIFICATION] guidance: post-tombstone semantics demand a separate sub-section. When a stage sets a deletion sentinel (DIR_DELETED, FREE_CLUSTER, NULL, etc.), the spec must enumerate (a) what name-resolution caches must be evicted before the sentinel becomes visible, and (b) which inode-state fields must be cleared so a later evolve-stage cannot re-process the dead entity.
- Add to prompts/linux_to_spec.md [SCOPE GUARDRAILS]: parent vs target vs sibling separation — Pre/Post-Condition bullets must be grouped under explicit sub-headings 'on parent', 'on target', 'on sibling state' so the spec author cannot conflate them. The Linux-source survey step must walk every dereferenced inode/dentry and assign it to one of the three groups before drafting Post-Conditions.

### codegen_prompt_recommendations (additive)
- Add to prompts/liteos_digest.md (or [LITEOS-A DIGEST] section of codegen.md): when spec carries a tombstone Invariant, also evict the target Vnode from path_cache / VfsHashRemove BEFORE the lock that protects the tombstone is released — Reclaim runs at refcount=0 which is not synchronous with unlink and is unsafe for concurrent-lookup blocking.
- Add to prompts/liteos_digest.md: parent-inode metadata refresh is the caller's responsibility in Linux (mtime/atime/iversion + mark_inode_dirty) — when porting an unlink-style mutation, the LiteOS-A equivalent (vnode dirty mark + parent's exfat_inode_info timestamp update) must be emitted between the s_lock unlock and any deferred Phase-2 work.
- Add to prompts/codegen.md output rules: when [GUARANTEE] declares Phase 2 lock-free with respect to a primary lock but reads inode-info fields (start_clu, flags), the generated code must SAMPLE those fields into stack locals BEFORE releasing the primary lock. In addition, after a free / unlink completes, RESET those fields to their sentinel value so a future Phase-2 evolve cannot re-run the operation.
