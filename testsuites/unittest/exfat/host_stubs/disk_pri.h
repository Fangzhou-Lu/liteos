/* Host stub for drivers/block/disk/include/disk_pri.h.
 * exfat_super.c references los_part / los_part_find / get_part during the
 * Mount and Unmount narratives — none of which run during cmocka tests
 * (only FS-internal helpers are exercised). Stubs let the file compile. */
#ifndef _HOST_STUB_DISK_PRI_H
#define _HOST_STUB_DISK_PRI_H

#include "vnode.h"
#include <stdint.h>
#include <stddef.h>

typedef struct los_part {
    int    part_id;
    void  *dev;
    char  *part_name;
} los_part;

static inline los_part *los_part_find(struct Vnode *blk)
{
    (void)blk;
    return NULL;
}

static inline los_part *get_part(int part_id)
{
    (void)part_id;
    return NULL;
}

static inline int SetDiskPartName(los_part *part, const char *name)
{
    (void)part;
    (void)name;
    return 0;
}

#endif /* _HOST_STUB_DISK_PRI_H */
