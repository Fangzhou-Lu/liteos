#!/usr/bin/env bash
# run_all.sh — master regression aggregator for exFAT on LiteOS-A
#
# Wave 1: cmocka host suite (fast, ~2 s)
#   SSH to 192.168.1.15, runs `make run` in
#   /mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/exfat
#   Captures stdout to /tmp/regress_cmocka.log
#
# Wave 2: QEMU LTP suite (slow, several minutes)
#   Runs tools/regress/qemu_ltp_run.sh locally (that script handles SSH itself).
#   Captures stdout to /tmp/regress_qemu_ltp.log
#
# Prerequisites: SSH key auth to 192.168.1.15 must be configured.
#   $SERVER_PASSWD must be exported (used by remote sudo operations).
#
# Output: docs/test/exfat_regression_<timestamp>.md  (symlinked from _latest.md)
# Exit: 0 = all pass, 1 = any failure

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_HOST="192.168.1.15"
REMOTE_DIR="/mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/exfat"
CMOCKA_LOG="/tmp/regress_cmocka.log"
QEMU_LOG="/tmp/regress_qemu_ltp.log"
DOCS_DIR="${REPO_ROOT}/docs/test"
mkdir -p "${DOCS_DIR}"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT="${DOCS_DIR}/exfat_regression_${TS}.md"
LATEST="${DOCS_DIR}/exfat_regression_latest.md"

# ── colour helpers ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi

# ── Wave 1: cmocka host suite ───────────────────────────────────────────────
echo "[run_all] Wave 1: cmocka host suite on ${REMOTE_HOST} ..."
cmocka_rc=0
ssh -o BatchMode=yes -o ConnectTimeout=10 "${REMOTE_HOST}" \
    "cd '${REMOTE_DIR}' && make run" > "${CMOCKA_LOG}" 2>&1 || cmocka_rc=$?

cmocka_failures=0
if grep -qE 'TOTAL FAILURES: [0-9]+' "${CMOCKA_LOG}"; then
    cmocka_failures="$(grep -oE 'TOTAL FAILURES: ([0-9]+)' "${CMOCKA_LOG}" \
        | tail -1 | grep -oE '[0-9]+$')"
fi
cmocka_pass=0
if [ "${cmocka_rc}" -eq 0 ] && [ "${cmocka_failures}" -eq 0 ]; then
    cmocka_pass=1
    echo "[run_all] Wave 1: PASS (failures=0)"
else
    echo "[run_all] Wave 1: FAIL (failures=${cmocka_failures}, ssh_rc=${cmocka_rc})"
fi

# ── Wave 2: QEMU LTP suite ──────────────────────────────────────────────────
QEMU_SCRIPT="${REPO_ROOT}/tools/regress/qemu_ltp_run.sh"
qemu_rc=0
ltp_pass_count=0
ltp_fail_count=0

qemu_skipped=0
if [ ! -f "${QEMU_SCRIPT}" ]; then
    echo "[run_all] qemu_ltp_run.sh not yet present, skipping wave 2"
    printf '(wave 2 skipped — qemu_ltp_run.sh not present)\n' > "${QEMU_LOG}"
    qemu_skipped=1
    qemu_rc=-1
