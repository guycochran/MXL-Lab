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

## Conclusion

The two-real-source steady-state path and isolated source-flow replacement path
both pass. The next acceptance step is a longer loaded run that includes
periodic guarded recovery of each ingress, never both at once.

