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

The first five-minute gate passes when the adapter and Core remain alive, the adapter health check stays healthy, the Core reports zero invalid, too-early, too-late, timeout, reader-reopen, and synchronization-timeout counters, and RSS stabilizes after startup.

Next, perform repeated TAKE operations, stop and restore the NDI sender, and verify that the Core keeps its output flow identities while attributing source loss upstream. Run a 30-minute gate before the eight-hour NDI acceptance test.

## Stop

```bash
docker compose --profile ndi-core-ingress stop ndi-core-ingress
```

Stopping the profile does not remove its MXL flow directories. Remove only the three UUID directories named in `env.core-ingress` when a clean restart is required.
