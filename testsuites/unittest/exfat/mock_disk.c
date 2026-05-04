/*
 * mock_disk — see mock_disk.h.
 *
 * Implementation detail: the in-memory image is a single contiguous buffer.
 * los_part_read pulls (sector * 512 .. sector*512 + count*512) bytes out
 * (out-of-range reads return -1 → -EIO). los_part_write copies into the
 * snapshot but never persists.
 */

#include "mock_disk.h"
#include "host_stubs/los_typedef.h"
#include "host_stubs/disk.h"
#include "host_stubs/vnode.h"
#include "host_stubs/fs/file.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

UINT8 *m_aucSysMem0 = (UINT8 *)0xDEADBEEFul;   /* sentinel; LOS_MemAlloc ignores. */

/* g_exfatVops / g_exfatFops live in fs/exfat/exfat_ops.c (production). The
 * cmocka harness does NOT compile exfat_ops.c (it would drag in Reclaim/Write
 * which depend on super.c/write.c — outside read-path scope). Lookup only
 * uses the *address* of these tables (vp->vop / vp->fop), so an empty stub
 * is sufficient. */
struct VnodeOps             g_exfatVops;
struct file_operations_vfs  g_exfatFops;

static uint8_t *g_image = NULL;
static size_t   g_image_len = 0;
static unsigned long g_read_count = 0;
static unsigned long g_write_count = 0;
static unsigned long g_read_fail_at = 0;
static unsigned long g_write_fail_at = 0;

void mock_disk_load(const void *buf, size_t len)
{
    if (g_image != NULL) {
        free(g_image);
    }
    g_image = (uint8_t *)malloc(len);
    if (g_image == NULL) {
        fprintf(stderr, "mock_disk_load: out of memory (%zu bytes)\n", len);
        abort();
    }
    memcpy(g_image, buf, len);
    g_image_len = len;
}

void mock_disk_unload(void)
{
    if (g_image != NULL) {
        free(g_image);
        g_image = NULL;
    }
    g_image_len = 0;
}

void mock_disk_set_read_fail_at(unsigned long n)  { g_read_fail_at  = n; }
void mock_disk_set_write_fail_at(unsigned long n) { g_write_fail_at = n; }

unsigned long mock_disk_read_count(void)  { return g_read_count;  }
unsigned long mock_disk_write_count(void) { return g_write_count; }

void mock_disk_reset_counters(void)
{
    g_read_count = 0;
    g_write_count = 0;
    g_read_fail_at = 0;
    g_write_fail_at = 0;
}

INT32 los_part_read(INT32 pt, VOID *buf, UINT64 sector, UINT32 count, BOOL useRead)
{
    (void)pt; (void)useRead;
    g_read_count++;
    if (g_read_fail_at != 0 && g_read_count == g_read_fail_at) {
        return -1;
    }
    if (g_image == NULL) {
        return -1;
    }
    UINT64 byte_off = sector * MOCK_PART_BLOCKSIZE;
    UINT64 byte_len = (UINT64)count * MOCK_PART_BLOCKSIZE;
    if (byte_off + byte_len > g_image_len) {
        return -1;
    }
    memcpy(buf, g_image + byte_off, byte_len);
    return 0;
}

INT32 los_part_write(INT32 pt, const VOID *buf, UINT64 sector, UINT32 count)
{
    (void)pt;
    g_write_count++;
    if (g_write_fail_at != 0 && g_write_count == g_write_fail_at) {
        return -1;
    }
    if (g_image == NULL) {
        return -1;
    }
    UINT64 byte_off = sector * MOCK_PART_BLOCKSIZE;
    UINT64 byte_len = (UINT64)count * MOCK_PART_BLOCKSIZE;
    if (byte_off + byte_len > g_image_len) {
        return -1;
    }
    /* Modifies in-RAM snapshot only; never writes back to disk file. */
    memcpy(g_image + byte_off, buf, byte_len);
    return 0;
}
