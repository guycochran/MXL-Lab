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

FLOW_DIR="${MXL_DOMAIN%/}/${FLOW_OUT}.mxl-flow"
pipeline_pid=

stop_pipeline() {
  if [ -n "$pipeline_pid" ] && kill -0 "$pipeline_pid" 2>/dev/null; then
    kill -TERM "$pipeline_pid" 2>/dev/null || true
    wait "$pipeline_pid" 2>/dev/null || true
  fi
  pipeline_pid=
}

trap 'stop_pipeline; exit 0' INT TERM

while :; do
  while [ ! -d "$FLOW_DIR" ]; do
    echo "waiting for MXL flow $FLOW_OUT" >&2
    sleep 1
  done

  flow_inode=$(stat -c %i "$FLOW_DIR")
  echo "publishing $NDI_NAME from MXL flow $FLOW_OUT (inode $flow_inode)" >&2
  gst-launch-1.0 -e \
    mxlsrc video-flow-id="$FLOW_OUT" domain="$MXL_DOMAIN" \
    ! videoconvert ! video/x-raw,format=UYVY \
    ! queue max-size-buffers=4 leaky=downstream \
    ! ndisink ndi-name="$NDI_NAME" &
  pipeline_pid=$!

  while kill -0 "$pipeline_pid" 2>/dev/null; do
    current_inode=
    if [ -d "$FLOW_DIR" ]; then
      current_inode=$(stat -c %i "$FLOW_DIR" 2>/dev/null || true)
    fi
    if [ "$current_inode" != "$flow_inode" ]; then
      echo "MXL flow $FLOW_OUT was replaced; reopening it" >&2
      stop_pipeline
      break
    fi
    sleep 1
  done

  wait "$pipeline_pid" 2>/dev/null || true
  pipeline_pid=
  sleep 1
done
