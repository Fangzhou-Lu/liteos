#!/usr/bin/env bash
# cmocka_local_run.sh — build & run exFAT cmocka host suite using local toolchain.
#
# Runs entirely on the host (no SSH, no Docker). Targets:
#   - macOS arm64 + Homebrew cmocka 2.x
#   - Linux x86_64 / arm64 + libcmocka-dev
#
# Usage:
#   bash tools/regress/cmocka_local_run.sh [--no-clean]
#
# Output:
#   testsuites/unittest/exfat/exfat_tests   (binary)
#   stdout: full cmocka log
#   exit code: 0 = all pass, !=0 = build failure or test failure

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="${REPO_ROOT}/testsuites/unittest/exfat"
CLEAN=1
for arg in "$@"; do
    case "$arg" in
        --no-clean) CLEAN=0 ;;
        -h|--help)
            sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
    esac
done

if [ ! -f "${TEST_DIR}/Makefile" ]; then
    echo "[cmocka-local] ERROR: ${TEST_DIR}/Makefile not found" >&2
    exit 2
fi

# -- preflight --------------------------------------------------------------
echo "[cmocka-local] host: $(uname -srm)"
if command -v brew >/dev/null 2>&1; then
    CMOCKA_PFX="$(brew --prefix cmocka 2>/dev/null || true)"
    if [ -z "${CMOCKA_PFX}" ]; then
        echo "[cmocka-local] WARN: cmocka not installed via Homebrew. Try: brew install cmocka" >&2
    else
        echo "[cmocka-local] cmocka: ${CMOCKA_PFX}"
    fi
elif command -v dpkg >/dev/null 2>&1; then
    if ! dpkg -s libcmocka-dev >/dev/null 2>&1; then
        echo "[cmocka-local] WARN: libcmocka-dev not installed. Try: apt install libcmocka-dev" >&2
    fi
fi

# -- build & run ------------------------------------------------------------
cd "${TEST_DIR}"
if [ "${CLEAN}" -eq 1 ]; then
    echo "[cmocka-local] make clean"
    make clean >/dev/null 2>&1 || true
fi

echo "[cmocka-local] make (compile + run)"
START_TS=$(date +%s)
make
RC=$?
END_TS=$(date +%s)
echo "[cmocka-local] elapsed: $((END_TS - START_TS))s, exit=${RC}"

# Final tally is printed by Makefile's run target.
exit "${RC}"
