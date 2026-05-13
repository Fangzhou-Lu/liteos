#!/usr/bin/env bash
# build_ltp_fsfull_pack.sh — rebuild LiteOS LTP fsfull pack from Linux baseline JSON.
#
# Notes:
#   LTP's legacy old/tlibio.h gates several AIO/LIO helper macros on __linux__ only.
#   LiteOS clang defines __OHOS__/__LITEOS__ but not __linux__, so we inject -D__linux__
#   for this userspace-compat build to enable the expected LTP Linux code paths.
#
# Source of truth:
#   out/linux_native_ltp_fsfull.json   (results[].case, e.g. access/access01)
#
# Output:
#   out/ltp_fsfull.tar.gz
#   out/ltp_fsfull_pack/ltp_pack/{bin/<subdir>/<name>,run_fsfull.sh,manifest.json,cases.txt,dirs.txt}

set -euo pipefail

LITEOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_ROOT="$(cd "${LITEOS_DIR}/../.." && pwd)"
CONTAINER="${CONTAINER:-oh-dev}"
CONTAINER_ROOT="${CONTAINER_ROOT:-/home/openharmony}"
JSON_REL="${JSON_REL:-out/linux_native_ltp_fsfull.json}"
SYSROOT_REL="${SYSROOT_REL:-out/arm_virt/qemu_small_system_demo/sysroot}"
LINUX_HEADERS_REL="${LINUX_HEADERS_REL:-out/arm_virt/qemu_small_system_demo/obj/third_party/musl/scripts/build_lite/linux_header_install_for_liteos_a_user/usr/include}"
LTP_TREE="${LTP_TREE:-/work/oh-mini-ltp/ltp}"
PACK_REL="${PACK_REL:-out/ltp_fsfull_pack}"
TAR_REL="${TAR_REL:-out/ltp_fsfull.tar.gz}"
SKIP_CASES_REL="${SKIP_CASES_REL:-kernel/liteos_a/tools/regress/ltp_fsfull_skip_cases.txt}"
JOBS="${JOBS:-$(sysctl -n hw.ncpu 2>/dev/null || echo 8)}"
BUILD_ROOT="${BUILD_ROOT:-/tmp/ltp-liteos-fsfull-build}"

JSON_PATH="${REPO_ROOT}/${JSON_REL}"
PACK_DIR="${REPO_ROOT}/${PACK_REL}"
TAR_PATH="${REPO_ROOT}/${TAR_REL}"
SKIP_CASES_PATH="${REPO_ROOT}/${SKIP_CASES_REL}"

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "[ltp-fsfull] ERROR: container '${CONTAINER}' not found." >&2
    exit 2
fi
if [ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)" != "true" ]; then
    echo "[ltp-fsfull] ERROR: container '${CONTAINER}' is not running." >&2
    exit 2
fi
if [ ! -f "${JSON_PATH}" ]; then
    echo "[ltp-fsfull] ERROR: missing ${JSON_PATH}" >&2
    exit 2
fi
if [ ! -f "${SKIP_CASES_PATH}" ]; then
    echo "[ltp-fsfull] ERROR: missing skip list ${SKIP_CASES_PATH}" >&2
    exit 2
fi

echo "[ltp-fsfull] repo root:  ${REPO_ROOT}"
echo "[ltp-fsfull] container:  ${CONTAINER}"
echo "[ltp-fsfull] ltp tree:   ${LTP_TREE}"
echo "[ltp-fsfull] baseline:   ${JSON_PATH}"
echo "[ltp-fsfull] sysroot:    ${SYSROOT_REL}"
echo "[ltp-fsfull] linux hdrs: ${LINUX_HEADERS_REL}"
echo "[ltp-fsfull] output dir: ${PACK_DIR}"
echo "[ltp-fsfull] output tgz: ${TAR_PATH}"
echo "[ltp-fsfull] skip list:   ${SKIP_CASES_PATH}"
echo "[ltp-fsfull] build root:  ${BUILD_ROOT}"

rm -rf "${PACK_DIR}"
mkdir -p "${PACK_DIR}/ltp_pack/bin"

