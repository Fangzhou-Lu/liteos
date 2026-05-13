#!/usr/bin/env bash
# docker_qemu_ltp_run_fsfull.sh — run the 358-case fsfull LTP pack in local arm64 Docker/QEMU.
#
# This is the coexistence path: it does NOT replace the existing smoke runner.
# It reuses docker_qemu_ltp_run.sh with the fsfull pack contract:
#   out/ltp_fsfull.tar.gz + ltp_pack/run_fsfull.sh + ltp_pack/manifest.json

set -euo pipefail

LITEOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_ROOT="$(cd "${LITEOS_DIR}/../.." && pwd)"
TIMEOUT_S="${1:-120}"
ENTRY="${2:-py}"

export QEMU_LTP_TGZ="${QEMU_LTP_TGZ:-${REPO_ROOT}/out/ltp_fsfull.tar.gz}"
export QEMU_LTP_RUN_SCRIPT="${QEMU_LTP_RUN_SCRIPT:-run_fsfull.sh}"
export QEMU_LTP_PACK_SENTINEL="${QEMU_LTP_PACK_SENTINEL:-manifest.json}"
export QEMU_LTP_PACK_DIR="${QEMU_LTP_PACK_DIR:-/storage/ltp_pack}"
export QEMU_LTP_LOG="${QEMU_LTP_LOG:-${REPO_ROOT}/out/qemu_ltp_fsfull_log.txt}"
export QEMU_LTP_SERIAL_LOG="${QEMU_LTP_SERIAL_LOG:-${REPO_ROOT}/out/qemu_ltp_fsfull_serial.log}"

exec bash "${LITEOS_DIR}/tools/regress/docker_qemu_ltp_run.sh" "${ENTRY}" "${TIMEOUT_S}"
