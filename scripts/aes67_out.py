#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
AES67 output function.

Reads the MXL audio flow and sends it as an AES67 (or ST 2110-30) RTP
multicast stream, with an SDP file and optional SAP announcements so Dante
Controller can find it.

Timing: the pipeline runs on the system clock in TAI mode with a base time
of zero, so running time equals TAI time. The RTP timestamp of each packet is
then its sample time in TAI at 48 kHz, which is what AES67 and ST 2110 expect
(a=mediaclk:direct=0 against the PTP epoch). This is only as good as the VM's
clock: with PTP running (the ptp function) CLOCK_TAI follows the grandmaster.

Settings come from /config/audio.json and /config/ptp.json. Live status
(levels, destination, SDP) goes to /preview/audio-status.json for the GUI.
"""
import os
import signal
import socket
import sys
import threading
import time
import zlib

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

sys.path.insert(0, "/opt/scripts")
import ptp_common as pc  # noqa: E402

DOMAIN = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl/domain_ndi_lab")
FLOW = os.environ.get("FLOW_AUDIO", "7fbec3b1-1b0f-417d-9059-8b94a47197ef")
STATUS_OUT = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "audio-status.json")
LEVELS_OUT = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "audio-levels.json")
LEVEL_INTERVAL_MS = 50
# AES67 and ST 2110-30 media clock. The MXL audio flow is written at this rate too.
SAMPLE_RATE = 48000


def normal_priority():
    """
    Drop the calling thread out of real-time scheduling. Status and level
    writing must never compete with the thread that sends the packets.
    On Linux, pid 0 here means this thread only.
    """
    try:
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    except OSError:
        pass
SDP_OUT = os.path.join(pc.CONFIG_DIR, "aes67.sdp")
SAP_GROUP, SAP_PORT, SAP_INTERVAL_S = "239.255.255.255", 9875, 30


def log(msg):
    print(f"aes67-out: {msg}", flush=True)


class Sender:
    def __init__(self):
        self.audio = pc.audio_settings()
        self.ptp = pc.ptp_settings()
        self.iface = pc.resolve_iface(self.audio["iface"])
        self.src_ip = ""
        self.mac = pc.iface_mac(self.iface)
        self.channels = None
        self.gm_sdp = ""
        self.sdp = ""
        self.sdp_version = int(time.time())
        self.session_id = zlib.crc32(f"{self.audio['address']}:{self.audio['port']}".encode())
        self.lock = threading.Lock()
        self.levels = {"rms": [], "peak": []}
        self.level_seq = 0
        self.state = "starting"
        self.error = None
        self.latency_ms = None
        self.arrival_ms = None       # how long after its timestamp audio reaches the payloader
        self.arrival_window = []
        self.bytes_prev = (0, time.time())
        self.kbps = 0.0
        self.pipe = None
        self.sink = None
        self.loop = GLib.MainLoop()
        self.exit_code = 0
        self.sap_sock = None
        self.stop_evt = threading.Event()

    # ------------------------------------------------------------ status
    def write_status(self):
        a = self.audio
        with self.lock:
            data = {
                "ts": time.time(), "state": self.state, "error": self.error,
                "dest": f"{a['address']}:{a['port']}", "iface": self.iface, "src_ip": self.src_ip,
                "channels": self.channels, "encoding": a["encoding"], "ptime_us": a["ptime_us"],
                "rms": self.levels["rms"], "peak": self.levels["peak"],
                "kbps": round(self.kbps, 1), "latency_ms": self.latency_ms,
                "send_delay_ms": a["send_delay_ms"], "read_delay_ms": a["read_delay_ms"],
                "arrival_ms": self.arrival_ms, "gm": self.gm_sdp,
                "ptp_mode": self.ptp["mode"], "sap": bool(a["sap"]), "sdp": self.sdp,
            }
        try:
            pc.write_json_atomic(STATUS_OUT, data)
        except OSError:
            pass

    def levels_loop(self):
        """Write each new level reading to a small file for the GUI's meter stream."""
        normal_priority()
        last = -1
        while not self.stop_evt.wait(LEVEL_INTERVAL_MS / 1000):
            with self.lock:
                seq, lv, state = self.level_seq, self.levels, self.state
            if seq == last:
                continue
            last = seq
            try:
                pc.write_json_atomic(LEVELS_OUT, {"ts": time.time(), "seq": seq, "state": state,
                                                  "rms": lv["rms"], "peak": lv["peak"]})
            except OSError:
                pass

    def status_loop(self):
        normal_priority()
        ticks = 0
        while not self.stop_evt.wait(0.2):
            ticks += 1
            if ticks % 10 == 0 and self.pipe is not None:
                q = Gst.Query.new_latency()
                if self.pipe.query(q):
                    lat = self.send_lag(q.parse_latency()[1])
                    with self.lock:
                        self.latency_ms = lat
            if self.sink is not None:
                try:
                    served = self.sink.get_property("bytes-served")
                    now = time.time()
                    b0, t0 = self.bytes_prev
                    if now - t0 >= 1.0:
                        self.kbps = (served - b0) * 8 / 1000 / (now - t0)
                        self.bytes_prev = (served, now)
                except TypeError:
                    pass
            self.write_status()

    # --------------------------------------------------------------- SDP
    def build_sdp(self):
        a, p = self.audio, self.ptp
        ch, pt = self.channels, int(a["payload_type"])
        ptime = "1" if int(a["ptime_us"]) == 1000 else "0.125"
        domain = int(p["domain"])
        if p["mode"] != "off" and self.gm_sdp:
            refclk = f"a=ts-refclk:ptp=IEEE1588-2008:{self.gm_sdp}:{domain}"
        elif p["mode"] != "off":
            refclk = "a=ts-refclk:ptp=IEEE1588-2008:traceable"
        else:
            refclk = f"a=ts-refclk:localmac={self.mac.upper().replace(':', '-')}"
        lines = [
            "v=0",
            f"o=- {self.session_id} {self.sdp_version} IN IP4 {self.src_ip}",
            f"s={a['session_name']}",
            f"c=IN IP4 {a['address']}/{int(a['ttl'])}",
            "t=0 0",
            f"m=audio {int(a['port'])} RTP/AVP {pt}",
            f"i={ch} channels",
            f"a=rtpmap:{pt} {a['encoding']}/{SAMPLE_RATE}/{ch}",
        ]
        if p["profile"] == "st2059":
            groups = ["M"] if ch == 1 else ["ST"] * (ch // 2)
            lines.append(f"a=fmtp:{pt} channel-order=SMPTE2110.({','.join(groups)})")
        lines += [
            f"a=source-filter: incl IN IP4 {a['address']} {self.src_ip}",
            f"a=ptime:{ptime}",
            refclk,
            "a=mediaclk:direct=0",
            "a=recvonly",
        ]
        if p["profile"] == "dante" and p["mode"] != "off":
            # Dante Controller reads this to match the stream to its PTP domain
            lines.append(f"a=clock-domain:PTPv2 {domain}")
        return "\r\n".join(lines) + "\r\n"

    def refresh_sdp(self, reason=None):
        if self.channels is None or not self.src_ip:
            return
        sdp = self.build_sdp()
        if sdp == self.sdp:
            return
        if self.sdp:
            self.sdp_version += 1
            sdp = self.build_sdp()
        with self.lock:
            self.sdp = sdp
        try:
            with open(SDP_OUT + ".tmp", "w") as f:
                f.write(sdp)
            os.replace(SDP_OUT + ".tmp", SDP_OUT)
        except OSError as exc:
            log(f"could not write {SDP_OUT}: {exc}")
        log("SDP " + ("updated: " + reason if reason else "ready") + f", written to {SDP_OUT}")
        if self.audio["sap"]:
            self.sap_send()

    # --------------------------------------------------------------- SAP
    def sap_packet(self, delete=False):
        sdp = self.sdp.encode()
        msg_hash = zlib.crc32(sdp) & 0xFFFF
        header = bytes([0x24 if delete else 0x20, 0, msg_hash >> 8, msg_hash & 0xFF])
        return header + socket.inet_aton(self.src_ip) + b"application/sdp\0" + sdp

    def sap_send(self, delete=False):
        if not self.sdp:
            return
        try:
            if self.sap_sock is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(self.audio["ttl"]))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.src_ip))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
                self.sap_sock = s
            self.sap_sock.sendto(self.sap_packet(delete), (SAP_GROUP, SAP_PORT))
        except OSError as exc:
            log(f"SAP send failed: {exc}")

    def ptp_loop(self):
        """Follow grandmaster changes so the SDP reference clock stays right. Re-announce with SAP."""
        next_sap = time.time() + SAP_INTERVAL_S
        while not self.stop_evt.wait(2):
            if self.ptp["mode"] != "off":
                data, err = pc.pmc_query(self.ptp["domain"], ids=("PARENT_DATA_SET", "TIME_STATUS_NP"))
                gm = "" if err else pc.summarise(data)["gm_sdp"]
                if gm != self.gm_sdp:
                    self.gm_sdp = gm
                    self.refresh_sdp(f"grandmaster {gm or 'not known'}")
            if self.audio["sap"] and time.time() >= next_sap:
                self.sap_send()
                next_sap = time.time() + SAP_INTERVAL_S

    # ---------------------------------------------------------- pipeline
    def wait_for_flow(self):
        path = os.path.join(DOMAIN, f"{FLOW}.mxl-flow")
        waited = 0
        while not os.path.isdir(path):
            if waited % 15 == 0:
                log(f"waiting for the audio flow {FLOW}. The NDI source may have no audio.")
            with self.lock:
                self.state = "waiting"
            time.sleep(1)
            waited += 1

    def build(self):
        a = self.audio
        enc = a["encoding"]
        fmt = "S24BE" if enc == "L24" else "S16BE"
        pay = "rtpL24pay" if enc == "L24" else "rtpL16pay"
        ptime_ns = int(a["ptime_us"]) * 1000
        read_ns = int(float(a["read_delay_ms"]) * 1_000_000)
        # Sending waits for the read delay plus the send delay. The RTP
        # timestamps move forward by the read delay only, in 48 kHz samples.
        delay_ns = read_ns + int(float(a["send_delay_ms"]) * 1_000_000)
        rtp_offset = round(float(a["read_delay_ms"]) * SAMPLE_RATE / 1000) & 0xFFFFFFFF
        if a["source"] == "tone":
            # 1 kHz at -20 dBFS on every channel, for checking the network path
            # to Dante or a 2110 receiver without an NDI source
            ch = int(a["channels"])
            src = ("audiotestsrc is-live=true wave=sine freq=1000 volume=0.1 samplesperbuffer=48 "
                   f"! audio/x-raw,format=F32LE,rate={SAMPLE_RATE},channels={ch},"
                   f"channel-mask=(bitmask){hex((1 << ch) - 1)},layout=interleaved ")
        else:
            src = f'mxlsrc audio-flow-id="{FLOW}" domain="{DOMAIN}" '
        desc = (
            src +
            "! identity name=src_out silent=true "
            "! queue max-size-time=0 max-size-bytes=0 max-size-buffers=0 "
            f"! level name=level interval={LEVEL_INTERVAL_MS * 1_000_000} post-messages=true "
            "! audioconvert ! audioresample "
            f"! audio/x-raw,format={fmt},rate={SAMPLE_RATE} "
            f"! {pay} name=pay pt={int(a['payload_type'])} timestamp-offset={rtp_offset} "
            f"min-ptime={ptime_ns} max-ptime={ptime_ns} "
            f'! udpsink name=out host={a["address"]} port={int(a["port"])} '
            f'multicast-iface={self.iface} ttl-mc={int(a["ttl"])} qos-dscp={int(a["dscp"])} '
            "auto-multicast=true loop=false sync=true async=false "
            # processing-deadline defaults to 20 ms and would hold every packet back by that much
            f"processing-deadline=0 ts-offset={delay_ns}"
        )
        pipe = Gst.parse_launch(desc)

        # Running time == TAI time: TAI system clock and a base time of zero.
        clock = Gst.SystemClock.obtain()
        clock.set_property("clock-type", Gst.ClockType.TAI)
        pipe.use_clock(clock)
        pipe.set_start_time(Gst.CLOCK_TIME_NONE)
        pipe.set_base_time(0)

        pay_el = pipe.get_by_name("pay")
        pay_el.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self.on_event)
        # Measured straight after the source, before the queue: further down,
        # buffers also wait for the read delay itself.
        pipe.get_by_name("src_out").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self.on_buffer)
        self.clock = clock
        self.sink = pipe.get_by_name("out")
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)
        self.pipe = pipe

    def on_event(self, pad, info):
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            st = ev.parse_caps().get_structure(0)
            ok, ch = st.get_int("channels")
            if ok and ch != self.channels:
                self.channels = ch
                log(f"sending {ch} ch {self.audio['encoding']} {SAMPLE_RATE // 1000} kHz, "
                    f"{self.audio['ptime_us']} us packets to {self.audio['address']}:{self.audio['port']} "
                    f"from {self.src_ip} on {self.iface}")
                GLib.idle_add(self.refresh_sdp)
        return Gst.PadProbeReturn.OK

    def on_buffer(self, _pad, info):
        """How late audio reaches us compared with its timestamp (running time == TAI here)."""
        buf = info.get_buffer()
        if buf.pts != Gst.CLOCK_TIME_NONE:
            lag = (self.clock.get_time() - buf.pts) / 1e6
            self.arrival_window.append(lag)
            if len(self.arrival_window) >= 500:          # about half a second of 1 ms buffers
                peak = round(max(self.arrival_window), 1)
                self.arrival_window = []
                with self.lock:
                    first = self.arrival_ms is None
                    self.arrival_ms = peak
                limit = float(self.audio["read_delay_ms"])
                if peak > limit and (first or int(time.time()) % 10 == 0):
                    log(f"audio reaches the sender {peak} ms after its timestamp, more than the "
                        f"{limit:g} ms read delay. Packets will be late. Raise the read delay.")
        return Gst.PadProbeReturn.OK

    def on_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ELEMENT:
            st = msg.get_structure()
            if st is not None and st.get_name() == "level":
                rms = [round(v, 1) for v in st.get_value("rms")]
                peak = [round(v, 1) for v in st.get_value("peak")]
                with self.lock:
                    self.levels = {"rms": rms, "peak": peak}
                    self.level_seq += 1
                    self.state = "sending"
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(f"error: {err.message}")
            if dbg:
                log(dbg.splitlines()[0])
            with self.lock:
                self.state, self.error = "error", err.message
            self.write_status()
            self.exit_code = 1   # the container restarts and tries again
            self.loop.quit()
        elif t == Gst.MessageType.LATENCY:
            # udpsink announces a latency change when processing-deadline is set.
            # Without a recalculation the pipeline keeps the 20 ms default.
            self.pipe.recalculate_latency()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipe:
            _, new, _ = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                self.pipe.recalculate_latency()
                GLib.timeout_add(1500, self.report_latency)

    def send_lag(self, pipeline_latency_ns):
        """
        How long after its RTP timestamp a packet leaves. Normally the pipeline
        latency plus the send delay. If audio reaches the sender later than the
        read delay allows, it goes out as soon as it arrives, so later still.
        """
        a = self.audio
        planned = pipeline_latency_ns / 1e6 + float(a["send_delay_ms"])
        late = (self.arrival_ms - float(a["read_delay_ms"])) if self.arrival_ms is not None else 0
        return round(max(planned, late), 2)

    def report_latency(self):
        q = Gst.Query.new_latency()
        if self.pipe is not None and self.pipe.query(q):
            lat = self.send_lag(q.parse_latency()[1])
            with self.lock:
                self.latency_ms = lat
            log(f"packets leave about {lat} ms after their RTP timestamp. Receivers need more "
                "latency than that, plus the network.")
        return False

    # --------------------------------------------------------------- run
    def on_signal(self):
        """SIGTERM or SIGINT while the main loop runs: stop it, and clean up in run()."""
        log("stopping")
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def cleanup(self):
        self.stop_evt.set()
        if self.audio["sap"]:
            self.sap_send(delete=True)
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.NULL)
        with self.lock:
            self.state = "stopped" if self.exit_code == 0 else "error"
        self.write_status()

    def realtime(self):
        """
        SCHED_FIFO for this process. GStreamer's streaming threads are created
        later and inherit it, so udpsink wakes on time even on a busy VM.
        Needs CAP_SYS_NICE, which the compose file grants.
        """
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
            log("running at real-time priority (SCHED_FIFO 50)")
        except (PermissionError, OSError) as exc:
            log(f"could not get real-time priority ({exc}). Packet timing will be looser.")

    def run(self):
        """Returns the process exit code."""
        # Until the main loop runs, a signal simply ends the process
        signal.signal(signal.SIGTERM, exit_on_signal)
        signal.signal(signal.SIGINT, exit_on_signal)
        if not self.audio["enabled"]:
            with self.lock:
                self.state = "off"
            self.write_status()
            log("AES67 output is off. Waiting until it is turned on in the GUI.")
            while True:
                time.sleep(3600)
        for _ in range(30):
            self.src_ip = pc.iface_ipv4(self.iface)
            if self.src_ip:
                break
            log(f"no IPv4 address on {self.iface or 'any interface'} yet")
            time.sleep(2)
        if not self.src_ip:
            return 1

        threading.Thread(target=self.status_loop, daemon=True).start()
        threading.Thread(target=self.levels_loop, daemon=True).start()
        if self.audio["source"] == "tone":
            log("sending a 1 kHz test tone at -20 dBFS instead of the MXL audio flow")
        else:
            self.wait_for_flow()
        if self.ptp["mode"] != "off":
            data, err = pc.pmc_query(self.ptp["domain"], ids=("PARENT_DATA_SET", "TIME_STATUS_NP"))
            self.gm_sdp = "" if err else pc.summarise(data)["gm_sdp"]
            if err:
                log(f"PTP status not available yet ({err}). The SDP will update when it is.")
        else:
            log("PTP is off. RTP timestamps follow this VM's free-running clock, "
                "so receivers locked to PTP will not stay in sync.")
        log(f"kernel TAI offset is {pc.kernel_tai_offset()} s")

        self.realtime()
        Gst.init(None)
        try:
            self.build()
        except GLib.Error as exc:
            log(f"could not build the pipeline: {exc.message}")
            with self.lock:
                self.state, self.error = "error", exc.message
            self.write_status()
            return 1
        threading.Thread(target=self.ptp_loop, daemon=True).start()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.on_signal)
        self.pipe.set_state(Gst.State.PLAYING)
        with self.lock:
            self.state = "starting"
        self.loop.run()
        self.cleanup()
        return self.exit_code


def exit_on_signal(signum, _frame):
    log("stopping")
    raise SystemExit(0)


if __name__ == "__main__":
    sys.exit(Sender().run())
