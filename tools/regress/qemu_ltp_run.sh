#!/bin/sh
# qemu_ltp_run.sh — LTP exFAT smoke regression runner for LiteOS-A / QEMU arm_virt
#
# Usage:
#   SRC=<OH_OUT_DIR> FLASH=<img> TIMEOUT_S=<n> ./qemu_ltp_run.sh [SRC [FLASH [TIMEOUT_S]]]
#
# Defaults:
#   SRC     = /mnt/work/openharmony/out/arm_virt/qemu_small_system_demo
#   FLASH   = $SRC/../smallmmc.img
#   TIMEOUT_S = 600
#
# What this script does (all work runs via SSH on 192.168.1.15):
#   1. Build/rebuild smallmmc.img 4-partition layout if needed (idempotent).
#   2. Populate p1 (rootfs), p2 (userfs + ltp_smoke), p4 (exfat + /etc) idempotently.
#   3. Write NUL-terminated bootargs at offset 512K.
#   4. Launch QEMU -nographic with FIFO-driven stdin; tee serial to log.
#   5. Wait for shell prompt, feed mount + LTP commands, collect TEST_DONE_MARKER.
#   6. Parse RC= lines, print PASS/FAIL summary, write log to $SRC/../qemu_ltp_log.txt.
#   7. Exit 0=all pass, 1=some fail, 2=panic/hang.
#
# Constraints honoured:
#   - bootargs NUL-terminated via perl -e 'print chr(0)'
#   - QEMU uses -nographic (not -display none) — required for QEMU 11.0+
#   - qemu_run.sh is never called (it wipes p4)
#   - No trap deletes the image (reusable across runs)
#   - Unsupported syscall IDs are logged to stdout for user action, not auto-patched
#   - sudo uses SUDO_ASKPASS=/tmp/askpass.sh sudo -A
#
# Exit codes: 0=all PASS, 1=test FAIL(s), 2=panic/hang/timeout

set -u

# ---------------------------------------------------------------------------
# Parameters — resolve all defaults before SSH so remote always gets 4 concrete args
# ---------------------------------------------------------------------------
SRC="${SRC:-${1:-/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo}}"
_FLASH_DEFAULT="$(dirname "$SRC")/smallmmc.img"
FLASH="${FLASH:-${2:-$_FLASH_DEFAULT}}"
TIMEOUT_S="${TIMEOUT_S:-${3:-600}}"

REMOTE=192.168.1.15
LTP_SMOKE_TGZ=/mnt/work/test/ltp_smoke.tar.gz

echo "[ltp-run] SRC=$SRC"
echo "[ltp-run] FLASH=$FLASH"
echo "[ltp-run] TIMEOUT_S=$TIMEOUT_S"

# ---------------------------------------------------------------------------
# Remote execution block (everything below runs on the remote host)
# ---------------------------------------------------------------------------
ssh "$REMOTE" /bin/sh -s "$SRC" "$FLASH" "$TIMEOUT_S" "$LTP_SMOKE_TGZ" << 'SSH_EOF'
# positional args assigned before set -u to avoid unbound errors
SRC="$1"
FLASH="$2"
TIMEOUT_S="$3"
LTP_SMOKE_TGZ="$4"
set -u

BIOS="$SRC/OHOS_Image.bin"
LOG="$SRC/../qemu_ltp_log.txt"
SERIAL_LOG=/tmp/qemu_ltp_serial.log
FIFO=/tmp/qemu_ltp_in
# Reset dedup state from any previous run
rm -f /tmp/ltp_syscall_warn_seen /tmp/ltp_syscall_warn_new /tmp/ltp_syscall_warn_diff
SUDO="sudo -A"
export SUDO_ASKPASS=/tmp/askpass.sh

echo "[ltp-run] FLASH=$FLASH"
echo "[ltp-run] LOG=$LOG"

