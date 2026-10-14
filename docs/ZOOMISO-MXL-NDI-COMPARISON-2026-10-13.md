# ZoomISO Cloud MXL versus NDI comparison — 2026-10-13

This checkpoint compares two co-located 1920x1080p30 paths from the same
ZoomISO Cloud participant output on a CPU-only host:

1. ZoomISO publishes v210 video directly into an MXL shared-memory domain.
2. ZoomISO publishes NDI, which is received, decoded, converted to v210, and
   republished into the same MXL domain.

The meeting, participant identity, host addresses, generated flow UUIDs, and
raw operational evidence are intentionally not published.

## Method

All controls used the same active participant, requested format, host, ZoomISO
instance, and observation tooling. Receiver probes measured delivered grain
cadence and drops. A concurrent uniqueness detector measured whether successive
frames contained changing picture content rather than merely arriving at the
advertised cadence.

Three stages were run:

- 10 minutes with one native MXL output and one NDI output active together,
  routed through a two-input MXL Core instance.
- 5 minutes with only the native MXL output active.
- 5 minutes with only the NDI output active, including its required NDI-to-MXL
  receive/decode/convert adapter.

Each stage required all monitored containers to remain running with zero
restarts, receiver drops to remain zero, and content to remain live. The
isolated stages completed with zero monitor errors.

## Isolated-path result

| Path | ZoomISO average CPU | Adapter average CPU | Combined average CPU | Average unique picture rate |
| --- | ---: | ---: | ---: | ---: |
| Native MXL | 165.74% | none | 165.74% | 29.90 fps |
| NDI through MXL adapter | 212.65% | 148.35% | 361.00% | 27.45 fps |

CPU percentages use the container convention where 100% is one logical CPU.
For this workload, the complete NDI path consumed approximately 2.18 times the
average CPU of the native MXL path.

Measured ranges:

| Measurement | Native MXL | NDI path |
| --- | ---: | ---: |
| ZoomISO CPU | 147.07-197.50% | 172.05-235.65% |
| ZoomISO RSS | 1.680-1.687 GiB | 1.763-1.772 GiB |
| Adapter CPU | n/a | 127.35-169.56% |
| Adapter RSS | n/a | 105.20-107.50 MiB |
| Delivered grain cadence | 29.7-30.3 fps | 29.7-30.3 fps |
| Unique picture cadence | 29.0-30.3 fps | 14.7-30.0 fps |
| Receiver drops | 0 | 0 |

The NDI transport continued delivering grains at approximately 30 fps, but
some observation windows contained repeated pictures. The native MXL path
remained in the detector's healthy state for every isolated observation.

## Simultaneous-path result

Both paths negotiated 1920x1080p30 and completed the 10-minute simultaneous
run without probe drops, restarts, timeouts, reader reopenings, selected-input
regressions, no-common-window errors, sustained-unavailable alarms, or monitor
errors. Core remained running throughout.

Resource ranges while both paths and Core were active:

| Service | CPU | RSS |
| --- | ---: | ---: |
| ZoomISO | 288.25-428.75% | 1.691-1.709 GiB |
| NDI receive/decode/convert adapter | 171.85-226.07% | 110.80-127.40 MiB |
| MXL Core | 39.11-60.73% | 76.88-78.14 MiB |

The native path produced 24.3-30.3 unique pictures per second. The NDI-return
path produced 17.3-30.3 unique pictures per second and periodically entered the
detector's degraded state while continuing to deliver approximately 30 grains
per second. Twelve late-read events were observed across both inputs during the
simultaneous run; they did not produce a timeout, active alarm, output stall, or
receiver drop.

## Interpretation

This comparison demonstrates the practical advantage of a codec-free,
shared-memory path on a CPU-constrained production host:

- Native MXL avoids the NDI encode/decode and pixel-conversion work.
- The measured end-to-end MXL path required less than half the average CPU of
  the equivalent NDI-return path.
- MXL preserved a steadier changing-picture cadence even though both transports
  reported nominal 30 fps delivery.
- Core's switching/output work was small relative to the NDI conversion cost.

This result is specific to the tested host, software versions, format, and
co-located topology. It does not claim that NDI is universally unsuitable; it
quantifies the cost of using NDI where a local MXL shared-memory connection is
available.

## Next control

Repeat the comparison with two active MXL outputs and two matched NDI outputs.
Use identical participant assignments and formats for both pairs, run isolated
two-output controls before the simultaneous four-output stage, and preserve the
same cadence, uniqueness, resource, restart, and drop checks.
