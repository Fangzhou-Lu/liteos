/* Host stub for drivers/block/disk/include/disk.h:
 *   los_part_read / los_part_write are routed to mock_disk.c.
 *   los_part_find / SetDiskPartName are unused by the helpers we test —
 *   only super.c needs them, and super.c is NOT in the cmocka build. */
#ifndef _HOST_STUB_DISK_H
#define _HOST_STUB_DISK_H

#include "los_typedef.h"

#define ENOERR 0

INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);
/* NB: real LiteOS-A signature has 4 args (no BOOL useWrite); see
 * drivers/block/disk/include/disk.h:485. The stub mirrors that exactly so
 * exfat_write.c compiles unchanged in the host harness. */
INT32 los_part_write(INT32 pt, const VOID *buf, UINT64 sector, UINT32 count);

#endif
