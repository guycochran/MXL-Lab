# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""Background watchers: MXL flow rates, PTP state through pmc, and the AES67 sender's status."""
import os
import re
import subprocess
import threading
import time

from .settings import AUDIO_STATUS, DOMAIN, FLOW_A, FLOW_AUDIO, FLOW_B, load_json, pc


class FlowWatch:
    """Runs mxl-info against each flow and works out the grain rate."""

    HEAD = re.compile(r"head\s*index\D*(\d+)", re.I)

    def __init__(self):
        self.lock = threading.Lock()
        self.prev = {}
        self.cache = {}
        self.cache_ts = 0.0

    def one(self, flow_id):
        exists = os.path.isdir(os.path.join(DOMAIN, f"{flow_id}.mxl-flow"))
        info = {"id": flow_id, "exists": exists, "head": None, "rate": None, "raw": ""}
        if not exists:
            return info
        try:
            res = subprocess.run(["mxl-info", "-d", DOMAIN, "-f", flow_id],
                                 capture_output=True, text=True, timeout=2)
            raw = (res.stdout or res.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raw = f"mxl-info failed: {exc}"
        info["raw"] = raw[:2000]
        m = self.HEAD.search(raw)
        if m:
            head, now = int(m.group(1)), time.time()
            info["head"] = head
            prev = self.prev.get(flow_id)
            if prev and now > prev[1] and head >= prev[0]:
                info["rate"] = round((head - prev[0]) / (now - prev[1]), 1)
            self.prev[flow_id] = (head, now)
        return info

    def status(self):
        with self.lock:
            if time.time() - self.cache_ts > 1.0:
                self.cache = {"A": self.one(FLOW_A), "B": self.one(FLOW_B),
                              "audio": self.one(FLOW_AUDIO)}
                self.cache_ts = time.time()
            return self.cache


# -------------------------------------------------------------------- PTP
class PtpWatch:
    """Polls the local ptp4l once a second and keeps two minutes of offsets."""

    HISTORY = 120
    STEP_S = 0.5      # a jump in CLOCK_TAI bigger than this, between polls, is a step

    def __init__(self):
        self.lock = threading.Lock()
        self.summary = {}
        self.error = None
        self.history = []     # [time, offset_ns]
        self.updated = 0.0
        self.last_step = None  # {"at": unix time, "seconds": size}
        self.on_step = None    # called with the step size, in a new thread
        self._tai_minus_mono = None
        threading.Thread(target=self._run, daemon=True).start()

    def _check_step(self):
        """
        CLOCK_TAI minus CLOCK_MONOTONIC only changes when the clock is stepped
        or the kernel TAI offset changes. Either one moves MXL time under the
        running functions, which breaks the flows, so restart them in order.
        """
        diff = time.clock_gettime(time.CLOCK_TAI) - time.monotonic()
        prev, self._tai_minus_mono = self._tai_minus_mono, diff
        if prev is None or abs(diff - prev) < self.STEP_S:
            return
        size = diff - prev
        with self.lock:
            self.last_step = {"at": time.time(), "seconds": round(size, 3)}
        if self.on_step is not None:
            threading.Thread(target=self.on_step, args=(size,), daemon=True).start()

    def _run(self):
        while True:
            try:
                self._check_step()
            except OSError as exc:   # CLOCK_TAI unreadable: nothing to compare
                print(f"gui: clock step check failed: {exc}", flush=True)
            cfg = pc.ptp_settings()
            if cfg["mode"] == "off":
                with self.lock:
                    self.summary, self.error, self.history = {}, None, []
                time.sleep(2)
                continue
            data, err = pc.pmc_query(cfg["domain"])
            now = time.time()
            with self.lock:
                self.error = err
                if not err:
                    self.summary = pc.summarise(data)
                    off = self.summary.get("offset_ns")
                    if (off is not None and self.summary.get("port_state") in ("SLAVE", "UNCALIBRATED")
                            and (self.summary.get("last_sync_ingress") or 0) > 0):
                        self.history.append([round(now, 1), off])
                        self.history = self.history[-self.HISTORY:]
                else:
                    self.summary = {}
                self.updated = now
            time.sleep(1)

    def reset(self):
        """Forget the last answer, so a restarted ptp4l is not judged on its predecessor."""
        with self.lock:
            self.summary, self.error, self.history = {}, None, []

    def locked(self):
        """Following with three offsets under 1 ms in a row, or acting as grandmaster."""
        with self.lock:
            state = self.summary.get("port_state", "")
            if state in ("MASTER", "GRAND_MASTER"):
                return True
            if state != "SLAVE":
                return False
            recent = [abs(o) for _, o in self.history[-3:]]
            return len(recent) == 3 and max(recent) < 1_000_000

    def snapshot(self):
        with self.lock:
            return {"summary": dict(self.summary), "error": self.error,
                    "history": list(self.history), "updated": self.updated,
                    "kernel_tai": pc.kernel_tai_offset(), "last_step": self.last_step,
                    "boot_time": time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)}


def audio_status():
    st = load_json(AUDIO_STATUS)
    if st.get("ts"):
        st["age"] = round(time.time() - st["ts"], 1)
    st.pop("sdp", None)
    return st

