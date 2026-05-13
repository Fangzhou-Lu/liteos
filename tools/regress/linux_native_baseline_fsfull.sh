#!/bin/bash
# Linux-native exFAT LTP baseline — full FS-syscall subset (~60 subdirs / ~358 cases).
# Boots qemu-system-aarch64 with Linux kernel + initramfs (busybox + glibc + all LTP
# fs-related test binaries), mounts exFAT image at /mnt/exfat, runs each binary with
# a per-test timeout, captures RC=N (name) lines. Result JSON: linux_native_ltp_fsfull.json.
#
# Run inside oh-dev container (paths assume /work/oh-mini-ltp/ltp source + built bins).
set -euo pipefail

REPO_ROOT=/home/openharmony
KERNEL_IMAGE=${KERNEL_IMAGE:-/work/build-arm64/arch/arm64/boot/Image}
LTP_SRC=${LTP_SRC:-/work/oh-mini-ltp/ltp}
SYSCALL_DIR="$LTP_SRC/testcases/kernel/syscalls"
WORK=/tmp/linux-baseline-fsfull
ROOTFS="$WORK/rootfs"
INITRAMFS="$WORK/initramfs.cpio"
EXFAT_IMG="$WORK/exfat.img"
SERIAL_LOG="$WORK/serial.log"
RESULT_JSON="$REPO_ROOT/out/linux_native_ltp_fsfull.json"
EXFAT_IMG_MIB=${EXFAT_IMG_MIB:-128}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-7200}
PER_TEST_TIMEOUT=${PER_TEST_TIMEOUT:-5}

# Curated fs-syscall subset (~60 subdirs).
FS_DIRS="access chdir chmod chown close creat dup dup2 dup3 faccessat faccessat2 \
fchdir fchmod fchmodat fchown fchownat fcntl fdatasync fstat fstatat fstatfs fsync \
ftruncate getcwd getdents link linkat llseek lseek lstat mkdir mkdirat mmap msync \
munmap open openat preadv2 pwritev2 read readdir readlink readlinkat readv rename \
renameat renameat2 rmdir stat statfs statx symlink symlinkat sync syncfs truncate \
unlink unlinkat utime utimensat utimes write writev"

log() { echo "[fsfull-baseline] $*"; }

require() {
    local tool="$1"
    command -v "$tool" >/dev/null 2>&1 || { echo "missing $tool" >&2; exit 1; }
}

require qemu-system-aarch64
require cpio
require gzip
require mkfs.exfat
require aarch64-linux-gnu-gcc
require aarch64-linux-gnu-strip
[ -r "$KERNEL_IMAGE" ] || { echo "kernel Image missing: $KERNEL_IMAGE" >&2; exit 1; }

# 1. Enumerate built binaries
log "enumerating fs test binaries"
CASES=()
for d in $FS_DIRS; do
    for bin in "$SYSCALL_DIR/$d"/*; do
        [ -x "$bin" ] && [ ! -d "$bin" ] && {
            base="$(basename "$bin")"
            # Skip non-test files: Makefile.deps, helper *.h, *.c
            case "$base" in
                *.c|*.h|*.o|*.in|Makefile*|README*|*.sh) continue ;;
            esac
            CASES+=("$d/$base")
        }
    done
done
log "loaded ${#CASES[@]} testcases across $(echo $FS_DIRS | wc -w) subdirs"

# 2. Build rootfs
rm -rf "$WORK"
mkdir -p "$ROOTFS"/{bin,sbin,proc,sys,dev,mnt/exfat,tmp,etc,ltp,lib,lib/aarch64-linux-gnu}

cp /bin/busybox "$ROOTFS/bin/busybox"
chmod +x "$ROOTFS/bin/busybox"
for cmd in sh ls mount umount echo cat sleep mkdir mknod losetup halt sync find timeout true false; do
    ln -sf busybox "$ROOTFS/bin/$cmd" || true
done

# Copy aarch64 glibc dynamic-link essentials — paths resolved by the
# cross-toolchain (same toolchain that built the LTP binaries).
CROSS_CC=${CROSS_CC:-aarch64-linux-gnu-gcc}
log "copying aarch64 glibc shared libs from cross-toolchain sysroot"
ldso=$("$CROSS_CC" -print-file-name=ld-linux-aarch64.so.1)
[ -r "$ldso" ] || { echo "cross-toolchain cannot locate ld-linux-aarch64.so.1" >&2; exit 1; }
cp -L "$ldso" "$ROOTFS/lib/"
for so in libc.so.6 libpthread.so.0 libdl.so.2 libm.so.6 librt.so.1 libnsl.so.1; do
    p=$("$CROSS_CC" -print-file-name="$so")
    [ -r "$p" ] && cp -L "$p" "$ROOTFS/lib/aarch64-linux-gnu/" || true
done

# Copy + strip test binaries (originals are unstripped, ~1MB each; strip → ~50-100KB)
log "copying + stripping ${#CASES[@]} test binaries"
for c in "${CASES[@]}"; do
    sub="${c%%/*}"
    name="${c##*/}"
    mkdir -p "$ROOTFS/ltp/$sub"
    cp "$SYSCALL_DIR/$c" "$ROOTFS/ltp/$sub/$name"
    aarch64-linux-gnu-strip "$ROOTFS/ltp/$sub/$name" 2>/dev/null || true
done

# init script — runs each test under per-test timeout
cat > "$ROOTFS/init" <<INITEOF
#!/bin/sh
/bin/busybox --install -s /bin

mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev || true

