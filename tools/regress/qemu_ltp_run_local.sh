#!/bin/bash
# qemu_ltp_run_local.sh — local-docker LTP runner (no SSH/sudo/parted)
# Mirrors tools/regress/qemu_ltp_run.sh contract: idempotent image (smallmmc_exfat.img),
# 4 partitions (vfat/vfat/vfat/exfat), exfataddr bootargs, RC=0 PASS / RC=N FAIL parse.
#
# Default payload = smoke pack (`out/ltp_smoke.tar.gz` + `run_exfat.sh`), but callers may
# override QEMU_LTP_TGZ / QEMU_LTP_RUN_SCRIPT / QEMU_LTP_PACK_SENTINEL to run a different pack
# (for example fsfull: `out/ltp_fsfull.tar.gz` + `run_fsfull.sh`).
#
# Usage: bash tools/regress/qemu_ltp_run_local.sh [TIMEOUT_S]
set -u
TIMEOUT_S="${1:-30}"
if [ "$TIMEOUT_S" -gt 120 ]; then
    echo "[ltp-run] clamp TIMEOUT_S from $TIMEOUT_S to 120"
    TIMEOUT_S=120
fi
POLL_S=10
STEP_TIMEOUT_S=30
CHAR_DELAY_S=0.02
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
SRC=$REPO/out/arm_virt/qemu_small_system_demo
FLASH_BASE=${QEMU_LTP_FLASH_BASE:-$REPO/out/smallmmc_exfat.img}
FLASH_WORK=${QEMU_LTP_FLASH_WORK:-$REPO/out/smallmmc_exfat.work.img}
FLASH=$FLASH_BASE
BIOS=$SRC/OHOS_Image.bin
LTP_TGZ=${QEMU_LTP_TGZ:-$REPO/out/ltp_smoke.tar.gz}
PACK_DIR_GUEST=${QEMU_LTP_PACK_DIR:-/storage/ltp_pack}
RUN_SCRIPT=${QEMU_LTP_RUN_SCRIPT:-run_exfat.sh}
PACK_SENTINEL_REL=${QEMU_LTP_PACK_SENTINEL:-bin/open01}
LOG=${QEMU_LTP_LOG:-$REPO/out/qemu_ltp_log.txt}
SERIAL=${QEMU_LTP_SERIAL_LOG:-$REPO/out/qemu_ltp_serial.log}
WORK=$SRC/obj/kernel/liteos_a/make_out/_mmcwork_exfat
ROOTSRC=$SRC/rootfs
P2_OFFSET_BYTES=$((30 * 1024 * 1024))
mkdir -p "$WORK"

[ -f "$BIOS" ] || { echo "[ltp-run] missing $BIOS"; exit 2; }
[ -f "$LTP_TGZ" ] || { echo "[ltp-run] missing $LTP_TGZ"; exit 2; }
# ROOTSRC only required when reseed will run (it sources /etc/* into exfat p4).
# When QEMU_LTP_RESEED=0 and a baseline exfat image already exists, skip the check.

# Probe signatures like the official script.
need_p4=${QEMU_LTP_RESEED:-1}
if [ -f "$FLASH" ]; then
    p2_sig=$(dd if="$FLASH" bs=1 skip=31457283 count=8 2>/dev/null | cat)
    p4_sig=$(dd if="$FLASH" bs=1 skip=104857603 count=8 2>/dev/null | cat)
    echo "[ltp-run] p2='$p2_sig' p4='$p4_sig'"
    case "$p4_sig" in "EXFAT   ") need_p4=0 ;; esac
    need_p1_p2=0
    case "$p2_sig" in mkfs.fat|MSDOS5.0|"FAT32   ") : ;; *) need_p1_p2=1 ;; esac
else
    need_p1_p2=1
fi

if [ "${QEMU_LTP_RESEED:-0}" = 1 ] || [ "$need_p1_p2" = 1 ] || [ "$need_p4" = 1 ]; then
    [ -d "$ROOTSRC" ] || { echo "[ltp-run] reseed requires $ROOTSRC"; exit 2; }
    echo "[ltp-run] Rebuilding image with sfdisk+mtools+FUSE..."
    rm -rf "$WORK"
    mkdir -p "$WORK"
    rm -f "$FLASH"
    dd if=/dev/zero of="$FLASH" bs=1M count=150 status=none
    sfdisk "$FLASH" >/dev/null <<SFDISK
