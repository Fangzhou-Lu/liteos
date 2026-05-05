/* Host stub for kernel/include/los_tables.h.
 * The real macro emits a linker-section entry that aggregates filesystem
 * registrations into g_fsmap[]. The host cmocka harness exercises individual
 * helpers (chksum, options, dentry, balloc, ...) directly — never the VFS
 * mount path — so the registration is irrelevant. Make it a no-op that still
 * preserves the syntactic shape (`...;` follows in the source). */
#ifndef _HOST_STUB_LOS_TABLES_H
#define _HOST_STUB_LOS_TABLES_H

#define LOS_HAL_TABLE_ENTRY(_label, _section)  /* no-op on host */

/* FSMAP_ENTRY is defined in fs-private headers atop LOS_HAL_TABLE_ENTRY in
 * the real tree. Define it equivalently as a no-op declaration so the source
 * line `FSMAP_ENTRY(name, "fsname", ops, false, true);` compiles to nothing.
 * The trailing semicolon in the call site is consumed by an empty extern
 * declaration. */
#define FSMAP_ENTRY(_label, _name, _ops, _flag1, _flag2) \
    extern int _label##_unused_host_decl

#endif /* _HOST_STUB_LOS_TABLES_H */
