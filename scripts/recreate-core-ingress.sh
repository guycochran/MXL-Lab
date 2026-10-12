#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Recreate exactly one Core ingress while preserving its pilot environment.
set -eu

target=${1:-}
outage_seconds=${OUTAGE_SECONDS:-3}
timeout_seconds=${RECOVERY_TIMEOUT:-90}

case "$target" in
  a|primary|ndi-core-ingress)
    service=ndi-core-ingress
    container=mxl-ndi-core-ingress
    env_file=${CORE_INGRESS_ENV_FILE:-env.core-ingress}
    selector=CORE_INGRESS_ENV_FILE
    ;;
  b|secondary|ndi-core-ingress-b)
    service=ndi-core-ingress-b
    container=mxl-ndi-core-ingress-b
    env_file=${CORE_INGRESS_B_ENV_FILE:-env.core-ingress-b}
    selector=CORE_INGRESS_B_ENV_FILE
    ;;
  *)
    echo "usage: $0 a|b" >&2
    exit 2
    ;;
esac

case "$outage_seconds:$timeout_seconds" in
  *[!0-9:]*|:*) echo "OUTAGE_SECONDS and RECOVERY_TIMEOUT must be non-negative integers" >&2; exit 2 ;;
esac
[ -r "$env_file" ] || { echo "missing $env_file" >&2; exit 2; }

read_value() {
  sed -n "s/^$1=//p" "$env_file" | tail -n 1
}

mxl_domain=$(read_value MXL_DOMAIN)
flow_a=$(read_value FLOW_A)
ndi_src=$(read_value NDI_SRC)
ndi_url=$(read_value NDI_URL)

[ -n "$flow_a" ] || { echo "FLOW_A is required in $env_file" >&2; exit 2; }
[ -n "$ndi_src" ] || [ -n "$ndi_url" ] || {
  echo "NDI_SRC or NDI_URL is required in $env_file" >&2
  exit 2
}
case "$mxl_domain" in
  /dev/shm/mxl/*) ;;
  *) echo "MXL_DOMAIN must be an isolated child path under /dev/shm/mxl" >&2; exit 2 ;;
esac

flow_path="$mxl_domain/$flow_a.mxl-flow"
before_inode=missing
[ -d "$flow_path" ] && before_inode=$(stat -c %i "$flow_path")

echo "recreating $service with $env_file; planned outage ${outage_seconds}s"
docker compose --profile ndi-core-ingress stop "$service"
sleep "$outage_seconds"
env "$selector=$env_file" docker compose --profile ndi-core-ingress up -d --force-recreate "$service"

elapsed=0
state=missing
while [ "$elapsed" -lt "$timeout_seconds" ]; do
  state=$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)
  [ "$state" = running/healthy ] && [ -d "$flow_path" ] && break
  sleep 1
  elapsed=$((elapsed + 1))
done

after_inode=missing
[ -d "$flow_path" ] && after_inode=$(stat -c %i "$flow_path")
printf 'service=%s state=%s wait_seconds=%s flow=%s inode_before=%s inode_after=%s\n' \
  "$service" "$state" "$elapsed" "$flow_a" "$before_inode" "$after_inode"

[ "$state" = running/healthy ] && [ "$after_inode" != missing ]