label: dos
start=20480,  size=40960,  type=0c
start=61440,  size=102400, type=0c
start=163840, size=40960,  type=0c
start=204800,              type=07
SFDISK
    mkfs.vfat -C "$WORK/p1.img" $((20 * 1024)) >/dev/null
    mkfs.vfat -C "$WORK/p2.img" $((50 * 1024)) >/dev/null
    mkfs.vfat -C "$WORK/p3.img" $((20 * 1024)) >/dev/null
    truncate -s 50M "$WORK/p4.img"
    mkfs.exfat -n EXFATSMOKE "$WORK/p4.img" >/dev/null
    (
        cd "$ROOTSRC" || exit 1
        for f in *; do
            MTOOLS_SKIP_CHECK=1 mcopy -s -i "$WORK/p1.img" "$f" ::
        done
    )
    # ltp_pack -> p2. Guest-side layout remains /storage/ltp_pack regardless of payload size.
    EXTRACT=$WORK/_ltp_extract
    rm -rf "$EXTRACT"
    mkdir -p "$EXTRACT"
    tar -xzf "$LTP_TGZ" -C "$EXTRACT"
    MTOOLS_SKIP_CHECK=1 mcopy -s -i "$WORK/p2.img" "$EXTRACT/ltp_pack" ::
    # /etc -> p4 (exfat via FUSE), per QEMU smoke contract.
    LOOP=$(losetup --find --show "$WORK/p4.img")
    mkdir -p "$WORK/mnt"
    mount.exfat-fuse "$LOOP" "$WORK/mnt"
    mkdir -p "$WORK/mnt/etc"
    cp -a "$ROOTSRC/etc/." "$WORK/mnt/etc/"
    [ -f "$WORK/mnt/etc/passwd" ] || cat >"$WORK/mnt/etc/passwd" <<PWEOF
root:x:0:0:root:/:/bin/sh
nobody:x:65534:65534:nobody:/:/sbin/nologin
PWEOF
    [ -f "$WORK/mnt/etc/group" ] || cat >"$WORK/mnt/etc/group" <<GREOF
root:x:0:
nobody:x:65534:
GREOF
    sync
    fusermount3 -u "$WORK/mnt" || fusermount -u "$WORK/mnt"
    losetup -d "$LOOP"
    # Splice partitions into the flat mmc image.
    dd if="$WORK/p1.img" of="$FLASH" bs=1M seek=10 conv=notrunc status=none
    dd if="$WORK/p2.img" of="$FLASH" bs=1M seek=30 conv=notrunc status=none
    dd if="$WORK/p3.img" of="$FLASH" bs=1M seek=80 conv=notrunc status=none
    dd if="$WORK/p4.img" of="$FLASH" bs=1M seek=100 conv=notrunc status=none
fi

# bootargs
printf 'bootargs=root=emmc fstype=vfat rootaddr=10M rootsize=20M useraddr=30M usersize=50M exfataddr=100M\0' > "$WORK/bootargs"
dd if="$WORK/bootargs" of="$FLASH" conv=notrunc seek=524288 oflag=seek_bytes status=none
cp "$FLASH_BASE" "$FLASH_WORK"
echo "[ltp-run] flash work copy: $FLASH_BASE -> $FLASH_WORK"
FLASH=$FLASH_WORK

ensure_guest_pack() {
    local p2_img="$FLASH@@$P2_OFFSET_BYTES"
    local extract_dir="$WORK/_ltp_pack_refresh"
    local have_bin_tree=0

    if MTOOLS_SKIP_CHECK=1 mdir -i "$p2_img" "::/ltp_pack/$RUN_SCRIPT" >/dev/null 2>&1 && \
       MTOOLS_SKIP_CHECK=1 mdir -i "$p2_img" "::/ltp_pack/$PACK_SENTINEL_REL" >/dev/null 2>&1; then
        echo "[ltp-run] guest pack already satisfies $RUN_SCRIPT + $PACK_SENTINEL_REL"
        return 0
    fi

    if MTOOLS_SKIP_CHECK=1 mdir -i "$p2_img" "::/ltp_pack/bin" >/dev/null 2>&1; then
        have_bin_tree=1
    fi

    echo "[ltp-run] refreshing guest pack from $LTP_TGZ -> $PACK_DIR_GUEST"
    rm -rf "$extract_dir"
    mkdir -p "$extract_dir"
    tar -xzf "$LTP_TGZ" -C "$extract_dir"
    [ -d "$extract_dir/ltp_pack" ] || { echo "[ltp-run] missing ltp_pack/ in $LTP_TGZ"; exit 2; }

    MTOOLS_SKIP_CHECK=1 mmd -i "$p2_img" ::/ltp_pack 2>/dev/null || true
    if [ "$have_bin_tree" -eq 1 ]; then
        local entry
        for entry in run_fsfull.sh manifest.json cases.txt dirs.txt skipped_cases.txt; do
            if [ -f "$extract_dir/ltp_pack/$entry" ]; then
                MTOOLS_SKIP_CHECK=1 mcopy -o -i "$p2_img" "$extract_dir/ltp_pack/$entry" ::/ltp_pack
            fi
        done
        return 0
    fi

    MTOOLS_SKIP_CHECK=1 mcopy -s -o -i "$p2_img" "$extract_dir/ltp_pack/"* ::/ltp_pack
}

