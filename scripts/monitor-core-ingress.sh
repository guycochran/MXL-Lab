#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Log container health, resources and MXL flow snapshots without changing state.
set -eu

ENV_FILE=${ENV_FILE:-env.core-ingress}
CONTAINER=${CONTAINER:-mxl-ndi-core-ingress}
INTERVAL=${INTERVAL:-10}
OUT_DIR=${1:-run-core-ingress-$(date -u +%Y%m%dT%H%M%SZ)}

[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE; copy env.core-ingress.example first" >&2; exit 2; }
MXL_DOMAIN=$(sed -n 's/^MXL_DOMAIN=//p' "$ENV_FILE" | tail -n 1)
FLOW_A=$(sed -n 's/^FLOW_A=//p' "$ENV_FILE" | tail -n 1)
[ -n "$MXL_DOMAIN" ] && [ -n "$FLOW_A" ] || { echo "MXL_DOMAIN and FLOW_A are required" >&2; exit 2; }
case "$MXL_DOMAIN" in
  /dev/shm/mxl/*) ;;
  /dev/shm/mxl) echo "refusing to monitor the shared parent domain" >&2; exit 2 ;;
  *) echo "MXL_DOMAIN must be an absolute child path under /dev/shm/mxl" >&2; exit 2 ;;
esac

mkdir -p "$OUT_DIR"
printf 'timestamp\tstate\thealth\tflow_a_present\tcpu\tmemory\tnet_io\n' >"$OUT_DIR/resources.tsv"
: >"$OUT_DIR/mxl-info.log"
: >"$OUT_DIR/events.log"

while docker inspect "$CONTAINER" >/dev/null 2>&1; do
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  state=$(docker inspect --format '{{.State.Status}}' "$CONTAINER")
  health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER")
  if [ -d "$MXL_DOMAIN/${FLOW_A}.mxl-flow" ]; then flow_present=true; else flow_present=false; fi
  stats=$(docker stats --no-stream --format '{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}' "$CONTAINER" 2>/dev/null || printf '\t\t')
  printf '%s\t%s\t%s\t%s\t%s\n' "$timestamp" "$state" "$health" "$flow_present" "$stats" >>"$OUT_DIR/resources.tsv"
  {
    printf '\n[%s]\n' "$timestamp"
    docker exec "$CONTAINER" mxl-info -d "$MXL_DOMAIN" -l 2>&1 || true
  } >>"$OUT_DIR/mxl-info.log"
  [ "$state" = running ] || { printf '%s container state=%s health=%s\n' "$timestamp" "$state" "$health" >>"$OUT_DIR/events.log"; break; }
  sleep "$INTERVAL"
done

timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  docker inspect --format "${timestamp} exit_code={{.State.ExitCode}} state={{.State.Status}} error={{.State.Error}}" "$CONTAINER" >>"$OUT_DIR/events.log"
else
  printf '%s container missing\n' "$timestamp" >>"$OUT_DIR/events.log"
fi
