#!/usr/bin/env bash
# docker_build_qemu.sh — build qemu_small_system_demo inside an existing oh-dev container.
#
# Verified local flow for the mini LiteOS-A tree:
#   1) use `hb build` only to regenerate GN/Ninja files
#   2) use ninja to build musl/sysroot/rootfs/kernel image targets explicitly
#
# This avoids the known `hb build` tail failure on the missing `images` target while
# still producing the real artifacts under out/arm_virt/qemu_small_system_demo/.
#
# Usage:
#   bash tools/regress/docker_build_qemu.sh
#   CONTAINER=oh-dev bash tools/regress/docker_build_qemu.sh
#   bash tools/regress/docker_build_qemu.sh -p qemu_small_system_demo@ohemu --target-cpu arm
#
# Artifacts:
#   out/arm_virt/qemu_small_system_demo/OHOS_Image
#   out/arm_virt/qemu_small_system_demo/OHOS_Image.bin
#   out/arm_virt/qemu_small_system_demo/sysroot/
#   out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out/rootfs_vfat.img

set -euo pipefail

LITEOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_ROOT="$(cd "${LITEOS_DIR}/../.." && pwd)"
CONTAINER="${CONTAINER:-oh-dev}"
CONTAINER_ROOT="${CONTAINER_ROOT:-/home/openharmony}"
OUT_SUBDIR="${OUT_SUBDIR:-out/arm_virt/qemu_small_system_demo}"
NINJA_TARGETS=(musl sysroot_lite rootfs make liteos build_kernel_image)

HB_ARGS=(-p qemu_small_system_demo@ohemu --target-cpu arm)
if [ "$#" -gt 0 ]; then
    HB_ARGS=("$@")
fi

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "[docker-build] ERROR: container '${CONTAINER}' not found." >&2
    echo "[docker-build]        Start or create the existing build container first." >&2
    exit 2
fi

RUNNING="$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)"
if [ "${RUNNING}" != "true" ]; then
    echo "[docker-build] ERROR: container '${CONTAINER}' is not running." >&2
    exit 2
fi

echo "[docker-build] repo root:      ${REPO_ROOT}"
echo "[docker-build] container:      ${CONTAINER}"
echo "[docker-build] container root: ${CONTAINER_ROOT}"
echo "[docker-build] hb args:        ${HB_ARGS[*]}"
echo "[docker-build] ninja targets:  ${NINJA_TARGETS[*]}"

START_TS=$(date +%s)

docker exec -w "${CONTAINER_ROOT}" "${CONTAINER}" bash -lc "
set -euo pipefail
export LITEOS_MANIFEST_ROOT='${CONTAINER_ROOT}'
HB_RC=0
hb build ${HB_ARGS[*]} || HB_RC=\$?
if [ ! -f '${OUT_SUBDIR}/build.ninja' ]; then
    echo '[docker-build] ERROR: build.ninja missing after hb build; GN generation did not complete.' >&2
    exit \${HB_RC:-1}
fi
prebuilts/build-tools/linux-aarch64/bin/ninja -C '${OUT_SUBDIR}' ${NINJA_TARGETS[*]}
"

END_TS=$(date +%s)
echo "[docker-build] elapsed: $((END_TS - START_TS))s"

ARTIFACTS=(
    "${REPO_ROOT}/${OUT_SUBDIR}/OHOS_Image"
    "${REPO_ROOT}/${OUT_SUBDIR}/OHOS_Image.bin"
    "${REPO_ROOT}/${OUT_SUBDIR}/sysroot"
    "${REPO_ROOT}/${OUT_SUBDIR}/obj/kernel/liteos_a/make_out/rootfs_vfat.img"
    "${REPO_ROOT}/${OUT_SUBDIR}/obj/kernel/liteos_a/make_out/rootfs.zip"
)

for artifact in "${ARTIFACTS[@]}"; do
    if [ -e "${artifact}" ]; then
        if [ -d "${artifact}" ]; then
            echo "[docker-build] artifact dir:  ${artifact}"
        else
            echo "[docker-build] artifact file: ${artifact} ($(wc -c < "${artifact}") bytes)"
        fi
    else
        echo "[docker-build] WARN: missing artifact ${artifact}" >&2
    fi
done