ensure_guest_pack

echo "[ltp-run] payload tgz:  $LTP_TGZ"
echo "[ltp-run] guest pack:   $PACK_DIR_GUEST"
echo "[ltp-run] run script:   $RUN_SCRIPT"
echo "[ltp-run] sentinel:     $PACK_SENTINEL_REL"
echo "[ltp-run] log:          $LOG"
echo "[ltp-run] serial:       $SERIAL"

cleanup_qemu() {
    exec 9>&- 2>/dev/null || true
    kill -9 "$QPID" 2>/dev/null || true
    wait "$QPID" 2>/dev/null || true
}

report_new_syscalls() {
    local new_sc diff_sc
    new_sc=$(grep -oE 'Unsupported (API|syscall ID:) [^ ]+' "$SERIAL" 2>/dev/null | sort -u || true)
    if [ "$new_sc" != "$SEEN_SYSCALL" ]; then
        diff_sc=$(comm -23 <(printf '%s\n' "$new_sc") <(printf '%s\n' "$SEEN_SYSCALL") 2>/dev/null || true)
        [ -n "$diff_sc" ] && echo "[ltp-run] WARN: $diff_sc"
        SEEN_SYSCALL="$new_sc"
    fi
}

wait_for_pattern() {
    local pattern="$1"
    local label="$2"
    local timeout_s="$3"
    local elapsed=0
    local last_size=-1
    local stuck=0
    local cur_size last

    while [ "$elapsed" -lt "$timeout_s" ]; do
        sleep "$POLL_S"
        elapsed=$((elapsed + POLL_S))
        cur_size=$(wc -c < "$SERIAL" 2>/dev/null || echo 0)
        if [ "$cur_size" != "$last_size" ]; then
            last_size="$cur_size"
            stuck=0
        else
            stuck=$((stuck + POLL_S))
        fi

        if tr -d '\r' < "$SERIAL" 2>/dev/null | grep -qE 'panic|Unhandled (prefetch|data) abort|Kernel BUG|Oops'; then
            echo "[ltp-run] PANIC while waiting for ${label} at ${elapsed}s — tail:"
            tail -20 "$SERIAL"
            return 2
        fi

        report_new_syscalls

        if tr -d '\r' < "$SERIAL" 2>/dev/null | grep -qE "$pattern"; then
            echo "[ltp-run] ${label} at ${elapsed}s"
            return 0
        fi

        if ! kill -0 "$QPID" 2>/dev/null; then
            echo "[ltp-run] qemu pid $QPID died while waiting for ${label} at ${elapsed}s — tail:"
            tail -20 "$SERIAL"
            return 2
        fi

        last=$(tail -1 "$SERIAL" 2>/dev/null | tr -d '\r' | head -c 120)
        echo "[ltp-run] waiting ${label}: t=${elapsed}s size=${cur_size} stuck=${stuck}s last='${last}'"

        if [ "$stuck" -ge "$STEP_TIMEOUT_S" ]; then
            echo "[ltp-run] STUCK ${STEP_TIMEOUT_S}s without serial output while waiting for ${label} — aborting"
            tail -30 "$SERIAL"
            return 2
        fi
    done

    echo "[ltp-run] ERROR: timeout waiting for ${label} after ${timeout_s}s — tail:"
    tail -30 "$SERIAL"
    return 1
}

