#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
set -eu

case "${1:-}" in
  program)
    FLOW_OUT=${PROGRAM_FLOW:?PROGRAM_FLOW is required}
    NDI_NAME=${PROGRAM_NDI_NAME:-MXL Switcher Program}
    ;;
  preview)
    FLOW_OUT=${PREVIEW_FLOW:?PREVIEW_FLOW is required}
    NDI_NAME=${PREVIEW_NDI_NAME:-MXL Switcher Preview}
    ;;
  *)
    echo "usage: run-core-out.sh program|preview" >&2
    exit 2
    ;;
esac

: "${MXL_DOMAIN:?MXL_DOMAIN is required}"

exec gst-launch-1.0 -e \
  mxlsrc video-flow-id="$FLOW_OUT" domain="$MXL_DOMAIN" \
  ! videoconvert ! video/x-raw,format=UYVY \
  ! queue max-size-buffers=4 leaky=downstream \
  ! ndisink ndi-name="$NDI_NAME"
