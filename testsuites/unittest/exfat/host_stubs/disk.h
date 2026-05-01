/* Host stub for drivers/block/disk/include/disk.h:
 *   los_part_read / los_part_write are routed to mock_disk.c.
 *   los_part_find / SetDiskPartName are unused by the helpers we test —
 *   only super.c needs them, and super.c is NOT in the cmocka build. */
#ifndef _HOST_STUB_DISK_H
#define _HOST_STUB_DISK_H

#include "los_typedef.h"

#define ENOERR 0

INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead);
INT32 los_part_write(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useWrite);

#endif