# ---------------------------------------------------------------------------
# Helper: free all loop devices we used (best-effort, ignore errors)
# ---------------------------------------------------------------------------
free_loops() {
    for n in 590 591 592 593 594; do
        $SUDO losetup -d /dev/loop$n 2>/dev/null || true
    done
}

# ---------------------------------------------------------------------------
# Step 1: detect whether image needs rebuild by reading magic bytes directly
#
# Partition byte offsets in the flat image:
#   p2 starts at 30 MiB = 31457280; FAT32 OEM name at bytes +3..+10 = "MSDOS5.0" or "mkfs.fat"
#   p4 starts at 100 MiB = 104857600; exFAT signature at bytes +3..+10 = "EXFAT   "
#
# od reads raw bytes: skip=$offset count=8 chars -> compare string
# ---------------------------------------------------------------------------
need_mkfs_p1=1
need_mkfs_p2=1
need_mkfs_p4=1
new_image=0

if [ -f "$FLASH" ]; then
    echo "[ltp-run] Existing image found, probing partition magic bytes..."

    # p2: FAT32 OEM name at offset 30MiB+3 = 31457283, length 8
    p2_sig=$(dd if="$FLASH" bs=1 skip=31457283 count=8 2>/dev/null | cat)
    # p4: exFAT signature at offset 100MiB+3 = 104857603, length 8
    p4_sig=$(dd if="$FLASH" bs=1 skip=104857603 count=8 2>/dev/null | cat)

    echo "[ltp-run] p2 sig: '$p2_sig'"
    echo "[ltp-run] p4 sig: '$p4_sig'"

    # FAT32: OEM name is "mkfs.fat" or "MSDOS5.0" or similar non-null
    case "$p2_sig" in
        mkfs.fat|MSDOS5.0|"FAT32   ") need_mkfs_p1=0; need_mkfs_p2=0 ;;
    esac

    # exFAT: signature is exactly "EXFAT   " (with 3 trailing spaces)
    case "$p4_sig" in
        "EXFAT   ") need_mkfs_p4=0 ;;
    esac
else
    new_image=1
fi

echo "[ltp-run] need_mkfs_p1=$need_mkfs_p1 need_mkfs_p2=$need_mkfs_p2 need_mkfs_p4=$need_mkfs_p4"

# ---------------------------------------------------------------------------
# Step 2: build or rebuild image partitions as needed
# ---------------------------------------------------------------------------
if [ "$new_image" -eq 1 ] || [ "$need_mkfs_p2" -eq 1 ] || [ "$need_mkfs_p4" -eq 1 ]; then
    echo "[ltp-run] Building/rebuilding smallmmc.img partitions..."
    free_loops

    if [ "$new_image" -eq 1 ]; then
        dd if=/dev/zero of="$FLASH" bs=1M count=150 2>/dev/null
        $SUDO losetup /dev/loop590 "$FLASH"
        $SUDO parted -s /dev/loop590 -- mklabel msdos \
            mkpart primary fat32 10MiB  30MiB  \
            mkpart primary fat32 30MiB  80MiB  \
            mkpart primary fat32 80MiB  100MiB \
            mkpart primary       100MiB -1s
        need_mkfs_p1=1; need_mkfs_p2=1; need_mkfs_p4=1
    else
        $SUDO losetup /dev/loop590 "$FLASH"
    fi

    $SUDO losetup -o $((10*1024*1024))  /dev/loop591 /dev/loop590
    $SUDO losetup -o $((30*1024*1024))  /dev/loop592 /dev/loop590
    $SUDO losetup -o $((80*1024*1024))  /dev/loop593 /dev/loop590
    $SUDO losetup -o $((100*1024*1024)) /dev/loop594 /dev/loop590

    if [ "$need_mkfs_p1" -eq 1 ]; then
        echo "[ltp-run] Formatting p1 (vfat)..."
        $SUDO mkfs.vfat -F 32 /dev/loop591
    fi
    if [ "$need_mkfs_p2" -eq 1 ]; then
        echo "[ltp-run] Formatting p2 (vfat)..."
        $SUDO mkfs.vfat -F 32 /dev/loop592
        echo "[ltp-run] Formatting p3 (vfat)..."
        $SUDO mkfs.vfat -F 32 /dev/loop593
    fi
    if [ "$need_mkfs_p4" -eq 1 ]; then
        echo "[ltp-run] Formatting p4 (exfat)..."
        $SUDO mkfs.exfat -n EXFATSMOKE /dev/loop594
    fi

    if [ "$need_mkfs_p1" -eq 1 ]; then
        echo "[ltp-run] Populating p1 (rootfs)..."
        $SUDO mkdir -p /tmp/p1_mnt
        $SUDO mount /dev/loop591 /tmp/p1_mnt
        $SUDO cp -rf "$SRC/rootfs/"* /tmp/p1_mnt/ 2>/dev/null || true
        sync
        $SUDO umount /tmp/p1_mnt
    fi

    if [ "$need_mkfs_p2" -eq 1 ]; then
        echo "[ltp-run] Populating p2 (userfs)..."
        $SUDO mkdir -p /tmp/p2_mnt
        $SUDO mount /dev/loop592 /tmp/p2_mnt
        $SUDO cp -rf "$SRC/userfs/"* /tmp/p2_mnt/ 2>/dev/null || true
        sync
        $SUDO umount /tmp/p2_mnt
    fi

    free_loops
