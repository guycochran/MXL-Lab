# NDI Core ingress pilot

The `ndi-core-ingress` Compose profile runs only the NDI receiver and MXL publisher needed to feed an external MXL Switcher Core. It does not start autoframing, NDI outputs, AES67, PTP, or the Docker-control GUI.

## Isolation requirements

- Use a fresh lab VM or host.
- Keep `MXL_DOMAIN` set to a child directory such as `/dev/shm/mxl/domain_ndi_lab`.
- Never use `/dev/shm/mxl` itself as the pilot domain.
- Use pilot-owned flow UUIDs that do not overlap production or synthetic acceptance flows.
- Keep PTP and NTP settings unchanged for the first ingest test.

## Prepare configuration

```bash
cp env.core-ingress.example env.core-ingress
export CORE_INGRESS_ENV_FILE=env.core-ingress
```

Set `NDI_SRC` to the exact discovered source name or set `NDI_URL` to a sender address. Review the three flow UUIDs before starting.
`MXL_HISTORY_DURATION_NS=1000000000` keeps one second of flow history for the
pilot. That lets the faster synthetic source remain readable while the Core
uses a five-frame input delay to absorb brief NDI scheduling jitter. The pilot
Core also uses a bounded 20 ms read wait.

Create the isolated shared-memory directory on the pilot host:

```bash
sudo install -d -m 0777 /dev/shm/mxl/domain_ndi_lab
```

## Build and start

The official NDI runtime must already be present in `ndi/libndi.so.6` as described in the main README.

```bash
docker compose build ndi-core-ingress
docker compose --profile ndi-core-ingress up -d ndi-core-ingress
docker compose ps ndi-core-ingress
```

The service becomes healthy after Flow A appears in the dedicated domain.

## Monitor a gate

In a second shell:

```bash
chmod +x scripts/monitor-core-ingress.sh
INTERVAL=10 scripts/monitor-core-ingress.sh run-core-ingress-5m
```

The monitor is read-only. It records container state and health, CPU, memory, network traffic, Flow A presence, and repeated `mxl-info` snapshots. Stop it with Ctrl-C after the desired gate duration.

Inspect logs and the exact flow without touching other domains:

```bash
docker compose logs --no-color ndi-core-ingress
docker exec mxl-ndi-core-ingress mxl-info -d /dev/shm/mxl/domain_ndi_lab -l
```

## Connect MXL Switcher Core

Configure one Core input with `FLOW_A` from `env.core-ingress`. Keep a known synthetic source as the second input. Use a Core configuration and control port dedicated to the pilot.

For a two-real-source gate, copy `env.core-ingress-b.example` to
`env.core-ingress-b`, assign the second sender by `NDI_SRC` or `NDI_URL`, and
start both services:

```sh
export CORE_INGRESS_ENV_FILE=env.core-ingress
export CORE_INGRESS_B_ENV_FILE=env.core-ingress-b
docker compose --profile ndi-core-ingress up -d \
  ndi-core-ingress ndi-core-ingress-b
```

The second service publishes its source as `FLOW_A` from its own environment
file. The provided example uses the pilot's second video UUID and a distinct
audio UUID. Set `SRC_FPS` to the Core operating point so `videorate` normalizes
senders whose native cadence differs slightly, such as 30/1 and 30000/1001.
Set `ENABLE_AUDIO=false` for a video-only sender so an absent or unusable audio
pad cannot hold up the video ingress pipeline.

The first five-minute gate passes when the adapter and Core remain alive, the adapter health check stays healthy, the Core reports zero invalid, too-early, too-late, timeout, reader-reopen, and synchronization-timeout counters, and RSS stabilizes after startup.

Next, perform repeated TAKE operations, stop and restore the NDI sender, and verify that the Core keeps its output flow identities while attributing source loss upstream. Run a 30-minute gate before the eight-hour NDI acceptance test.

For a controlled container-recovery test, use the guarded helper rather than a
bare `docker compose up`. The helper always reapplies the correct untracked
environment file, refuses a blank source or a shared parent domain, interrupts
only the selected ingress, and waits for both container health and Flow A:

```sh
scripts/recreate-core-ingress.sh a
scripts/recreate-core-ingress.sh b
```

`OUTAGE_SECONDS` defaults to 3 and `RECOVERY_TIMEOUT` defaults to 90. A bare
recreation without `CORE_INGRESS_ENV_FILE` or `CORE_INGRESS_B_ENV_FILE` falls
back to the blank example configuration and must not be used as a recovery
test.

After both Program and Preview have sustained receiver probes, exercise
repeated alternating recovery with the acceptance gate:

```sh
ROUNDS=5 scripts/run-core-ingress-recovery-gate.sh run-recovery-gate
```

Each round recreates A and then B; the two inputs are never interrupted
together. The gate captures health and container state around every recovery
and requires exactly one Core reader reopen for the selected flow, continuous
zero-drop receiver progress, unchanged validity/timing/synchronization error
counters, healthy permanent services, and bounded recovered alignment. The
defaults require at least a 24-frame common window, at most 4 frames of source
skew, and at most one NDI ingest startup retry. Override those bounds only when
the operating format and buffer history justify different values.

For a spaced soak rather than a rapid recovery sequence, set
`RECOVERY_INTERVAL_SECONDS`. Resource snapshots for the Core, both ingresses,
and both outputs are saved before and after each recovery:

```sh
ROUNDS=5 RECOVERY_INTERVAL_SECONDS=180 \
  scripts/run-core-ingress-recovery-gate.sh run-recovery-soak
```

### Validated dual-NIC recovery result

A loaded two-source pilot completed this 10-event spaced gate after moving the
sender on the second subnet from a routed path to a dedicated Layer-2 NIC. All
five recoveries of each ingress passed on the first startup attempt. Each event
produced exactly one aggregate and one per-source reader reopen; Program and
Preview receivers progressed continuously with zero reported drops.

Across the run, validity, early/late, read-timeout, synchronization-timeout,
and selected-input-regression counters remained at zero. The recovered common
window was 27-28 frames and recovered source skew was 0-1 frame. The 10
intentional interruptions added 2,722 held grains and 1,063 no-common-window
observations while output continued. Core RSS remained 320.1-320.3 MiB; ingress
RSS remained 185.8-194.6 MiB and 183.2-187.4 MiB; the two NDI outputs remained
26.9-28.0 MiB and 37.2-37.3 MiB. All permanent containers finished healthy
with zero Docker restarts.

This result supports using a directly attached interface for a sender on a
separate media subnet. Verify the live socket's source address and kernel route
before accepting recovery evidence: an established connection can otherwise
retain its old routed source address after a new interface is added.

## Publish Program and Preview as NDI

The `ndi-core-egress` profile reads the Core's stable Program and Preview MXL
flows and advertises them as ordinary NDI sources. Copy the example, confirm the
domain and flow IDs match the Core, and start the two output adapters:

```bash
cp env.core-egress.example env.core-egress
sudo env CORE_EGRESS_ENV_FILE=env.core-egress \
  docker compose --profile ndi-core-egress up -d \
  ndi-core-program-out ndi-core-preview-out
```

Receivers should discover `MXL Switcher Program` and `MXL Switcher Preview`.
Program is the on-air result; Preview is the source queued for the next TAKE.
Both outputs are video-only in this pilot.

The output wrappers watch the MXL flow-directory inode. If Core restarts and
recreates Program or Preview under the same stable UUID, each wrapper stops its
stale GStreamer process and reopens the replacement flow automatically. The
Compose health checks require both the configured flow directory and a live
GStreamer publisher, so a container cannot report healthy while attached to an
obsolete shared-memory object.

```bash
sudo docker compose ps ndi-core-program-out ndi-core-preview-out
sudo docker compose logs --no-color \
  ndi-core-program-out ndi-core-preview-out
```

## Stop

```bash
docker compose --profile ndi-core-ingress stop ndi-core-ingress
docker compose --profile ndi-core-egress stop \
  ndi-core-program-out ndi-core-preview-out
```

Stopping the profile does not remove its MXL flow directories. Remove only the three UUID directories named in `env.core-ingress` when a clean restart is required.
