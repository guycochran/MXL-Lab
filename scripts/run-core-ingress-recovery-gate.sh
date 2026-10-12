#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Exercise alternating Core ingress replacement while both NDI outputs are read.
set -eu

rounds=${ROUNDS:-5}
settle_seconds=${SETTLE_SECONDS:-10}
max_ingress_restarts=${MAX_INGRESS_RESTARTS:-1}
min_recovered_window=${MIN_RECOVERED_WINDOW:-24}
max_recovered_skew=${MAX_RECOVERED_SKEW:-4}
health_url=${CORE_HEALTH_URL:-http://127.0.0.1:18080/api/mxl/v1/health}
output_dir=${1:-"recovery-gate-$(date -u +%Y%m%dT%H%M%SZ)"}

case "$rounds:$settle_seconds:$max_ingress_restarts:$min_recovered_window:$max_recovered_skew" in
  *[!0-9:]*|:*) echo "gate thresholds must be non-negative integers" >&2; exit 2 ;;
esac
[ "$rounds" -gt 0 ] || { echo "ROUNDS must be greater than zero" >&2; exit 2; }

for command_name in curl docker jq sed; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "missing required command: $command_name" >&2
    exit 2
  }
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
recreate_script="$script_dir/recreate-core-ingress.sh"
[ -x "$recreate_script" ] || { echo "missing executable $recreate_script" >&2; exit 2; }

flow_a=4d584c10-2001-4c10-8000-000000000001
flow_b=4d584c10-2002-4c10-8000-000000000002
core=mxl-core-pilot
ingress_a=mxl-ndi-core-ingress
ingress_b=mxl-ndi-core-ingress-b
program_out=mxl-ndi-core-program-out
preview_out=mxl-ndi-core-preview-out
program_probe=mxl-ndi-core-program-probe
preview_probe=mxl-ndi-core-preview-probe

mkdir -p "$output_dir"
output_dir=$(CDPATH= cd -- "$output_dir" && pwd)
events="$output_dir/events.tsv"
printf 'utc\tcycle\ttarget\tresult\ttarget_restarts\treopen_delta\theld_delta\tno_common_delta\tlast_window\tlast_skew\tprogram_rendered\tpreview_rendered\n' >"$events"

health_snapshot() {
  destination=$1
  curl -fsS "$health_url" >"$destination"
  jq -e '.status == "running"' "$destination" >/dev/null
}

container_snapshot() {
  destination=$1
  : >"$destination"
  for name in "$core" "$ingress_a" "$ingress_b" "$program_out" "$preview_out" "$program_probe" "$preview_probe"; do
    docker inspect --format '{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}' "$name" >>"$destination"
  done
}

validate_containers() {
  state=$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}/{{.RestartCount}}' "$core")
  [ "$state" = running/healthy/0 ] || { echo "$core is $state" >&2; return 1; }
  for name in "$ingress_a" "$ingress_b"; do
    state=$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}/{{.RestartCount}}' "$name")
    status_health=${state%/*}
    restart_count=${state##*/}
    [ "$status_health" = running/healthy ] && [ "$restart_count" -le "$max_ingress_restarts" ] || {
      echo "$name is $state (maximum accepted ingest retries: $max_ingress_restarts)" >&2
      return 1
    }
  done
  for name in "$program_out" "$preview_out"; do
    state=$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}/{{.RestartCount}}' "$name")
    case "$state" in running/healthy/0|running/none/0) ;; *) echo "$name is $state" >&2; return 1 ;; esac
  done
  for name in "$program_probe" "$preview_probe"; do
    state=$(docker inspect --format '{{.State.Status}}/{{.RestartCount}}' "$name")
    [ "$state" = running/0 ] || { echo "$name is $state" >&2; return 1; }
  done
}

probe_totals() {
  docker logs "$1" 2>&1 \
    | sed -n 's/.*rendered: \([0-9][0-9]*\), dropped: \([0-9][0-9]*\).*/\1 \2/p' \
    | tail -n 1
}

check_unchanged() {
  field=$1
  before_file=$2
  after_file=$3
  before_value=$(jq -r ".$field" "$before_file")
  after_value=$(jq -r ".$field" "$after_file")
  [ "$before_value" = "$after_value" ] || {
    echo "$field changed from $before_value to $after_value" >&2
    return 1
  }
}

validate_containers
health_snapshot "$output_dir/health-baseline.json"
container_snapshot "$output_dir/containers-baseline.txt"
set -- $(probe_totals "$program_probe"); previous_program=${1:-0}; program_drops=${2:-999}
set -- $(probe_totals "$preview_probe"); previous_preview=${1:-0}; preview_drops=${2:-999}
[ "$program_drops" -eq 0 ] && [ "$preview_drops" -eq 0 ] || {
  echo "receiver probe already reports drops" >&2
  exit 1
}