fi

# ---------------------------------------------------------------------------
# Step 3: idempotently deploy ltp_smoke to p2 and /etc to p4
# ---------------------------------------------------------------------------
$SUDO losetup /dev/loop590 "$FLASH"
$SUDO losetup -o $((30*1024*1024))  /dev/loop592 /dev/loop590
$SUDO losetup -o $((100*1024*1024)) /dev/loop594 /dev/loop590

$SUDO mkdir -p /tmp/p2_mnt /tmp/p4_mnt

$SUDO mount /dev/loop592 /tmp/p2_mnt

if [ ! -x /tmp/p2_mnt/ltp_pack/bin/open01 ]; then
    echo "[ltp-run] Deploying ltp_smoke to p2..."
    $SUDO mkdir -p /tmp/p2_mnt/ltp_pack
    $SUDO tar -xzf "$LTP_SMOKE_TGZ" -C /tmp/p2_mnt/ltp_pack --strip-components=1 --no-same-owner 2>&1 | grep -v "Cannot change ownership" || true
    sync
    echo "[ltp-run] ltp_pack deployed: $(ls /tmp/p2_mnt/ltp_pack/bin/ 2>/dev/null | tr '\n' ' ')"
else
    echo "[ltp-run] ltp_pack already present on p2, skipping."
fi

$SUDO umount /tmp/p2_mnt

$SUDO mount -t exfat /dev/loop594 /tmp/p4_mnt

if [ ! -d /tmp/p4_mnt/etc ]; then
    echo "[ltp-run] Deploying /etc payload to p4..."
    $SUDO mkdir -p /tmp/p4_mnt/etc
    $SUDO cp /etc/hostname /tmp/p4_mnt/etc/ 2>/dev/null || true
    $SUDO cp /etc/hosts    /tmp/p4_mnt/etc/ 2>/dev/null || true
    $SUDO cp /etc/os-release /tmp/p4_mnt/etc/ 2>/dev/null || true
    sync
else
    echo "[ltp-run] /etc already present on p4, skipping."
fi

$SUDO umount /tmp/p4_mnt
free_loops