export JSON_PATH PACK_DIR SKIP_CASES_PATH
python3 - <<'PY'
import json
import os
from pathlib import Path

json_path = Path(os.environ['JSON_PATH'])
pack_dir = Path(os.environ['PACK_DIR'])
skip_cases_path = Path(os.environ['SKIP_CASES_PATH'])
ltp_pack = pack_dir / 'ltp_pack'
results = json.loads(json_path.read_text())['results']
all_cases = [item['case'] for item in results]
if not all_cases:
    raise SystemExit('[ltp-fsfull] ERROR: no cases in baseline json')
if any('/' not in case for case in all_cases):
    bad = [case for case in all_cases if '/' not in case][:10]
    raise SystemExit(f'[ltp-fsfull] ERROR: malformed case names without subdir: {bad}')
if len(all_cases) != len(set(all_cases)):
    seen = set()
    dup = []
    for case in all_cases:
        if case in seen:
            dup.append(case)
        seen.add(case)
    raise SystemExit(f'[ltp-fsfull] ERROR: duplicate case entries in baseline json: {dup[:10]}')

skip_reasons = {}
for raw in skip_cases_path.read_text(encoding='utf-8').splitlines():
    line = raw.strip()
    if not line or line.startswith('#'):
        continue
    if '\t' in line:
        case_name, reason = line.split('\t', 1)
    else:
        case_name, reason = line, 'unspecified skip reason'
    case_name = case_name.strip()
    reason = reason.strip() or 'unspecified skip reason'
    if '/' not in case_name:
        raise SystemExit(f'[ltp-fsfull] ERROR: malformed skip entry without subdir: {case_name!r}')
    skip_reasons[case_name] = reason

unknown_skips = sorted(case for case in skip_reasons if case not in all_cases)
if unknown_skips:
    raise SystemExit(f'[ltp-fsfull] ERROR: skip entries not present in baseline json: {unknown_skips[:10]}')

cases = [case for case in all_cases if case not in skip_reasons]
skipped_cases = [
    {'case': case, 'reason': skip_reasons[case]}
    for case in all_cases
    if case in skip_reasons
]
subdirs = sorted({case.split('/', 1)[0] for case in cases})
by_basename = {}
for case in cases:
    basename = case.split('/', 1)[1]
    by_basename.setdefault(basename, []).append(case)
