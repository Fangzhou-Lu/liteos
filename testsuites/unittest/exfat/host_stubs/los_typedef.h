/*
 * Host-side stub for kernel/include/los_typedef.h.
 * Compiled only with the exfat cmocka host harness — never linked into the kernel.
 */
#ifndef _HOST_STUB_LOS_TYPEDEF_H
#define _HOST_STUB_LOS_TYPEDEF_H

#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>   /* host glibc provides off_t / loff_t / ssize_t */

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
