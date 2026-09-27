#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
#
# Pin the Dockerfile to the upstream versions an existing, working lab image
# was built from, so a fresh build reproduces it.
#
#   scripts/pin-from-image.sh              read mxl-lab:latest, update Dockerfile
#   scripts/pin-from-image.sh --dry-run    only print what it found
set -euo pipefail
cd "$(dirname "$0")/.."

image=mxl-lab:latest
dry_run=no
[ "${1:-}" = "--dry-run" ] && dry_run=yes

if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "No $image image found. Build the lab first." >&2
  exit 1
fi

# shellcheck disable=SC2016  # expanded inside the container, not here
found=$(docker run --rm --entrypoint sh "$image" -c '
  echo "MXL_REF=$(git -C /src/mxl rev-parse HEAD)"
  echo "VCPKG_REF=$(git -C /opt/vcpkg rev-parse HEAD)"
  echo "GST_PLUGINS_RS_REF=$(git -C /src/gst-plugins-rs rev-parse HEAD)"
  echo "RUST_TOOLCHAIN=$(rustc --version | cut -d" " -f2)"
  echo "OPENCV_VERSION=$(pip3 show opencv-python-headless 2>/dev/null | sed -n "s/^Version: //p")"
  echo "MODEL_SHA256=$(sha256sum /opt/models/face_detection_yunet_2023mar.onnx | cut -d" " -f1)"
')
echo "Built into $image:"
while IFS= read -r line; do echo "  $line"; done <<< "$found"

get() { echo "$found" | sed -n "s/^$1=//p"; }

model_sha=$(get MODEL_SHA256)
pinned_sha=$(sed -n 's/^ARG YUNET_SHA256=//p' Dockerfile)
if [ "$model_sha" != "$pinned_sha" ]; then
  echo "Note: the image's face model differs from the one pinned in the Dockerfile." >&2
  echo "      The pinned model will be used by the next build." >&2
fi

[ "$dry_run" = yes ] && exit 0

for key in MXL_REF VCPKG_REF GST_PLUGINS_RS_REF RUST_TOOLCHAIN OPENCV_VERSION; do
  value=$(get "$key")
  if [ -z "$value" ]; then
    echo "Could not read $key from the image, leaving it as it is." >&2
    continue
  fi
  sed -i "s|^ARG ${key}=.*|ARG ${key}=${value}|" Dockerfile
done
echo "Dockerfile updated. Review with: git diff Dockerfile"