collisions = {name: vals for name, vals in by_basename.items() if len(vals) > 1}
manifest = {
    'source_json': str(json_path),
    'source_case_count': len(all_cases),
    'case_count': len(cases),
    'skipped_case_count': len(skipped_cases),
    'subdir_count': len(subdirs),
    'cases': cases,
    'skipped_cases': skipped_cases,
    'subdirs': subdirs,
    'basename_collisions': collisions,
    'skip_case_file': str(skip_cases_path),
    'layout': {
        'bin_root': 'ltp_pack/bin',
        'runner': 'ltp_pack/run_fsfull.sh',
        'guest_dir': '/storage/ltp_pack',
    },
}
(pack_dir / 'cases.txt').write_text(''.join(case + '\n' for case in cases), encoding='utf-8')
(pack_dir / 'dirs.txt').write_text(''.join(d + '\n' for d in subdirs), encoding='utf-8')
(pack_dir / 'skipped_cases.txt').write_text(''.join(f"{item['case']}\t{item['reason']}\n" for item in skipped_cases), encoding='utf-8')
(ltp_pack / 'cases.txt').write_text(''.join(case + '\n' for case in cases), encoding='utf-8')
(ltp_pack / 'dirs.txt').write_text(''.join(d + '\n' for d in subdirs), encoding='utf-8')
(ltp_pack / 'skipped_cases.txt').write_text(''.join(f"{item['case']}\t{item['reason']}\n" for item in skipped_cases), encoding='utf-8')
(ltp_pack / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
runner = ltp_pack / 'run_fsfull.sh'
with runner.open('w', encoding='utf-8') as f:
    f.write('#!/bin/sh\n')
    f.write('set -eu\n')
    f.write('PACK_DIR=/storage/ltp_pack\n')
    f.write('MANIFEST="$PACK_DIR/cases.txt"\n')
    f.write('BIN_ROOT="$PACK_DIR/bin"\n')
    f.write('WORKDIR=/tmp\n')
    f.write('PER_TEST_TIMEOUT=${PER_TEST_TIMEOUT:-3}\n')
    f.write('mkdir -p "$WORKDIR" /mnt/exfat\n')
    f.write('cd "$WORKDIR"\n')
    f.write('echo "=== LTP exFAT fsfull Test ==="\n')
    f.write('while IFS= read -r case_rel; do\n')
    f.write('    [ -n "$case_rel" ] || continue\n')
    f.write('    t="$BIN_ROOT/$case_rel"\n')
    f.write('    if [ ! -x "$t" ]; then\n')
    f.write('        echo "RC=127 ($case_rel)"\n')
    f.write('        continue\n')
    f.write('    fi\n')
    f.write('    if command -v timeout >/dev/null 2>&1; then\n')
    f.write('        TMPDIR=/mnt/exfat timeout "$PER_TEST_TIMEOUT" "$t" 2>&1\n')
    f.write('    else\n')
    f.write('        TMPDIR=/mnt/exfat "$t" 2>&1\n')
    f.write('    fi\n')
    f.write('    echo "RC=$? ($case_rel)"\n')
    f.write('done < "$MANIFEST"\n')
    f.write('echo "=== SUMMARY ==="\n')
    f.write('echo TEST_DONE_MARKER\n')
runner.chmod(0o755)
print(f'[ltp-fsfull] loaded {len(all_cases)} cases from {json_path}')
print(f'[ltp-fsfull] skipped {len(skipped_cases)} cases from {skip_cases_path.name}')
print(f'[ltp-fsfull] will package {len(cases)} cases across {len(subdirs)} subdirs')
print(f'[ltp-fsfull] basename collisions: {len(collisions)}')
print(f'[ltp-fsfull] wrote manifest to {ltp_pack / "manifest.json"}')
print(f'[ltp-fsfull] wrote runner to {runner}')
PY

docker exec \
    -e LTP_TREE="${LTP_TREE}" \
    -e PACK_DIR="${CONTAINER_ROOT}/${PACK_REL}" \
    -e SYSROOT="${CONTAINER_ROOT}/${SYSROOT_REL}" \
    -e LINUX_HEADERS="${CONTAINER_ROOT}/${LINUX_HEADERS_REL}" \
    -e LLVM_BIN="${CONTAINER_ROOT}/prebuilts/clang/ohos/linux-aarch64/llvm/bin" \
    -e JOBS="${JOBS}" \
    -e BUILD_ROOT="${BUILD_ROOT}" \
    -w "${CONTAINER_ROOT}" "${CONTAINER}" bash -lc '
set -euo pipefail

[ -d "$LTP_TREE" ] || { echo "[ltp-fsfull] missing LTP tree: $LTP_TREE" >&2; exit 1; }
[ -f "$LTP_TREE/configure" ] || { echo "[ltp-fsfull] missing configure in $LTP_TREE" >&2; exit 1; }
[ -d "$LTP_TREE/.git" ] || { echo "[ltp-fsfull] ERROR: clean fsfull build requires git checkout at $LTP_TREE" >&2; exit 1; }
[ -d "$SYSROOT" ] || { echo "[ltp-fsfull] missing sysroot: $SYSROOT" >&2; exit 1; }
[ -d "$LINUX_HEADERS" ] || { echo "[ltp-fsfull] missing Linux headers: $LINUX_HEADERS" >&2; exit 1; }

rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT/src"
cleanup_build_root() {
    rm -rf "$BUILD_ROOT"
}
trap cleanup_build_root EXIT

echo "[ltp-fsfull] snapshot source tree into $BUILD_ROOT/src"
git -C "$LTP_TREE" archive --format=tar HEAD | tar -xf - -C "$BUILD_ROOT/src"

cd "$BUILD_ROOT/src"
if [ ! -f VERSION ]; then
    make -s Version >/dev/null
    if [ ! -f VERSION ] && [ -f Version ]; then
        cp Version VERSION
    fi
fi
make autotools >/dev/null
./configure --host=arm-liteos-ohos --prefix=/opt/ltp \
    CC="$LLVM_BIN/clang" \
    AR="$LLVM_BIN/llvm-ar" \
    STRIP="$LLVM_BIN/llvm-strip" \
    CPPFLAGS="-D__linux__ -isystem $LINUX_HEADERS" \
    CFLAGS="-D__linux__ -mfloat-abi=softfp -mfpu=neon-vfpv4 -mcpu=cortex-a7 -O2 --target=arm-liteos-ohos --sysroot=$SYSROOT -isystem $LINUX_HEADERS" \
    LDFLAGS="-mfloat-abi=softfp -mfpu=neon-vfpv4 -mcpu=cortex-a7 --target=arm-liteos-ohos --sysroot=$SYSROOT" >/dev/null

if grep -R -n "/work/ltp-arm32-sysroot" . >/tmp/ltp_fsfull_stale_sysroot.log 2>/dev/null; then
    echo "[ltp-fsfull] ERROR: stale ARM32 sysroot leaked into clean build tree" >&2
    cat /tmp/ltp_fsfull_stale_sysroot.log >&2
    exit 1
fi

while IFS= read -r subdir; do
    [ -n "$subdir" ] || continue
    echo "[ltp-fsfull] build dir $subdir"
    make -C "testcases/kernel/syscalls/$subdir" -j"$JOBS" >/dev/null
done < "$PACK_DIR/dirs.txt"

while IFS= read -r case_rel; do
    [ -n "$case_rel" ] || continue
    subdir="${case_rel%%/*}"
    name="${case_rel#*/}"
    src="$BUILD_ROOT/src/testcases/kernel/syscalls/$subdir/$name"
    dst_dir="$PACK_DIR/ltp_pack/bin/$subdir"
    dst="$dst_dir/$name"
    [ -x "$src" ] || { echo "[ltp-fsfull] missing built binary: $src" >&2; exit 1; }
    mkdir -p "$dst_dir"
    cp "$src" "$dst"
    "$LLVM_BIN/llvm-strip" --strip-unneeded "$dst" || true
done < "$PACK_DIR/cases.txt"
'

(
    cd "${PACK_DIR}"
    rm -f "${TAR_PATH}"
    tar -czf "${TAR_PATH}" ltp_pack
)

export PACK_DIR TAR_PATH
python3 - <<'PY'
import json
import os
import tarfile
from pathlib import Path

pack_root = Path(os.environ['PACK_DIR']) / 'ltp_pack'
manifest = json.loads((pack_root / 'manifest.json').read_text())
case_count = manifest['case_count']
tar_path = Path(os.environ['TAR_PATH'])
bin_files = sorted(
    str(path.relative_to(pack_root / 'bin'))
    for path in (pack_root / 'bin').rglob('*')
    if path.is_file()
)
if len(bin_files) != case_count:
    raise SystemExit(f'[ltp-fsfull] ERROR: expected {case_count} binaries, found {len(bin_files)}')
with tarfile.open(tar_path, 'r:gz') as tf:
    file_members = [m.name for m in tf.getmembers() if m.name.startswith('ltp_pack/bin/') and m.isfile()]
if len(file_members) != case_count:
    raise SystemExit(f'[ltp-fsfull] ERROR: tar expected {case_count} bin entries, found {len(file_members)}')
print(f'[ltp-fsfull] packaged {len(bin_files)} binaries into {tar_path}')
print(f'[ltp-fsfull] tar contains {len(file_members)} file entries')
print(f'[ltp-fsfull] skipped cases recorded: {manifest["skipped_case_count"]}')
print(f'[ltp-fsfull] basename collisions recorded: {len(manifest["basename_collisions"])}')
print(f'[ltp-fsfull] sample entries: {bin_files[:5]}')
PY

echo "[ltp-fsfull] done"
