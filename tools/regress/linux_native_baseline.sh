#!/bin/bash
# Linux-native exFAT LTP baseline — qemu-system-aarch64 -M virt.
#
# Builds initramfs containing busybox + static aarch64 LTP testcases, boots
# Linux kernel image with EXFAT in-tree driver, runs the same caselist as
# the QEMU LiteOS-A lane and the exfat-fuse lane. RC table written to
# /home/openharmony/out/linux_native_ltp_baseline.json.

set -euo pipefail

REPO_ROOT=/home/openharmony
# Linux source bind-mounted from host ~/Codebase/linux (5.15.0 arm64 build).
KERNEL_IMAGE=${KERNEL_IMAGE:-/work/linux-src/arch/arm64/boot/Image}
# LTP aarch64 testcases are dynamically linked (interpreter /lib/ld-linux-aarch64.so.1).
LTP_TREE=${LTP_TREE:-/work/oh-mini-ltp/ltp}
SYSCALL_DIR="$LTP_TREE/testcases/kernel/syscalls"
CASELIST="$REPO_ROOT/kernel/liteos_a/tools/regress/ltp_exfat_caselist.txt"
WORK=/tmp/linux-baseline
INITRAMFS="$WORK/initramfs.cpio.gz"
ROOTFS="$WORK/rootfs"
EXFAT_IMG="$WORK/exfat.img"
SERIAL_LOG="$WORK/serial.log"
RESULT_JSON="$REPO_ROOT/out/linux_native_ltp_baseline.json"
EXFAT_IMG_MIB=${EXFAT_IMG_MIB:-64}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-180}

log() { echo "[linux-native-baseline] $*"; }

require() {
    local tool="$1"
    command -v "$tool" >/dev/null 2>&1 || { echo "missing $tool" >&2; exit 1; }
}

require qemu-system-aarch64
require cpio
require gzip
require mkfs.exfat
require aarch64-linux-gnu-gcc

[ -r "$KERNEL_IMAGE" ] || { echo "kernel Image missing: $KERNEL_IMAGE" >&2; exit 1; }
[ -r "$CASELIST" ] || { echo "caselist missing: $CASELIST" >&2; exit 1; }

mapfile -t CASES < <(grep -vE '^[[:space:]]*(#|$)' "$CASELIST")
log "loaded ${#CASES[@]} testcases"

rm -rf "$WORK"
mkdir -p "$ROOTFS"/{bin,sbin,proc,sys,dev,mnt/exfat,tmp,etc,ltp,lib,lib/aarch64-linux-gnu}

cp /bin/busybox "$ROOTFS/bin/busybox"
chmod +x "$ROOTFS/bin/busybox"
for cmd in sh ls mount umount echo cat sleep mkdir mknod losetup halt sync find; do
    ln -sf busybox "$ROOTFS/bin/$cmd" || true
done

# Glibc runtime for dynamic-linked aarch64 LTP binaries — paths resolved
# via the cross toolchain (aarch64-linux-gnu-gcc) instead of hardcoded
# host directories.
CROSS_CC=${CROSS_CC:-aarch64-linux-gnu-gcc}
log "copying aarch64 glibc runtime from cross-toolchain sysroot"
ldso=$("$CROSS_CC" -print-file-name=ld-linux-aarch64.so.1)
[ -r "$ldso" ] || { echo "cross-toolchain cannot locate ld-linux-aarch64.so.1" >&2; exit 1; }
cp -L "$ldso" "$ROOTFS/lib/"
for so in libc.so.6 libpthread.so.0 libdl.so.2 libm.so.6 librt.so.1; do
    p=$("$CROSS_CC" -print-file-name="$so")
    [ -r "$p" ] && cp -L "$p" "$ROOTFS/lib/aarch64-linux-gnu/" || true
done

