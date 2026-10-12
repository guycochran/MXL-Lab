# Dual-NDI steady-state and recovery acceptance — 2026-10-12

This pilot used two independent real NDI senders, two isolated NDI-to-MXL
ingresses, MXL Switcher Core, stable Program and Preview NDI outputs, and two
sustained NDI receiver probes. All media lived in the dedicated
`/dev/shm/mxl/domain_ndi_lab` domain.

## Thirty-minute loaded gate

- Result: pass
- Samples: 140
- TAKE operations: 30
- Final revision: 35
- Routed source grains: 141,394 total; 70,697 per source
- Held output grains: 12
- Common-window width: 27–28 frames
- Source skew: 0–1 frame
- Selected-input repeats: 6
- Selected-input skip events / skipped grains: 5 / 5
- Invalid, early, late, timeout, reader-reopen, sync-timeout, sync-early,
  sync-late, no-common-window, and selected-input-regression counters: 0
- Monitoring errors and gate failures: 0
- All seven containers remained running with zero restarts.
- Program and Preview receiver probes remained connected with zero drops.
  The last probe totals captured immediately before recovery testing were
  58,327 and 58,326 rendered frames.

Measured CPU and RSS ranges:

| Service | CPU | RSS |
| --- | ---: | ---: |
| Core | 12.59–34.25% | 319.9–321.2 MiB |
| NDI ingress A | 45.62–69.48% | 183.3–191.6 MiB |
| NDI ingress B | 20.53–34.36% | 187.1–187.5 MiB |
| Program NDI output | 33.20–53.10% | 25.27–25.68 MiB |
| Preview NDI output | 30.39–46.32% | 25.84–26.94 MiB |

## Configuration hazard found

A bare Compose recreation omitted `CORE_INGRESS_ENV_FILE` and therefore used
the blank example file. The recreated container had empty `NDI_SRC` and
`NDI_URL`, could not reacquire a source, and correctly failed health. This was
a deployment-command defect, not an MXL Core or NDI media-path failure.

`scripts/recreate-core-ingress.sh` now guards the operation. It selects the
correct untracked environment file, rejects blank source configuration and a
shared parent domain, interrupts only one ingress, and waits for both container
health and the configured Flow A directory.

## Corrected three-second recovery tests

Each test began from a healthy, synchronized two-source state. Only one ingress
was recreated at a time. Program and Preview remained connected throughout.

| Check | Ingress A | Ingress B |
| --- | ---: | ---: |
| Planned outage | 3 s | 3 s |
| Wait to healthy Flow A | 5 s | 5 s |
| Flow-directory inode | 3223 → 3262 | 2934 → 3297 |
| Reader reopen delta | +1 | +1 |
| Held-grain delta | +266 | +268 |
| No-common-window delta | +105 | +107 |
| Routed-grain delta during observation | +923 | +916 |
| Invalid/timing/sync/regression errors | 0 | 0 |
| Container restart count after recovery | 0 | 0 |
| Program receiver drops | 0 | 0 |
| Preview receiver drops | 0 | 0 |

After each source returned, the Core reopened the replacement inode, resumed a
27–28-frame common window with 0–1-frame source skew, and continued routing both
sources. The final receiver totals were 107,625 Program frames and 107,612
Preview frames, both with zero drops.

## Ten-cycle rolling-recovery gate

The next milestone ran five guarded recoveries of each real NDI ingress,
alternating A/B while sustained receivers kept both NDI outputs under load.
The inputs were never interrupted together.

- Result: pass, 10/10 recoveries
- Planned outage: 3 seconds per recovery
- Healthy Flow A recovery: 5 seconds in every cycle
- Replacement flow inode: changed in all 10 cycles
- Reader reopen delta: exactly +1 for the selected source in every cycle;
  +10 aggregate
- Routed-grain delta: +9,648 aggregate; +4,824 per source
- Held-grain delta: +2,670 total; 262–272 per recovery
- No-common-window delta: +1,062 total; 102–110 per recovery
- Recovered common-window width: 26–27 frames
- Recovered source skew: 1–2 frames
- Invalid, early, late, timeout, sync-timeout, sync-early, sync-late, and
  selected-input-regression deltas: 0
- Ingress startup retries in the final gate: 0
- All seven containers ended running with zero restarts.
- Program receiver: 9,244 → 14,784 frames across recorded cycles, zero drops
- Preview receiver: 7,938 → 13,481 frames across recorded cycles, zero drops

Two exploratory attempts sharpened the gate instead of being discarded. One
Ingress A recreation hit a transient NDI data-stream error 0.22 seconds after
launch, restarted once, and then recovered with uninterrupted zero-drop
outputs. A later recovery stabilized at 2 frames of skew with a still-healthy
26-frame common window. The final gate therefore records bounded ingest retries
and judges alignment by both available common-window depth and skew instead of
assuming that every pair of independent NDI senders will remain within one
frame after reacquisition.

The reusable gate is `scripts/run-core-ingress-recovery-gate.sh`. The raw final
evidence was retained with the private acceptance records rather than
published because it contains operational container and host details.

## Conclusion

The two-real-source steady-state path, isolated source-flow replacement, and
repeated rolling-recovery path all pass. The next acceptance step is a longer
soak with periodic guarded recoveries, followed by deliberate sender/network
fault injection and operational packaging.
