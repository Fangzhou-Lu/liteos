#!/usr/bin/env bash
# docker_qemu_ltp_run.sh — run QEMU LTP pack inside an arm64 container.
#
# Wraps `tools/regress/qemu_ltp_run_local.{sh,py}` so the QEMU run reuses the
# same arm64 toolchain image that built OHOS_Image.bin (no host qemu install).
#
# Default payload is smoke (`out/ltp_smoke.tar.gz` + `run_exfat.sh`). Override:
#   - QEMU_LTP_TGZ           absolute repo path to pack tarball
#   - QEMU_LTP_RUN_SCRIPT    guest-side entry script (default run_exfat.sh)
#   - QEMU_LTP_PACK_SENTINEL guest-side sentinel under ltp_pack/ (default bin/open01)
#   - QEMU_LTP_PACK_DIR      guest-side extracted pack dir (default /storage/ltp_pack)
#   - QEMU_LTP_LOG / SERIAL_LOG for separate logs
#
# Usage:
#   bash tools/regress/docker_qemu_ltp_run.sh                  # python entry, smoke pack
#   bash tools/regress/docker_qemu_ltp_run.sh sh               # shell entry
#   bash tools/regress/docker_qemu_ltp_run.sh py 30            # explicit
#   QEMU_LTP_TGZ=/home/openharmony/out/ltp_fsfull.tar.gz \
#   QEMU_LTP_RUN_SCRIPT=run_fsfull.sh \
#   QEMU_LTP_PACK_SENTINEL=manifest.json \
#   bash tools/regress/docker_qemu_ltp_run.sh
#
# Timeout policy:
#   - Max total: 30 s by default in caller, hard clamp handled by local runner
#   - Poll cadence: 10 s
#   - Step timeout: 30 s

set -uo pipefail

LITEOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_ROOT="$(cd "${LITEOS_DIR}/../.." && pwd)"
IMAGE="${IMAGE:-oh-build:1.0}"

ENTRY="${1:-py}"
TIMEOUT_S="${2:-30}"
PAYLOAD_TGZ="${QEMU_LTP_TGZ:-${REPO_ROOT}/out/ltp_smoke.tar.gz}"
RUN_SCRIPT="${QEMU_LTP_RUN_SCRIPT:-run_exfat.sh}"
PACK_SENTINEL="${QEMU_LTP_PACK_SENTINEL:-bin/open01}"
PACK_DIR_GUEST="${QEMU_LTP_PACK_DIR:-/storage/ltp_pack}"
LOG_PATH="${QEMU_LTP_LOG:-${REPO_ROOT}/out/qemu_ltp_log.txt}"
SERIAL_LOG_PATH="${QEMU_LTP_SERIAL_LOG:-${REPO_ROOT}/out/qemu_ltp_serial.log}"

case "${ENTRY}" in
    sh|shell) ENTRY_CMD=(bash kernel/liteos_a/tools/regress/qemu_ltp_run_local.sh "${TIMEOUT_S}") ;;
    py|python) ENTRY_CMD=(python3 kernel/liteos_a/tools/regress/qemu_ltp_run_local.py "${TIMEOUT_S}") ;;
    *)
        echo "[docker-ltp] ERROR: unknown entry '${ENTRY}' (expect 'sh' or 'py')" >&2
        exit 2 ;;
esac

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "[docker-ltp] ERROR: docker image '${IMAGE}' not found." >&2
    echo "[docker-ltp]       Build with: docker build -t ${IMAGE} -f .docker/Dockerfile.oh-master ." >&2
    echo "[docker-ltp]       (default IMAGE=oh-build:1.0; override with IMAGE=<name:tag>)" >&2
    exit 2
fi
for must in "${REPO_ROOT}/out/arm_virt/qemu_small_system_demo/OHOS_Image.bin" "${REPO_ROOT}/out/smallmmc_exfat.img" "${PAYLOAD_TGZ}"; do
    if [ ! -f "${must}" ]; then
        echo "[docker-ltp] ERROR: missing ${must}" >&2
        echo "[docker-ltp]        Run docker_build_qemu.sh first, then build the requested LTP pack." >&2
        exit 2
    fi
done

if [[ "${PAYLOAD_TGZ}" != "${REPO_ROOT}/"* ]]; then
    echo "[docker-ltp] ERROR: QEMU_LTP_TGZ must be under repo root so it is visible inside the container: ${PAYLOAD_TGZ}" >&2
    exit 2
fi
PAYLOAD_TGZ_CONT="/home/openharmony/${PAYLOAD_TGZ#${REPO_ROOT}/}"
LOG_PATH_CONT="/home/openharmony/${LOG_PATH#${REPO_ROOT}/}"
SERIAL_LOG_PATH_CONT="/home/openharmony/${SERIAL_LOG_PATH#${REPO_ROOT}/}"

echo "[docker-ltp] repo:      ${REPO_ROOT}"
echo "[docker-ltp] image:     ${IMAGE}"
echo "[docker-ltp] entry:     ${ENTRY_CMD[*]}"
echo "[docker-ltp] timeout:   ${TIMEOUT_S}s"
echo "[docker-ltp] payload:   ${PAYLOAD_TGZ}"
echo "[docker-ltp] script:    ${RUN_SCRIPT}"
echo "[docker-ltp] sentinel:  ${PACK_SENTINEL}"
echo "[docker-ltp] guest dir: ${PACK_DIR_GUEST}"
echo "[docker-ltp] log:       ${LOG_PATH}"
echo "[docker-ltp] serial:    ${SERIAL_LOG_PATH}"

START_TS=$(date +%s)
docker run --rm -i \
    --platform linux/arm64 \
    -e "QEMU_LTP_RESEED=${QEMU_LTP_RESEED:-0}" \
    -e "QEMU_LTP_FLASH_BASE=${QEMU_LTP_FLASH_BASE:-}" \
    -e "QEMU_LTP_FLASH_WORK=${QEMU_LTP_FLASH_WORK:-}" \
    -e "QEMU_LTP_TGZ=${PAYLOAD_TGZ_CONT}" \
    -e "QEMU_LTP_RUN_SCRIPT=${RUN_SCRIPT}" \
    -e "QEMU_LTP_PACK_SENTINEL=${PACK_SENTINEL}" \
    -e "QEMU_LTP_PACK_DIR=${PACK_DIR_GUEST}" \
    -e "QEMU_LTP_LOG=${LOG_PATH_CONT}" \
    -e "QEMU_LTP_SERIAL_LOG=${SERIAL_LOG_PATH_CONT}" \
    -v "${REPO_ROOT}":/home/openharmony \
    -w /home/openharmony \
    "${IMAGE}" \
    "${ENTRY_CMD[@]}"
RC=$?
END_TS=$(date +%s)

echo "[docker-ltp] elapsed: $((END_TS - START_TS))s, exit=${RC}"
if [ -f "${LOG_PATH}" ]; then
    echo "[docker-ltp] log: ${LOG_PATH} ($(wc -l < "${LOG_PATH}") lines)"
fi
exit "${RC}"
