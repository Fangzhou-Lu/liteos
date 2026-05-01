/*
 * mock_disk — in-memory exFAT image backing for cmocka tests.
 *
 * Semantics (per user spec):
 *   - mock_disk_load(buf, len) snapshots an image into RAM ONCE.
 *   - mock_part_read returns sectors from the in-memory snapshot — read-only,
 *     idempotent across tests using the same loaded image.
 *   - mock_part_write modifies the in-memory snapshot but NEVER writes back to
 *     the original file → repeatable image use across the whole run.
 *   - Optional: mock_disk_set_io_failure_after(N) makes the (N+1)-th read fail,
 *     for negative-path tests.
 */
#ifndef MOCK_DISK_H
#define MOCK_DISK_H

#include <stddef.h>
#include <stdint.h>

#define MOCK_PART_BLOCKSIZE 512u

/* Load a snapshot. Caller owns `buf` lifetime; mock_disk copies it. */
void mock_disk_load(const void *buf, size_t len);

/* Forget the current snapshot. Idempotent. */
void mock_disk_unload(void);

/* Inject a read failure on the N-th subsequent read (1-indexed).
 * 0 disables. Used to exercise -EIO paths. */
void mock_disk_set_read_fail_at(unsigned long n);

/* Inject a write failure analogously. */
void mock_disk_set_write_fail_at(unsigned long n);

/* Counters for assertions. */
unsigned long mock_disk_read_count(void);
unsigned long mock_disk_write_count(void);

/* Reset counters AND clear injected failures (does NOT unload the image). */
void mock_disk_reset_counters(void);

#endif