# ---------------------------------------------------------------------------
# Step 4: write NUL-terminated bootargs at offset 512K
# ---------------------------------------------------------------------------
echo "[ltp-run] Writing bootargs..."
BOOTARGS='bootargs=root=emmc fstype=vfat rootaddr=10M rootsize=20M useraddr=30M usersize=50M exfataddr=100M'
# perl chr(0) is the only reliable NUL in POSIX sh
printf '%s' "$BOOTARGS" > /tmp/ltp_bootargs
perl -e 'print chr(0)' >> /tmp/ltp_bootargs
$SUDO dd if=/tmp/ltp_bootargs of="$FLASH" conv=notrunc seek=524288 oflag=seek_bytes 2>/dev/null
echo "[ltp-run] Bootargs written ($(wc -c < /tmp/ltp_bootargs) bytes incl NUL)."

# ---------------------------------------------------------------------------
# Step 5: launch QEMU via FIFO
# ---------------------------------------------------------------------------
> "$SERIAL_LOG"
rm -f "$FIFO"
mkfifo "$FIFO"

echo "[ltp-run] Starting QEMU..."
# Kill any stale QEMU holding a write lock on the image before starting
$SUDO fuser -k "$FLASH" 2>/dev/null || true
sleep 1

QEMU_PID_FILE=/tmp/qemu_ltp_pid
rm -f "$QEMU_PID_FILE"
(
    exec 3<>"$FIFO"
    qemu-system-arm \
        -M virt,gic-version=2,secure=on \
        -cpu cortex-a7 \
        -smp cpus=1 \
        -m 1G \
        -bios "$BIOS" \
        -global virtio-mmio.force-legacy=false \
        -nographic \
        -drive if=none,file="$FLASH",format=raw,id=mmc \
        -device virtio-blk-device,drive=mmc \
        -device virtio-rng-device \
        < "$FIFO" > "$SERIAL_LOG" 2>&1
) &
QPID=$!
echo $QPID > "$QEMU_PID_FILE"
echo "[ltp-run] QEMU pid=$QPID"

# ---------------------------------------------------------------------------
# Step 6: wait for shell prompt (up to 60s)
# ---------------------------------------------------------------------------
echo "[ltp-run] Waiting for OHOS shell prompt..."
i=0
while [ $i -lt 60 ]; do
    grep -q 'OHOS:/' "$SERIAL_LOG" 2>/dev/null && break
    sleep 1
    i=$((i+1))
done

if ! grep -q 'OHOS:/' "$SERIAL_LOG" 2>/dev/null; then
    echo "[ltp-run] ERROR: Shell prompt not seen within 60s. Killing QEMU."
    kill -9 "$QPID" 2>/dev/null || true; $SUDO fuser -k "$FLASH" 2>/dev/null || true
    cp "$SERIAL_LOG" "$LOG"
    exit 2
fi
echo "[ltp-run] Shell prompt detected at ${i}s."

# ---------------------------------------------------------------------------
# Step 7: feed command sequence into QEMU stdin
# ---------------------------------------------------------------------------
{
    printf 'mkdir -p /mnt/exfat\n';                               sleep 1
    printf 'mount -t exfat /dev/mmcblk0p3 /mnt/exfat\n';         sleep 3
    printf 'echo MOUNT_RC=$?\n';                                  sleep 0.5
    printf 'mkdir -p /storage/ltp_pack && cd /storage/ltp_pack\n'; sleep 1
    printf 'mksh run_exfat.sh\n';                                 sleep 90
    printf 'echo TEST_DONE_MARKER\n';                             sleep 1
    printf 'umount /mnt/exfat\n';                                 sleep 2
    printf 'echo UMOUNT_RC=$?\n'
} > "$FIFO" &
FEED_PID=$!

# ---------------------------------------------------------------------------
# Step 8: monitor log for panic / completion / timeout
# ---------------------------------------------------------------------------
echo "[ltp-run] Monitoring serial log (timeout=${TIMEOUT_S}s)..."
elapsed=0
final_rc=0

