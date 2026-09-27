#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
PTP function.

Runs ptp4l from the settings in /config/ptp.json (written by the GUI), and
phc2sys as well when hardware timestamping is used. Because this container
shares the VM's kernel, ptp4l steers the VM's own system clock, which is the
clock MXL (CLOCK_TAI) and the AES67 sender run on.

It also keeps the kernel TAI offset in line with the UTC offset ptp4l applies,
so that CLOCK_TAI reads PTP time. Without that, MXL timestamps and AES67 RTP
timestamps would sit 37 seconds away from every other PTP device.

With PTP set to off it does nothing and waits.
"""
import os
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/opt/scripts")
import ptp_common as pc  # noqa: E402

procs = []
stopping = False


def log(msg):
    print(f"ptp: {msg}", flush=True)


def stop(*_):
    global stopping
    stopping = True
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def idle(reason):
    log(reason)
    while True:
        time.sleep(3600)


def main():
    cfg = pc.ptp_settings()
    if cfg["mode"] == "off":
        idle("PTP is off. Waiting until it is turned on in the GUI.")

    iface = pc.resolve_iface(cfg["iface"])
    if not iface:
        log("no network interface found. Set one in the GUI under PTP.")
        time.sleep(30)
        sys.exit(1)

    caps = pc.timestamping_caps(iface)
    ts = cfg["timestamping"]
    if ts == "hardware" and not caps["hardware"]:
        log(f"{iface} has no hardware timestamping, using software timestamping instead")
        ts = "software"
    if ts == "software" and not caps["software"]:
        log(f"warning: ethtool does not report software timestamping on {iface}. ptp4l may fail.")
    cfg["timestamping"] = ts

    os.makedirs(pc.PTP_RUN, exist_ok=True)
    try:
        os.unlink(pc.PTP_UDS)   # stale socket from a previous run
    except OSError:
        pass
    with open(pc.PTP_CONF, "w") as f:
        f.write(pc.render_ptp4l_conf(cfg, iface))

    role = {"follow": "follow only", "auto": "can be grandmaster"}[cfg["mode"]]
    log(f"{iface}, domain {cfg['domain']}, {role}, {ts} timestamping, "
        f"sync 2^{cfg['log_sync']} s, announce 2^{cfg['log_announce']} s, "
        f"{cfg['delay_mechanism']}, DSCP {cfg['dscp_event']}/{cfg['dscp_general']}")
    if ts == "software":
        log("software timestamping: ptp4l steers the system clock directly. "
            "Expect far larger offsets than a hardware timestamping NIC.")

    # Real-time priority so software timestamps are taken promptly on a busy VM
    rt = ["chrt", "-f", "60"] if shutil.which("chrt") else []
    procs.append(subprocess.Popen(rt + ["ptp4l", "-f", pc.PTP_CONF, "-i", iface, "-m"]))

    # Wait for ptp4l's management socket
    for _ in range(50):
        if os.path.exists(pc.PTP_UDS) or procs[0].poll() is not None:
            break
        time.sleep(0.2)

    if ts == "hardware":
        # Automatic mode follows ptp4l's port state: the system clock follows
        # the NIC clock when following, and the NIC clock follows the system
        # clock when this VM is grandmaster (-rr).
        procs.append(subprocess.Popen(rt + ["phc2sys", "-a", "-rr", "-m", "-z", pc.PTP_UDS,
                                       "-n", str(int(cfg["domain"]))]))

    gm_set = cfg["mode"] != "auto"
    last_note = ""
    while not stopping:
        for p in procs:
            if p.poll() is not None:
                log(f"{p.args[0]} exited with code {p.returncode}")
                stop()
        if not gm_set:
            err = pc.set_gm_timescale(cfg["domain"])
            if err is None:
                log("grandmaster settings: PTP timescale, UTC offset 37 s, internal oscillator")
                gm_set = True

        data, err = pc.pmc_query(cfg["domain"], ids=("PORT_DATA_SET", "TIME_PROPERTIES_DATA_SET",
                                                     "PARENT_DATA_SET", "DEFAULT_DATA_SET"))
        if not err:
            s = pc.summarise(data)
            # UNCALIBRATED too: the UTC offset is known from the first Announce,
            # and until it is set every RTP timestamp is 37 s away from PTP time.
            if s["port_state"] in ("SLAVE", "UNCALIBRATED", "MASTER", "GRAND_MASTER"):
                want = pc.expected_tai_offset(s)
                have = pc.kernel_tai_offset()
                if have != want:
                    fail = pc.set_kernel_tai_offset(want)
                    note = (f"kernel TAI offset set to {want} s (was {have})" if fail is None
                            else f"could not set the kernel TAI offset: {fail}")
                    if note != last_note:
                        log(note)
                        last_note = note
        time.sleep(5)


if __name__ == "__main__":
    main()
