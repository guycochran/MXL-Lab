# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""NDI source discovery and format probing, each run in a short-lived child process."""
import json
import subprocess
import sys
import threading
import time

# Runs in a short-lived child process for every scan. In the lab, only the
# first GStreamer device monitor in a process ever found NDI sources, while
# a fresh process found them every time, so each scan gets a fresh process.
SCAN_CODE = r"""
import json, sys, time, gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
settle = float(sys.argv[1])
mon = Gst.DeviceMonitor.new()
mon.add_filter("Source/Network", Gst.Caps.from_string("application/x-ndi"))
if not mon.start():
    print(json.dumps({"error": "device monitor did not start, is the NDI plugin loaded?"}))
    sys.exit(0)
time.sleep(settle)
out = []
for dev in mon.get_devices() or []:
    entry = {"name": dev.get_display_name()}
    props = dev.get_properties()
    if props is not None:
        for key in ("url-address", "address", "ndi-name"):
            if props.has_field(key):
                entry[key] = str(props.get_value(key))
    out.append(entry)
mon.stop()
print(json.dumps({"sources": out}))
"""


# Connects to one NDI source, waits for the first video frame and reports its
# size and frame rate. Runs in a child process for the same reason as scans.
PROBE_CODE = r"""
import json, sys, gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
name, url, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])

def fail(msg):
    print(json.dumps({"error": msg})); sys.exit(0)

pipe = Gst.Pipeline.new("probe")
src = Gst.ElementFactory.make("ndisrc")
demux = Gst.ElementFactory.make("ndisrcdemux")
sink = Gst.ElementFactory.make("appsink")
if not (src and demux and sink):
    fail("NDI GStreamer plugin not available")
src.set_property("ndi-name", name)
if url:
    src.set_property("url-address", url)
try:
    src.set_property("timeout", int(timeout * 1000))
except TypeError:
    pass
sink.set_property("sync", False)
sink.set_property("max-buffers", 1)
sink.set_property("drop", True)
for e in (src, demux, sink):
    pipe.add(e)
src.link(demux)

def on_pad(_demux, pad):
    if pad.get_name().startswith("video"):
        pad.link(sink.get_static_pad("sink"))
    else:
        fake = Gst.ElementFactory.make("fakesink")
        fake.set_property("sync", False)
        pipe.add(fake)
        fake.sync_state_with_parent()
        pad.link(fake.get_static_pad("sink"))

demux.connect("pad-added", on_pad)
pipe.set_state(Gst.State.PLAYING)
sample = sink.emit("try-pull-sample", int(timeout * Gst.SECOND))
if sample is None:
    pipe.set_state(Gst.State.NULL)
    fail("no video from the source. Check the name or address, and that it is sending.")
st = sample.get_caps().get_structure(0)
ok_w, width = st.get_int("width")
ok_h, height = st.get_int("height")
ok_f, num, den = st.get_fraction("framerate")
fmt = st.get_string("format") or ""
pipe.set_state(Gst.State.NULL)
if not (ok_w and ok_h):
    fail("could not read the frame size from the source")
print(json.dumps({"width": width, "height": height,
                  "fps_n": num if ok_f else 0, "fps_d": den if ok_f and den else 1,
                  "format": fmt}))
"""


def probe_source(name, url="", timeout=10):
    """Return the source's video format, or raise RuntimeError with a readable reason."""
    res = subprocess.run([sys.executable, "-c", PROBE_CODE, name, url, str(timeout)],
                         capture_output=True, text=True, timeout=timeout + 15)
    lines = [line for line in res.stdout.splitlines() if line.startswith("{")]
    if not lines:
        tail = (res.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("probe produced no result: " + " | ".join(tail))
    data = json.loads(lines[-1])
    if "error" in data:
        raise RuntimeError(data["error"])
    w, h, n, d = data["width"], data["height"], data["fps_n"], data["fps_d"]
    if w % 2 or h % 2:
        raise RuntimeError(f"source is {w}x{h}; width and height must be even")
    if w <= h:
        raise RuntimeError(f"source is {w}x{h}; the 9:16 crop needs a landscape source")
    if w > 4096 or h > 2160:
        raise RuntimeError(f"source is {w}x{h}; larger than 4096x2160 is not supported")
    if n <= 0:
        raise RuntimeError("source did not report a frame rate")
    data["fps"] = str(n) if d == 1 else f"{n}/{d}"
    data["label"] = f"{w}x{h} at {round(n / d, 2):g} fps"
    return data


class NdiDiscovery:
    """Scans for NDI sources in a background thread, one child process per scan."""

    SETTLE_S = 6      # time the NDI finder gets to collect sources per scan
    INTERVAL_S = 4    # pause between scans
    MIN_GAP_S = 2     # never start a scan sooner than this after the last one
    EXPIRE_S = 30     # drop a source only after it has been missing this long

    def __init__(self):
        self.lock = threading.Lock()
        self.sources = []
        self.updated = 0.0
        self.error = None
        self.scanning = False
        self.seen = {}    # name -> (entry, last time seen)
        self.restart = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _scan(self):
        res = subprocess.run([sys.executable, "-c", SCAN_CODE, str(self.SETTLE_S)],
                             capture_output=True, text=True, timeout=self.SETTLE_S + 15)
        lines = [line for line in res.stdout.splitlines() if line.startswith("{")]
        if not lines:
            tail = (res.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError("scan produced no result: " + " | ".join(tail))
        data = json.loads(lines[-1])
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["sources"]

    def _run(self):
        while True:
            self.restart.clear()
            with self.lock:
                self.scanning = True
            try:
                found = self._scan()
                now = time.time()
                with self.lock:
                    for entry in found:
                        self.seen[entry["name"]] = (entry, now)
                    # One scan can come back short, so a source is kept until it
                    # has been missing from every scan for EXPIRE_S.
                    self.seen = {n: v for n, v in self.seen.items() if now - v[1] < self.EXPIRE_S}
                    self.sources = sorted((v[0] for v in self.seen.values()), key=lambda e: e["name"].lower())
                    self.updated, self.error = now, None
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as exc:
                with self.lock:
                    self.error = str(exc)
            finally:
                with self.lock:
                    self.scanning = False
            time.sleep(self.MIN_GAP_S)
            self.restart.wait(max(0, self.INTERVAL_S - self.MIN_GAP_S))

    def reset(self):
        """Forget cached sources, for when the discovery method changes."""
        with self.lock:
            self.seen, self.sources = {}, []
        self.restart.set()

    def snapshot(self):
        with self.lock:
            return {"sources": list(self.sources), "updated": self.updated,
                    "error": self.error, "scanning": self.scanning}

