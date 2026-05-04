/*
 * Host-side stub for kernel/include/los_typedef.h.
 * Compiled only with the exfat cmocka host harness — never linked into the kernel.
 *
 * _GNU_SOURCE must be defined before ANY system header to unlock blksize_t /
 * blkcnt_t / loff_t from glibc <sys/types.h>. Use #ifndef so an outer
 * -D_GNU_SOURCE on the compiler command line also works.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#ifndef _HOST_STUB_LOS_TYPEDEF_H
#define _HOST_STUB_LOS_TYPEDEF_H

#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>   /* off_t / loff_t / ssize_t */
#include <unistd.h>      /* SEEK_SET / SEEK_CUR / SEEK_END */

/* blksize_t / blkcnt_t: defined by glibc only under _GNU_SOURCE, which may
 * arrive too late when <sys/stat.h> is included before our stub headers.
 * On glibc we forward from the underlying private types. On Darwin (macOS)
 * <sys/types.h> already provides both unconditionally; the typedefs below
 * would error on `__blksize_t` (glibc-only) so skip the workaround. */
#if defined(__GLIBC__)
#ifndef __blksize_t_defined
typedef __blksize_t blksize_t;
#define __blksize_t_defined
#endif
#ifndef __blkcnt_t_defined
typedef __blkcnt_t blkcnt_t;
#define __blkcnt_t_defined
#endif
#endif /* __GLIBC__ */

typedef int32_t  INT32;
typedef uint32_t UINT32;
typedef int8_t   INT8;
typedef uint8_t  UINT8;
typedef int16_t  INT16;
typedef uint16_t UINT16;
typedef int64_t  INT64;
typedef uint64_t UINT64;
typedef void     VOID;
typedef int      BOOL;

#ifndef TRUE
#define TRUE  1
#endif
#ifndef FALSE
#define FALSE 0
#endif

#define LOS_OK   0
#define LOS_NOK  1

#endif
