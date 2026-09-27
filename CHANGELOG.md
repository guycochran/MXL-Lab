# Changelog

All notable changes to this fork. Versions follow [semantic versioning](https://semver.org/).

## 3.0.0

Adds audio, PTP and a reworked GUI to the original lab ([mykeman/MXL-Lab](https://github.com/mykeman/MXL-Lab)).

### Added

- **MXL audio flow.** ndi-ingest writes the NDI source's audio to an MXL flow: 48 kHz, 1, 2, 4 or 8 channels, mapped one to one.
- **AES67 output** (aes67-out). Sends the audio flow as AES67 or ST 2110-30 multicast, L24 or L16, 1 ms or 125 us packets, with an SDP file and SAP announcements for Dante Controller. RTP timestamps are taken from `CLOCK_TAI`, so they are PTP time. A read delay and a send delay control when packets leave relative to their timestamps. Includes a 1 kHz test tone source and level meters.
- **PTP function** (ptp). Runs ptp4l, and phc2sys with hardware timestamping, from settings made in the GUI: off, follow only, or able to become grandmaster; Dante (AES67 media profile), ST 2059-2 or custom settings. Keeps the kernel TAI offset in line with the grandmaster so that `CLOCK_TAI` reads PTP time.
- **Tabbed GUI**: Status, NDI, 9:16 framing, and AES67 and clock. The Status tab lists anything that needs attention, worst first, with links to the tab that fixes it.
- Live PTP offset graph, grandmaster details and grandmaster time; an SDP download; audio meters at about 20 readings a second.
- Optional GUI login (`GUI_USER`, `GUI_PASSWORD`) and bind address (`GUI_BIND`).
- `scripts/pin-from-image.sh`, to pin the Dockerfile to the versions in a working image.
- Ruff and shellcheck configuration.

### Changed

- The Dockerfile pins every upstream source to an exact version, and checks the face model's SHA-256.
- The GUI backend is split into a `lab` package, and the page into `index.html`, `app.js` and `app.css`.
- `setup.sh` offers to turn off NTP (needed for PTP), and opens the PTP and SAP ports when ufw is active.
- Apply jobs, and VM boots with PTP on, wait for PTP to settle before starting the media functions.

### Fixed

- Text from the network (NDI source names and addresses) and from the server is escaped before it is put into the page.
- The lab recovers when the grandmaster's time jumps: ptp4l steps the clock when the offset passes 1 s, and the GUI detects any clock step and restarts the media functions in order.
- PTP headers are sent as version 2.0, which every PTPv2 device answers.
- In follow-only mode, ptp4l's priorities are set to 255 so that any grandmaster wins.

## 1.0.0

The original lab: NDI in, MXL flows, 9:16 face-following crop, NDI out, web GUI and install script. See [mykeman/MXL-Lab](https://github.com/mykeman/MXL-Lab).