while [ $elapsed -lt "$TIMEOUT_S" ]; do
    sleep 1
    elapsed=$((elapsed+1))

    if grep -qE 'panic|Oops|Kernel BUG|fatal error|Unhandled (prefetch|data) abort' "$SERIAL_LOG" 2>/dev/null; then
        echo "[ltp-run] KERNEL PANIC detected at ${elapsed}s — aborting."
        kill -9 "$QPID" 2>/dev/null || true; $SUDO fuser -k "$FLASH" 2>/dev/null || true
        kill "$FEED_PID" 2>/dev/null || true
        cp "$SERIAL_LOG" "$LOG"
        exit 2
    fi

    if grep -q 'Unsupported syscall ID:' "$SERIAL_LOG" 2>/dev/null; then
        # Report each unique syscall ID once per run (deduplicated via temp file)
        grep -o 'Unsupported syscall ID: [0-9]*' "$SERIAL_LOG" | sort -u > /tmp/ltp_syscall_warn_new
        if [ -f /tmp/ltp_syscall_warn_seen ]; then
            comm -23 /tmp/ltp_syscall_warn_new /tmp/ltp_syscall_warn_seen > /tmp/ltp_syscall_warn_diff
        else
            cp /tmp/ltp_syscall_warn_new /tmp/ltp_syscall_warn_diff
        fi
        while IFS= read -r sc; do
            echo "[ltp-run] WARNING: $sc  <-- add stub to syscall/syscall_lookup.h"
        done < /tmp/ltp_syscall_warn_diff
        cp /tmp/ltp_syscall_warn_new /tmp/ltp_syscall_warn_seen
    fi

    if grep -q 'TEST_DONE_MARKER' "$SERIAL_LOG" 2>/dev/null; then
        echo "[ltp-run] TEST_DONE_MARKER seen at ${elapsed}s."
        break
    fi
done

kill "$FEED_PID" 2>/dev/null || true

if [ $elapsed -ge "$TIMEOUT_S" ] && ! grep -q 'TEST_DONE_MARKER' "$SERIAL_LOG" 2>/dev/null; then
    echo "[ltp-run] ERROR: Test hung — no TEST_DONE_MARKER after ${TIMEOUT_S}s."
    kill -9 "$QPID" 2>/dev/null || true; $SUDO fuser -k "$FLASH" 2>/dev/null || true
    cp "$SERIAL_LOG" "$LOG"
    exit 2
fi

# ---------------------------------------------------------------------------
# Step 9: parse results
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

while IFS= read -r line; do
    case "$line" in
        RC=0\ PASS*) PASS=$((PASS+1)) ;;
        RC=*\ FAIL*) FAIL=$((FAIL+1)) ;;
        # LTP native output: "PASS" / "FAIL" lines
        *'  PASS'*)  PASS=$((PASS+1)) ;;
        *'  FAIL'*)  FAIL=$((FAIL+1)) ;;
    esac
done < "$SERIAL_LOG"

echo "[ltp-run] ============================================"
echo "[ltp-run] RESULTS: PASS=$PASS  FAIL=$FAIL"
echo "[ltp-run] ============================================"

if grep -q 'Unsupported syscall ID:' "$SERIAL_LOG" 2>/dev/null; then
    echo "[ltp-run] Unsupported syscall IDs found — see warnings above. Add stubs manually."
fi

# ---------------------------------------------------------------------------
# Step 10: copy log, kill QEMU, exit
# ---------------------------------------------------------------------------
cp "$SERIAL_LOG" "$LOG"
echo "[ltp-run] Full serial log saved to $LOG"

# Kill QEMU: try by PID first, then fuser on image as safety net
kill -9 "$QPID" 2>/dev/null || true
$SUDO fuser -k "$FLASH" 2>/dev/null || true
# Wait up to 3s for the file lock to release
_w=0
while [ $_w -lt 3 ] && lsof "$FLASH" 2>/dev/null | grep -q qemu; do
    sleep 1; _w=$((_w+1))
done

if [ $FAIL -gt 0 ]; then
    exit 1
fi
exit 0
SSH_EOF

REMOTE_RC=$?
echo "[ltp-run] Remote exited with rc=$REMOTE_RC"
exit $REMOTE_RC