cycle=0
for round in $(seq 1 "$rounds"); do
  for target in a b; do
    cycle=$((cycle + 1))
    prefix=$(printf '%02d-%s' "$cycle" "$target")
    before="$output_dir/$prefix-health-before.json"
    after="$output_dir/$prefix-health-after.json"
    health_snapshot "$before"
    container_snapshot "$output_dir/$prefix-containers-before.txt"

    case "$target" in
      a) flow=$flow_a ;;
      b) flow=$flow_b ;;
    esac
    before_reopens=$(jq -r '.reader_reopens' "$before")
    before_source_reopens=$(jq -r --arg flow "$flow" '.sources[] | select(.flow_id == $flow) | .reader_reopens' "$before")
    before_held=$(jq -r '.held' "$before")
    before_no_common=$(jq -r '.no_common_window' "$before")

    printf 'cycle %s/%s: recovering ingress %s\n' "$cycle" "$((rounds * 2))" "$target"
    if ! "$recreate_script" "$target" >"$output_dir/$prefix-recreate.log" 2>&1; then
      cat "$output_dir/$prefix-recreate.log" >&2
      printf '%s\t%s\t%s\tfail-recreate\n' "$(date -u +%FT%TZ)" "$cycle" "$target" >>"$events"
      exit 1
    fi
    sleep "$settle_seconds"

    health_snapshot "$after"
    container_snapshot "$output_dir/$prefix-containers-after.txt"
    validate_containers

    case "$target" in
      a) target_container=$ingress_a ;;
      b) target_container=$ingress_b ;;
    esac
    target_restarts=$(docker inspect --format '{{.RestartCount}}' "$target_container")

    after_reopens=$(jq -r '.reader_reopens' "$after")
    after_source_reopens=$(jq -r --arg flow "$flow" '.sources[] | select(.flow_id == $flow) | .reader_reopens' "$after")
    [ "$after_reopens" -eq $((before_reopens + 1)) ] || {
      echo "aggregate reader_reopens did not increment exactly once" >&2; exit 1;
    }
    [ "$after_source_reopens" -eq $((before_source_reopens + 1)) ] || {
      echo "source $target reader_reopens did not increment exactly once" >&2; exit 1;
    }

    for field in invalid too_early too_late timeouts sync_timeouts sync_too_early sync_too_late selected_input_regressions; do
      check_unchanged "$field" "$before" "$after"
    done

    last_window=$(jq -r '.last_common_window_width' "$after")
    last_skew=$(jq -r '.last_source_skew_frames' "$after")
    [ "$last_window" -ge "$min_recovered_window" ] || {
      echo "common window recovered to only $last_window frames (minimum: $min_recovered_window)" >&2
      exit 1
    }
    [ "$last_skew" -le "$max_recovered_skew" ] || {
      echo "source skew recovered to $last_skew frames (maximum: $max_recovered_skew)" >&2
      exit 1
    }

    set -- $(probe_totals "$program_probe"); program_rendered=${1:-0}; program_drops=${2:-999}
    set -- $(probe_totals "$preview_probe"); preview_rendered=${1:-0}; preview_drops=${2:-999}
    [ "$program_drops" -eq 0 ] && [ "$preview_drops" -eq 0 ] || {
      echo "receiver probe reports drops" >&2; exit 1;
    }
    [ "$program_rendered" -gt "$previous_program" ] && [ "$preview_rendered" -gt "$previous_preview" ] || {
      echo "receiver probe did not continue rendering" >&2; exit 1;
    }
    previous_program=$program_rendered
    previous_preview=$preview_rendered

    held_delta=$(( $(jq -r '.held' "$after") - before_held ))
    no_common_delta=$(( $(jq -r '.no_common_window' "$after") - before_no_common ))
    reopen_delta=$((after_reopens - before_reopens))
    result=pass
    [ "$target_restarts" -eq 0 ] || result=pass-bounded-retry
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -u +%FT%TZ)" "$cycle" "$target" "$result" "$target_restarts" "$reopen_delta" "$held_delta" "$no_common_delta" \
      "$last_window" "$last_skew" "$program_rendered" "$preview_rendered" >>"$events"
  done
done

health_snapshot "$output_dir/health-final.json"
container_snapshot "$output_dir/containers-final.txt"
printf 'PASS: %s alternating recoveries completed; evidence=%s\n' "$cycle" "$output_dir" | tee "$output_dir/result.txt"