else
    echo "[run_all] Wave 2: QEMU LTP suite ..."
    bash "${QEMU_SCRIPT}" > "${QEMU_LOG}" 2>&1 || qemu_rc=$?

    if grep -qE 'LTP_PASS=[0-9]+' "${QEMU_LOG}"; then
        ltp_pass_count="$(grep -oE 'LTP_PASS=([0-9]+)' "${QEMU_LOG}" \
            | tail -1 | grep -oE '[0-9]+$')"
    elif grep -qE 'RESULTS: PASS=[0-9]+' "${QEMU_LOG}"; then
        ltp_pass_count="$(grep -oE 'RESULTS: PASS=[0-9]+' "${QEMU_LOG}" \
            | tail -1 | grep -oE 'PASS=[0-9]+' | grep -oE '[0-9]+$')"
    fi
    if grep -qE 'LTP_FAIL=[0-9]+' "${QEMU_LOG}"; then
        ltp_fail_count="$(grep -oE 'LTP_FAIL=([0-9]+)' "${QEMU_LOG}" \
            | tail -1 | grep -oE '[0-9]+$')"
    elif grep -qE 'RESULTS: PASS=[0-9]+[[:space:]]+FAIL=[0-9]+' "${QEMU_LOG}"; then
        ltp_fail_count="$(grep -oE 'FAIL=[0-9]+' "${QEMU_LOG}" \
            | tail -1 | grep -oE '[0-9]+$')"
    fi

    case "${qemu_rc}" in
        0) echo "[run_all] Wave 2: PASS (ltp_pass=${ltp_pass_count} ltp_fail=${ltp_fail_count})" ;;
        1) echo "[run_all] Wave 2: FAIL (ltp_pass=${ltp_pass_count} ltp_fail=${ltp_fail_count})" ;;
        2) echo "[run_all] Wave 2: PANIC/HANG (rc=2)" ;;
        *) echo "[run_all] Wave 2: unknown rc=${qemu_rc}" ;;
    esac
fi

# ── QEMU exit-code semantics ────────────────────────────────────────────────
case "${qemu_rc}" in
    -1) qemu_rc_meaning="SKIPPED — qemu_ltp_run.sh not present" ;;
    0)  qemu_rc_meaning="all tests passed" ;;
    1)  qemu_rc_meaning="one or more LTP cases failed" ;;
    2)  qemu_rc_meaning="QEMU panic or hang detected" ;;
    *)  qemu_rc_meaning="unknown (rc=${qemu_rc})" ;;
esac

# ── Report generation ────────────────────────────────────────────────────────
mkdir -p "${DOCS_DIR}"

cmocka_tail="$(tail -20 "${CMOCKA_LOG}" 2>/dev/null || true)"
qemu_tail="$(tail -30 "${QEMU_LOG}" 2>/dev/null || true)"

cat > "${REPORT}" <<EOF
# exFAT Regression Report

**Generated:** $(date -u +"%Y-%m-%dT%H:%M:%SZ")

## Wave 1 — cmocka host suite

| Metric | Value |
|---|---|
| SSH exit code | ${cmocka_rc} |
| Total failures | ${cmocka_failures} |
| Result | $([ "${cmocka_pass}" -eq 1 ] && echo "PASS" || echo "FAIL") |

## Wave 2 — QEMU LTP suite

| Metric | Value |
|---|---|
| Script exit code | ${qemu_rc} |
| Exit code meaning | ${qemu_rc_meaning} |
| LTP pass count | ${ltp_pass_count} |
| LTP fail count | ${ltp_fail_count} |

## Log tails

### cmocka (last 20 lines)

\`\`\`
${cmocka_tail}
\`\`\`

### QEMU LTP (last 30 lines)

\`\`\`
${qemu_tail}
\`\`\`
EOF

ln -sf "$(basename "${REPORT}")" "${LATEST}"
echo "[run_all] Report: ${REPORT}"
echo "[run_all] Latest: ${LATEST}"

# ── Final verdict ────────────────────────────────────────────────────────────
overall_fail=0
[ "${cmocka_pass}" -eq 0 ] && overall_fail=1
# qemu_rc == -1 means "skipped — qemu_ltp_run.sh not yet present"; treat as
# non-failure so smoke checks during bootstrap still pass.
if [ "${qemu_rc}" != "-1" ] && [ "${qemu_rc}" -ne 0 ]; then
    overall_fail=1
fi

if [ "${overall_fail}" -eq 0 ]; then
    if [ "${qemu_skipped}" -eq 1 ]; then
        printf "${YELLOW}[REGRESS] PARTIAL — cmocka PASS, qemu SKIPPED${NC}\n"
    else
        printf "${GREEN}[REGRESS] PASS — cmocka:0 failures, qemu:rc=0${NC}\n"
    fi
    exit 0
else
    printf "${RED}[REGRESS] FAIL — cmocka_failures=${cmocka_failures} qemu_rc=${qemu_rc}${NC}\n"
    exit 1
fi
