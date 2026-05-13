#ifndef _HOST_STUB_LOS_VM_FILEMAP_H
#define _HOST_STUB_LOS_VM_FILEMAP_H

#include "fs/file.h"

#ifndef LOS_OK
#define LOS_OK 0
#endif

typedef struct VmMapRegion LosVmMapRegion;

static inline int OsVfsFileMmap(struct file *filep, LosVmMapRegion *region)
{
    (void)filep;
    (void)region;
    return LOS_OK;
}

#endif /* _HOST_STUB_LOS_VM_FILEMAP_H */