wait_for_rc_marker() {
    local marker="$1"
    local label="$2"
    local timeout_s="$3"
    local line rc

    wait_for_pattern "^${marker}:[0-9]+$" "$label" "$timeout_s" || return $?
    line=$(tr -d '\r' < "$SERIAL" | grep -E "^${marker}:[0-9]+$" | tail -1)
    rc=${line##*:}
    echo "[ltp-run] ${label} rc=${rc}"
    [ "$rc" = "0" ] && return 0

    echo "[ltp-run] ERROR: ${label} failed"
    tail -30 "$SERIAL"
    return 1
}

send_slow_line() {
    local text="$1"
    local i
    for ((i = 0; i < ${#text}; i++)); do
        printf '%s' "${text:i:1}" >&9
        sleep "$CHAR_DELAY_S"
    done
    printf '\n' >&9
}

send_cmd_with_marker() {
    local cmd="$1"
    local marker="$2"
    send_slow_line "$cmd"
    sleep "$CHAR_DELAY_S"
    send_slow_line "echo ${marker}:\$?"
}

# Launch QEMU via FIFO (mirrors official tools/regress/qemu_ltp_run.sh).
> "$SERIAL"
FIFO=$WORK/qemu_in.fifo
rm -f "$FIFO"
mkfifo "$FIFO"
(
    exec 3<>"$FIFO"
    exec qemu-system-arm \
        -M virt,gic-version=2,secure=on -cpu cortex-a7 -smp cpus=1 -m 1G \
        -bios "$BIOS" -global virtio-mmio.force-legacy=false \
        -nographic \
        -drive if=none,file="$FLASH_WORK",format=raw,id=mmc \
        -device virtio-blk-device,drive=mmc \
        -device virtio-rng-device \
        < "$FIFO" > "$SERIAL" 2>&1
) &
QPID=$!
SEEN_SYSCALL=""
echo "[ltp-run] qemu pid=$QPID"

# Wait for OHOS:/$ prompt (max 30s).
i=0
while [ $i -lt "$STEP_TIMEOUT_S" ]; do
    grep -q 'OHOS:/' "$SERIAL" 2>/dev/null && break
    sleep 1
    i=$((i + 1))
done
if ! grep -q 'OHOS:/' "$SERIAL" 2>/dev/null; then
    echo "[ltp-run] ERROR: no shell prompt within ${STEP_TIMEOUT_S}s"
    cleanup_qemu
    exit 2
fi
echo "[ltp-run] shell prompt at ${i}s"

# Persistent writer FD: avoids one-shot FIFO close semantics and pinpoints stalled guest step.
exec 9>"$FIFO"

send_cmd_with_marker 'mkdir -p /mnt/exfat' '__STEP_MKDIR_DONE__'
wait_for_rc_marker '__STEP_MKDIR_DONE__' 'mkdir /mnt/exfat' "$STEP_TIMEOUT_S" || {
    cleanup_qemu
    exit 2
}

send_cmd_with_marker 'mount -t exfat /dev/mmcblk0p3 /mnt/exfat' '__STEP_MOUNT_DONE__'
wait_for_rc_marker '__STEP_MOUNT_DONE__' 'mount exfat' "$STEP_TIMEOUT_S" || {
    cleanup_qemu
    exit 2
}

send_cmd_with_marker "cd ${PACK_DIR_GUEST}" '__STEP_CD_DONE__'
wait_for_rc_marker '__STEP_CD_DONE__' "cd ${PACK_DIR_GUEST}" "$STEP_TIMEOUT_S" || {
    cleanup_qemu
    exit 2
}

send_slow_line "mksh ${RUN_SCRIPT}"
sleep "$CHAR_DELAY_S"
send_slow_line 'echo __STEP_RUN_DONE__:$?'
wait_for_pattern '^TEST_DONE_MARKER$' "${RUN_SCRIPT} completion" "$TIMEOUT_S" || {
    cleanup_qemu
    exit 2
}
wait_for_rc_marker '__STEP_RUN_DONE__' "mksh ${RUN_SCRIPT} return" "$STEP_TIMEOUT_S" || {
    cleanup_qemu
    exit 2
}

send_cmd_with_marker 'umount /mnt/exfat' '__STEP_UMOUNT_DONE__'
wait_for_rc_marker '__STEP_UMOUNT_DONE__' 'umount /mnt/exfat' "$STEP_TIMEOUT_S" || {
    cleanup_qemu
    exit 2
}

cleanup_qemu
cp -f "$SERIAL" "$LOG"
PASS=$(grep -cE '^RC=0 \(' "$LOG" || true)
FAIL=$(grep -cE '^RC=[1-9][0-9]* \(' "$LOG" || true)
echo "[ltp-run] ============================================"
echo "[ltp-run] RESULTS: PASS=$PASS  FAIL=$FAIL  qemu_rc=0"
echo "[ltp-run] ============================================"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
