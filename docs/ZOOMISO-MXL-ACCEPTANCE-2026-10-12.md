# ZoomISO Cloud to MXL Core acceptance — 2026-10-12

This checkpoint evaluates a co-located, codec-free path from ZoomISO Cloud
into MXL Switcher Core. ZoomISO and the temporary Core ran on the same VM and
exchanged uncompressed video through an MXL shared-memory domain. No NDI
encode, NDI decode, or media-network hop was present between ZoomISO and Core.

The Zoom meeting, account details, host addresses, generated flow UUIDs, and
raw operational evidence are intentionally not published.

## Test topology

```text
Zoom meeting
     |
     v
+-----------------------+
| ZoomISO Cloud         |
| meetingSDKDemo        |
| 8 participant outputs |
+-----------+-----------+
            |
            | MXL shared memory, v210
            |
            +----> direct probes for Outputs 1-8
            |
            | Outputs 1 and 2
            v
+-----------------------+
| MXL Switcher Core     |
| two synchronized      |
| switcher inputs       |
+-----------+-----------+
            |
       +----+----+
       |         |
       v         v
  Program MXL  Preview MXL
       |         |
       v         v
  receiver probe receiver probe
```

The eight direct probes verify ZoomISO's published flows independently of the
Core. Program and Preview probes verify the Core's stable output flows and its
two-input common-window selection.

## Eight-output 720p gate

- Result: pass
- Hold: 1,800 seconds
- Samples: 55
- Format: eight inputs plus Program and Preview at 1280x720p29.97, v210
- All 10 receiver probes negotiated the expected capability.
- Receiver drops: 0
- ZoomISO restarts: 0
- Temporary container failures: 0
- Final routed source grains: 116,144 total; 58,072 per selected source
- Final Core frames: 58,157
- Held grains: 170
- Common-window width: 3-4 frames
- Source skew: 0-1 frame
- Invalid, early, late, timeout, reader-reopen, synchronization-timeout,
  synchronization-early/late, no-common-window, and selected-input-regression
  counters: 0

Measured resource ranges:

| Service | CPU | RSS |
| --- | ---: | ---: |
| ZoomISO | 483.62-1262.31% | 1.544-1.938 GiB |
| Temporary MXL Core | 17.60-35.85% | 30.12-31.12 MiB |

The host completed this gate with 16 vCPUs assigned to the ZoomISO VM. The
temporary Core and all probes were removed after evidence capture; ZoomISO
and all eight saved MXL output configurations remained intact.

## Eight-output 1080p gate

Before the 1080p attempt, the ZoomISO VM was increased to 20 vCPUs and
unrelated lab VMs were stopped. All eight ZoomISO outputs were configured for
1920x1080p29.97 MXL. The shared-memory flow definitions confirmed that ZoomISO
published true 1920x1080 v210 frames even though the Zoom dashboard reported
some incoming participant streams at 640x360. The configured output format
therefore does not imply that Zoom supplied matching source detail.

- Result: fail-fast during the second monitored sample
- Capability: all eight inputs plus Program and Preview negotiated
  1920x1080p29.97 v210
- Direct receiver cadence: approximately 29.97 fps
- Direct receiver drops: 0 on all eight ZoomISO outputs
- Program/Preview receiver drops: 0
- ZoomISO CPU samples: 1496.68% and 994.38% of 2000% available
- ZoomISO RSS: 1.455-1.503 GiB
- Temporary Core CPU: 33.17-36.79%
- Temporary Core RSS: 64.98 MiB
- ZoomISO restarts: 0
- Selected-input regressions, read timeouts, reader reopens, and sync
  timeouts: 0

Core counter changes from the validated baseline to fail-fast teardown:

| Counter | Baseline | Final | Delta |
| --- | ---: | ---: | ---: |
| Invalid selections | 20 | 121 | +101 |
| Too late | 2 | 5 | +3 |
| No common window | 9 | 58 | +49 |
| Selected-input regressions | 0 | 0 | 0 |

The harness stopped immediately, captured evidence, and removed only the
temporary Core and receiver containers. ZoomISO and its meeting process
remained running with zero restarts.

## Interpretation

The 1080p failure was not sustained VM CPU saturation: ZoomISO still had
several assigned vCPUs available during both samples, memory remained stable,
and all eight direct probes received 1080p video without drops. The failure
appeared in the Core's two-source common-window alignment as the independent
1080p flows lost sufficient overlap.

This distinguishes three separate facts:

1. ZoomISO can publish eight simultaneous 1080p29.97 MXL video flows on this
   host.
2. Direct MXL consumers can receive those flows without reported drops.
3. The tested Core configuration cannot yet accept the two selected flows as
   a clean synchronized switching pair while all eight 1080p outputs are
   active.

## Next acceptance step

Reduce ZoomISO to four actual 1080p outputs, rediscover the live MXL flow IDs,
and repeat the same loaded gate. Merely removing receiver probes is not a
valid reduction because ZoomISO would continue producing all eight flows. If
the four-output gate passes, extend it to 30 minutes before testing higher
output counts or changing Core history/common-window policy.

