#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
PTP and network helpers shared by the ptp runner, the AES67 sender and the GUI.

- PTP settings with profile presets (Dante/AES67 media profile, SMPTE ST 2059-2)
- ptp4l.conf generation
- pmc queries against the running ptp4l, parsed into plain dicts
- reading and setting the kernel TAI offset, so CLOCK_TAI matches PTP time
- interface discovery: default route, IPv4 address, MAC, timestamping support

Standard library only, so every container can import it.
"""
import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import socket
import struct
import subprocess
import uuid

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
PTP_CFG = os.path.join(CONFIG_DIR, "ptp.json")
AUDIO_CFG = os.path.join(CONFIG_DIR, "audio.json")
PTP_RUN = os.environ.get("PTP_RUN", "/run/ptp")
PTP_UDS = os.path.join(PTP_RUN, "ptp4l")
PTP_CONF = os.path.join(PTP_RUN, "ptp4l.conf")

PTP_MODES = ("off", "follow", "auto")
PTP_DEFAULTS = {
    "mode": "off",              # off | follow (never grandmaster) | auto (BMCA decides)
    "profile": "dante",         # dante | st2059 | custom
    "iface": "",                # blank means the interface with the default route
    "timestamping": "software",  # software | hardware
    "domain": 0,
    "priority1": 250,
    "priority2": 250,
    "log_sync": -3,
    "log_announce": 1,
    "log_delay_req": 0,
    "announce_timeout": 3,
    "delay_mechanism": "E2E",
    "dscp_event": 46,
    "dscp_general": 46,
}

# Values each profile sets. Anything not listed keeps its current value.
PTP_PROFILES = {
    "dante": {   # AES67 media profile, what Dante uses in AES67 mode
        "domain": 0, "log_sync": -3, "log_announce": 1, "log_delay_req": 0,
        "announce_timeout": 3, "delay_mechanism": "E2E",
        # Dante leaders announce priority1 249. 250 lets them win, and this
        # VM only takes over when there is no Dante leader.
        "priority1": 250, "priority2": 250,
    },
    "st2059": {  # SMPTE ST 2059-2 default profile
        "domain": 127, "log_sync": -3, "log_announce": -2, "log_delay_req": -3,
        "announce_timeout": 3, "delay_mechanism": "E2E",
        "priority1": 128, "priority2": 128,
    },
}

# ptp4l's own default when a grandmaster does not announce a usable offset
DEFAULT_UTC_OFFSET = 37

AUDIO_DEFAULTS = {
    "enabled": False,
    "channels": 2,
    "encoding": "L24",           # L24 | L16
    "ptime_us": 1000,            # 1000 (AES67, Dante, 2110-30 level A) or 125 (2110-30 level C)
    "address": "239.69.10.1",
    "port": 5004,
    "ttl": 16,
    "payload_type": 96,
    "dscp": 34,                  # AF41, the AES67 recommendation for media
    "iface": "",
    "send_delay_ms": 1.0,
    # How far behind MXL time the sender reads. Samples are committed to the
    # MXL audio flow some time after their timestamp (ingest latency, roughly
    # one NDI audio chunk). RTP timestamps and send times are both moved
    # forward by this much, so packets leave just after their timestamp.
    "read_delay_ms": 60.0,
    "session_name": "MXL Lab Audio",
    "sap": True,
    "source": "flow",            # flow (MXL audio flow) | tone (1 kHz test tone)
}


# ------------------------------------------------------------------ files
def load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def ptp_settings():
    cfg = dict(PTP_DEFAULTS)
    cfg.update({k: v for k, v in load_json(PTP_CFG).items() if k in PTP_DEFAULTS})
    if cfg["mode"] not in PTP_MODES:
        cfg["mode"] = "off"
    return cfg


def audio_settings():
    cfg = dict(AUDIO_DEFAULTS)
    cfg.update({k: v for k, v in load_json(AUDIO_CFG).items() if k in AUDIO_DEFAULTS})
    return cfg


# -------------------------------------------------------------- interfaces
SKIP_IFACE = re.compile(r"^(lo|docker\d*|veth.*|br-.*|virbr.*|ifb\d*|tap.*|tun.*)$")


def default_iface():
    """Interface that carries the default route, from /proc/net/route."""
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                    return parts[0]
    except (OSError, StopIteration, ValueError):
        pass
    return ""


def iface_ipv4(iface):
    if not iface:
        return ""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(sock.fileno(), 0x8915,  # SIOCGIFADDR
                             struct.pack("256s", iface[:15].encode()))
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return ""
    finally:
        sock.close()


def iface_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except OSError:
        return ""


def timestamping_caps(iface):
    """What `ethtool -T` reports: software and hardware timestamping, and the PHC index."""
    caps = {"software": False, "hardware": False, "phc": None}
    try:
        out = subprocess.run(["ethtool", "-T", iface], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return caps
    caps["software"] = "software-transmit" in out and "software-receive" in out
    caps["hardware"] = "hardware-transmit" in out and "hardware-receive" in out
    m = re.search(r"PTP Hardware Clock:\s*(\d+)", out)
    if m:
        caps["phc"] = int(m.group(1))
    else:
        caps["hardware"] = False
    return caps


def list_ifaces():
    out = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        names = []
    for name in names:
        if SKIP_IFACE.match(name):
            continue
        out.append({"name": name, "ipv4": iface_ipv4(name), "mac": iface_mac(name),
                    "ts": timestamping_caps(name)})
    return out


def resolve_iface(name):
    return name or default_iface()


# ----------------------------------------------------------- ptp4l.conf
def render_ptp4l_conf(cfg, iface):
    follow = cfg["mode"] == "follow"
    # The best master election compares priority1 first. A follower with 128
    # rates itself above a leader announcing 249 (as Dante does), and being
    # client only it then refuses both roles and stays in LISTENING.
    # 255 is the worst possible value, so any leader wins.
    prio1 = 255 if follow else int(cfg["priority1"])
    prio2 = 255 if follow else int(cfg["priority2"])
    lines = [
        "# Written by the MXL lab from config/ptp.json. Edit in the GUI, not here.",
        "[global]",
        f"domainNumber {int(cfg['domain'])}",
        f"priority1 {prio1}",
        f"priority2 {prio2}",
        f"clientOnly {1 if follow else 0}",
        f"logAnnounceInterval {int(cfg['log_announce'])}",
        f"logSyncInterval {int(cfg['log_sync'])}",
        f"logMinDelayReqInterval {int(cfg['log_delay_req'])}",
        f"announceReceiptTimeout {int(cfg['announce_timeout'])}",
        f"delay_mechanism {cfg['delay_mechanism']}",
        "network_transport UDPv4",
        f"time_stamping {cfg['timestamping']}",
        f"dscp_event {int(cfg['dscp_event'])}",
        f"dscp_general {int(cfg['dscp_general'])}",
        # virtio and other virtual NICs can be slow to hand back software
        # transmit timestamps. The 1 ms default causes constant faults there.
        "tx_timestamp_timeout 50",
        # ptp4l only steps the clock on first lock by default and slews after
        # that. If the grandmaster's time jumps (a Dante leader reboots, or a
        # different device takes over), the gap can be hours or days and
        # slewing never closes it. Step whenever the offset passes 1 s. Normal
        # jitter, even with software timestamping, is far below that.
        "step_threshold 1.0",
        # linuxptp 4 marks its messages as PTP 2.1. Some devices, including
        # switches acting as boundary clocks, ignore 2.1 Delay_Req messages and
        # never answer, so the port stays in UNCALIBRATED. 2.0 is accepted by
        # every PTPv2 device, Dante included.
        "ptp_minor_version 0",
        f"uds_address {PTP_UDS}",
        "summary_interval 2",
        "logging_level 6",
        "verbose 1",
        "use_syslog 0",
        "",
        f"[{iface}]",
        "",
    ]
    return "\n".join(lines)


# -------------------------------------------------------------------- pmc
PMC_IDS = ("PORT_DATA_SET", "CURRENT_DATA_SET", "PARENT_DATA_SET",
           "TIME_STATUS_NP", "TIME_PROPERTIES_DATA_SET", "DEFAULT_DATA_SET")
_RESP = re.compile(r"RESPONSE MANAGEMENT (\S+)")


def parse_pmc(text):
    """pmc output into {DATA_SET: {key: value}}. Values stay strings."""
    out, current = {}, None
    for line in text.splitlines():
        m = _RESP.search(line)
        if m:
            current = out.setdefault(m.group(1), {})
            continue
        if current is None or not line.startswith("\t\t"):
            continue
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            current[parts[0]] = parts[1].strip()
        elif len(parts) == 1:
            current[parts[0]] = ""
    return out


def pmc_cmd(domain):
    """
    pmc talks to ptp4l over UNIX datagram sockets, and ptp4l replies to the
    path pmc bound. By default that is /var/run/pmc.<pid> inside the calling
    container, which ptp4l in the ptp container cannot see, so replies are
    lost. Bind the reply socket on the shared /run/ptp volume instead.
    """
    client = os.path.join(PTP_RUN, f"pmc.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    return ["pmc", "-u", "-b", "0", "-d", str(int(domain)), "-s", PTP_UDS, "-i", client], client


def run_pmc(domain, args, timeout=3):
    cmd, client = pmc_cmd(domain)
    try:
        return subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout)
    finally:
        try:
            os.unlink(client)
        except OSError:
            pass


def pmc_query(domain, ids=PMC_IDS, timeout=3):
    """Ask the local ptp4l over its UNIX socket. Returns (parsed dict, error string)."""
    if not os.path.exists(PTP_UDS):
        return {}, "ptp4l is not running"
    try:
        res = run_pmc(domain, [f"GET {i}" for i in ids], timeout)
    except FileNotFoundError:
        return {}, "pmc not found, rebuild the lab image"
    except subprocess.TimeoutExpired:
        return {}, "ptp4l did not answer"
    data = parse_pmc(res.stdout)
    if not data:
        tail = (res.stderr or res.stdout).strip().splitlines()[-1:]
        return {}, "no answer from ptp4l" + (": " + tail[0] if tail else "")
    return data, None


def set_gm_timescale(domain, utc_offset=DEFAULT_UTC_OFFSET):
    """
    ptp4l starts as a grandmaster announcing an arbitrary timescale. Dante and
    ST 2059 receivers expect the PTP (TAI) timescale, so announce that, with the
    UTC offset, from an internal oscillator (0xa0).
    """
    cmd = ("SET GRANDMASTER_SETTINGS_NP clockClass 248 clockAccuracy 0xfe "
           "offsetScaledLogVariance 0xffff "
           f"currentUtcOffset {int(utc_offset)} leap61 0 leap59 0 "
           "currentUtcOffsetValid 1 ptpTimescale 1 timeTraceable 0 "
           "frequencyTraceable 0 timeSource 0xa0")
    try:
        res = run_pmc(domain, [cmd])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    got = parse_pmc(res.stdout).get("GRANDMASTER_SETTINGS_NP", {})
    return None if got.get("ptpTimescale") == "1" else "ptp4l did not accept the grandmaster settings"


OFFSET_LIMIT_NS = 140737488355327


def to_num(value, kind=float):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def sdp_clock_id(identity):
    """001122.fffe.334455 (linuxptp) to 00-11-22-FF-FE-33-44-55 (RFC 7273)."""
    hexed = re.sub(r"[^0-9a-fA-F]", "", identity or "")
    if len(hexed) != 16:
        return ""
    return "-".join(hexed[i:i + 2] for i in range(0, 16, 2)).upper()


def summarise(data):
    """The fields the GUI and SDP need, as numbers where they are numbers."""
    port = data.get("PORT_DATA_SET", {})
    cur = data.get("CURRENT_DATA_SET", {})
    par = data.get("PARENT_DATA_SET", {})
    tsn = data.get("TIME_STATUS_NP", {})
    tp = data.get("TIME_PROPERTIES_DATA_SET", {})
    dds = data.get("DEFAULT_DATA_SET", {})
    own = dds.get("clockIdentity", "")
    gm = par.get("grandmasterIdentity", "") or tsn.get("gmIdentity", "")
    # offsetFromMaster tops out at 2^47 ns (about 39 hours). TIME_STATUS_NP's
    # master_offset is a plain 64-bit value, so use it when the other is pinned.
    offset = to_num(cur.get("offsetFromMaster"))
    raw = to_num(tsn.get("master_offset"), int)
    if offset is not None and abs(offset) >= OFFSET_LIMIT_NS and raw:
        offset = float(raw)
    ingress = to_num(tsn.get("ingress_time"), int) or 0
    # The grandmaster's own time, from the last Sync: our receive time minus the offset
    gm_time = (ingress - raw) / 1e9 if ingress and raw is not None else None
    return {
        "port_state": port.get("portState", ""),
        "offset_ns": offset,
        "gm_time": gm_time,
        # 0 until ptp4l has processed a Sync from the grandmaster
        "last_sync_ingress": to_num(tsn.get("ingress_time"), int),
        "path_delay_ns": to_num(cur.get("meanPathDelay")),
        "steps_removed": to_num(cur.get("stepsRemoved"), int),
        "gm_identity": gm,
        "gm_sdp": sdp_clock_id(gm),
        "gm_is_self": bool(own) and own == gm,
        "gm_present": tsn.get("gmPresent", "") == "true",
        "gm_priority1": to_num(par.get("grandmasterPriority1"), int),
        "gm_priority2": to_num(par.get("grandmasterPriority2"), int),
        "gm_class": to_num(par.get("gm.ClockClass"), int),
        "clock_identity": own,
        "utc_offset": to_num(tp.get("currentUtcOffset"), int),
        "utc_offset_valid": tp.get("currentUtcOffsetValid", "") == "1",
        "ptp_timescale": tp.get("ptpTimescale", "1") == "1",
        "time_traceable": tp.get("timeTraceable", "") == "1",
    }


def expected_tai_offset(summary):
    """
    The UTC offset ptp4l applies when it steers CLOCK_REALTIME, so the kernel
    TAI offset can be set to match and CLOCK_TAI then reads PTP time.
    Mirrors ptp4l's clock_utc_correct().
    """
    if not summary.get("ptp_timescale", True):
        return 0
    announced = summary.get("utc_offset")
    if announced is not None and summary.get("utc_offset_valid") and summary.get("time_traceable"):
        return announced
    if announced is not None and announced > DEFAULT_UTC_OFFSET:
        return announced
    return DEFAULT_UTC_OFFSET


# --------------------------------------------------------- kernel TAI offset
class Timex(ctypes.Structure):
    _fields_ = [
        ("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
        ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long), ("time_usec", ctypes.c_long), ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long), ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long), ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


ADJ_TAI = 0x0080
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def kernel_tai_offset():
    tx = Timex()
    if _libc.adjtimex(ctypes.byref(tx)) < 0:
        return None
    return tx.tai


def set_kernel_tai_offset(seconds):
    """Needs CAP_SYS_TIME. Returns None on success, or the error text."""
    tx = Timex()
    tx.modes = ADJ_TAI
    tx.constant = int(seconds)
    if _libc.adjtimex(ctypes.byref(tx)) < 0:
        return os.strerror(ctypes.get_errno())
    return None


if __name__ == "__main__":
    print(json.dumps({"default_iface": default_iface(), "ifaces": list_ifaces(),
                      "kernel_tai": kernel_tai_offset()}, indent=2))