for case in "${CASES[@]}"; do
    base="${case%%[0-9]*}"
    src="$SYSCALL_DIR/$base/$case"
    [ -x "$src" ] || { echo "missing testcase binary: $src" >&2; exit 1; }
    cp "$src" "$ROOTFS/ltp/$case"
done

cat > "$ROOTFS/init" <<'EOF'
#!/bin/busybox sh
/bin/busybox --install -s /bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev || true

echo "=== Linux native exFAT LTP baseline ==="
echo "--- kernel: $(uname -a) ---"

ls /dev/ | head -20
echo "--- block devs ---"
ls /sys/class/block/ 2>&1 | head -10
IMG=/dev/vda
[ -b /dev/vdb ] && IMG=/dev/vdb
echo "using $IMG"
mount -t exfat "$IMG" /mnt/exfat || { echo "MOUNT FAIL rc=$?"; cat /proc/filesystems; halt -f; }
mount | grep exfat

echo
echo "=== running LTP cases ==="
for t in $(ls /ltp/); do
    cd /tmp
    TMPDIR=/mnt/exfat /ltp/$t 2>&1
    echo "RC=$? ($t)"
done

echo "=== TEST_DONE_MARKER ==="
sync
sleep 1
halt -f
EOF
chmod +x "$ROOTFS/init"

log "create initramfs"
(cd "$ROOTFS" && find . -print0 | cpio --null -ov --format=newc 2>/dev/null) | gzip -9 > "$INITRAMFS"
ls -la "$INITRAMFS"

log "create + format exfat backing image on host ($EXFAT_IMG_MIB MiB)"
truncate -s "${EXFAT_IMG_MIB}M" "$EXFAT_IMG"
mkfs.exfat -L LXBASE "$EXFAT_IMG" 2>&1 | tail -3

log "launching qemu-system-aarch64 -M virt -cpu cortex-a53"
timeout "$BOOT_TIMEOUT" qemu-system-aarch64 \
    -M virt -cpu cortex-a53 -smp 1 -m 512M \
    -kernel "$KERNEL_IMAGE" \
    -initrd "$INITRAMFS" \
    -drive if=none,file="$EXFAT_IMG",format=raw,id=blk0 \
    -device virtio-blk-device,drive=blk0 \
    -append "console=ttyAMA0 panic=1 quiet" \
    -nographic > "$SERIAL_LOG" 2>&1 || true

log "qemu exited; parsing serial log"
echo "--- last 60 lines of serial ---"
tail -60 "$SERIAL_LOG"

mkdir -p "$(dirname "$RESULT_JSON")"
python3 - "$SERIAL_LOG" "$CASELIST" "$RESULT_JSON" <<'PY'
import json, re, sys
serial_path, caselist_path, out_path = sys.argv[1:4]
with open(caselist_path) as f:
    cases = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
serial = open(serial_path).read()
rc_re = re.compile(r'RC=(\d+) \((\w+)\)')
rcs = {m.group(2): int(m.group(1)) for m in rc_re.finditer(serial)}
results = []
for c in cases:
    if c in rcs:
        results.append({"case": c, "rc": rcs[c], "skipped": False})
    else:
        results.append({"case": c, "rc": None, "skipped": True, "reason": "no RC line in serial"})
counts = {}
for r in results:
    key = "MISSING" if r["skipped"] else f"RC={r['rc']}"
    counts[key] = counts.get(key, 0) + 1
summary = {
    "lane": "linux-native-exfat-arm64-qemu",
    "kernel_image": "/work/linux-src/arch/arm64/boot/Image",
    "kernel_version": "5.15.0 multi_v7_defconfig-equivalent for arm64",
    "exfat_driver": "Linux kernel in-tree CONFIG_EXFAT_FS=y",
    "qemu_machine": "virt cortex-a53",
    "case_count": len(cases),
    "results": results,
    "rc_counts": counts,
}
with open(out_path, "w") as f:
    json.dump(summary, f, indent=2)
print("wrote", out_path)
print("rc counts:", counts)
PY