echo "=== Linux native exFAT LTP fsfull baseline ==="
echo "--- kernel: \$(uname -a) ---"

IMG=/dev/vda
[ -b /dev/vdb ] && IMG=/dev/vdb
echo "using \$IMG"
mount -t exfat "\$IMG" /mnt/exfat || { echo "MOUNT FAIL rc=\$?"; cat /proc/filesystems; halt -f; }
mount | grep exfat

echo
echo "=== running LTP fs cases (per-test timeout ${PER_TEST_TIMEOUT}s, time -p captured) ==="
# busybox 'time' builtin rejects leading var assignment (time -p VAR=val cmd → rc=127),
# so we export TMPDIR and feed time -p a clean utility chain (timeout + testcase).
export TMPDIR=/mnt/exfat
for sub in /ltp/*/; do
    sub_name=\$(basename "\$sub")
    for t in "\$sub"*; do
        [ -x "\$t" ] || continue
        case_name=\$(basename "\$t")
        cd /tmp
        # Redirect the brace-group's fd1 -> /dev/null (drops testcase stdout)
        # and fd2 -> _t (captures both 'time -p' output AND testcase stderr).
        # Internal cmd-level redirects would overwrite the fd time writes to,
        # so the redirect MUST be on the group, not on the inner command.
        { time -p timeout ${PER_TEST_TIMEOUT} "\$t"; } >/dev/null 2>/tmp/_t
        rc=\$?
        # POSIX time -p emits exactly one '^real ' line; testcase stderr almost
        # never starts with that prefix. Take the first match.
        real=\$(awk '/^real /{print \$2; exit}' /tmp/_t 2>/dev/null)
        echo "RC=\$rc TIME=\${real:-NA}s (\${sub_name}/\${case_name})"
    done
done

echo "=== TEST_DONE_MARKER ==="
sync
sleep 1
halt -f
INITEOF
chmod +x "$ROOTFS/init"

# 3. Pack initramfs
log "create initramfs (uncompressed; kernel may not have CONFIG_RD_GZIP)"
(cd "$ROOTFS" && find . -print0 | cpio --null -ov --format=newc 2>/dev/null) > "$INITRAMFS"
ls -la "$INITRAMFS"

# 4. Create exFAT image
log "create + format exfat backing image ($EXFAT_IMG_MIB MiB)"
truncate -s "${EXFAT_IMG_MIB}M" "$EXFAT_IMG"
mkfs.exfat -L LXBASE "$EXFAT_IMG" 2>&1 | tail -3

# 5. Boot
log "boot qemu-system-aarch64 ($BOOT_TIMEOUT s timeout)"
> "$SERIAL_LOG"
timeout "$BOOT_TIMEOUT" qemu-system-aarch64 -M virt -cpu cortex-a53 -smp 1 -m 2G \
    -nographic \
    -kernel "$KERNEL_IMAGE" \
    -initrd "$INITRAMFS" \
    -drive if=none,file="$EXFAT_IMG",format=raw,id=exfat \
    -device virtio-blk-device,drive=exfat \
    -append "console=ttyAMA0 panic=1 quiet" \
    2>&1 | tee "$SERIAL_LOG" || true

# 6. Parse RC=
log "parsing serial log"
python3 - "$SERIAL_LOG" "$RESULT_JSON" "$PER_TEST_TIMEOUT" <<'PYEOF'
import sys, json, re
log_path, json_path, per_test_timeout_s = sys.argv[1], sys.argv[2], int(sys.argv[3])
results = []
rx = re.compile(r'^RC=(-?\d+)\s+TIME=([0-9.]+|NA)s\s+\(([^)]+)\)\s*$')
with open(log_path, 'r', errors='replace') as f:
    for line in f:
        m = rx.match(line.strip())
        if m:
            rc, time_s, name = m.groups()
            elapsed = float(time_s) if time_s != 'NA' else None
            rc_int = int(rc)
            results.append({
                'case': name,
                'rc': rc_int,
                'elapsed_s': elapsed,
                'timed_out': rc_int == 124,
                'skipped': False,
            })
out = {
    'lane': 'linux-native-exfat-arm64-qemu-fsfull',
    'kernel_image': '/work/build-arm64/arch/arm64/boot/Image',
    'kernel_version': '5.15.0 trimmed arm64 defconfig (GCC 15.2)',
    'exfat_driver': 'Linux kernel in-tree CONFIG_EXFAT_FS=y',
    'qemu_machine': 'virt cortex-a53',
    'case_count': len(results),
    'per_test_timeout_s': per_test_timeout_s,
    'results': results,
}
with open(json_path, 'w') as f:
    json.dump(out, f, indent=2)
print(f'parsed {len(results)} results -> {json_path}')
# RC distribution
from collections import Counter
rc_count = Counter(r['rc'] for r in results)
print('--- rc distribution ---')
for rc, n in sorted(rc_count.items()):
    label = f' (timeout)' if rc == 124 else ''
    print(f'  RC={rc:>3}: {n}{label}')
# timing summary (only non-None elapsed)
timings = sorted(r['elapsed_s'] for r in results if r['elapsed_s'] is not None)
if timings:
    n = len(timings)
    def pct(p):
        return timings[min(n-1, int(p*n))]
    print('--- elapsed (s) summary ---')
    print(f'  min={timings[0]:.3f}  p50={pct(0.5):.3f}  p90={pct(0.9):.3f}  p99={pct(0.99):.3f}  max={timings[-1]:.3f}')
    print(f'  count_with_timing={n}  count_NA={len(results)-n}')
PYEOF

log "done. result: $RESULT_JSON"
