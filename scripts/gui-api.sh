#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
#
# Sourced by setup.sh and lab-apply.sh. Defines gui_url and gui_curl, which
# reach the GUI's API with the address and login set in .env.

env_get() {
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

gui_port=$(env_get GUI_PORT)
gui_port=${gui_port:-8080}
gui_host=$(env_get GUI_BIND)
if [ -z "$gui_host" ] || [ "$gui_host" = "0.0.0.0" ]; then
  gui_host=127.0.0.1
fi
# shellcheck disable=SC2034  # used by the scripts that source this file
gui_url="http://${gui_host}:${gui_port}"

gui_auth=()
if [ -n "$(env_get GUI_USER)" ] && [ -n "$(env_get GUI_PASSWORD)" ]; then
  gui_auth=(-u "$(env_get GUI_USER):$(env_get GUI_PASSWORD)")
fi

gui_curl() {
  curl -fs "${gui_auth[@]}" "$@"
}
