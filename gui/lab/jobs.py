# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""Ordered changes to the running lab: one at a time, in the background, with a log for the page."""
import os
import re
import threading
import time
import traceback
from datetime import UTC, datetime

from fastapi import HTTPException

from .docker_ctl import container
from .mxl_domain import clear_owned_flows
from .settings import DOMAIN, FLOW_A, FLOW_AUDIO, FLOW_B, PIPELINE_CFG, pc, pipeline_settings, write_json_atomic


class ApplyJob:
    """
    Runs one change at a time in the background and keeps a log for the page.

    A change is an optional first step (write settings, restart PTP), then
    either a full ordered restart of the media functions or, when there is no
    NDI source yet, just the AES67 output.
    """

    MEDIA_STOP = ("aes67-out", "ndi-out-vertical", "ndi-out-original", "autoframe", "ndi-ingest")

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log = []
        self.result = None
        self.kind = "pipeline"

    def _say(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _wait_flow(self, flow_id, timeout=15):
        path = os.path.join(DOMAIN, f"{flow_id}.mxl-flow")
        end = time.time() + timeout
        while time.time() < end:
            if os.path.isdir(path):
                return True
            time.sleep(0.5)
        return False

    def start(self, settings, first=None, kind="pipeline", audio=False):
        """
        settings None means no media restart. With audio=True the AES67
        output is then restarted (or stopped) on its own.
        """
        with self.lock:
            if self.running:
                raise HTTPException(409, "a change is already being applied")
            self.running, self.log, self.result, self.kind = True, [], None, kind
        threading.Thread(target=self._run, args=(settings, first, audio), daemon=True).start()

    def _audio_only(self):
        c = container("aes67-out")
        if c.status == "running":
            c.stop(timeout=5)
            self._say("Stopped aes67-out")
        if pc.audio_settings()["enabled"]:
            c.start()
            self._say("Started aes67-out")
        else:
            self._say("AES67 output is off")

    def _restart_media(self, settings):
        write_json_atomic(PIPELINE_CFG, settings)
        self._say("Saved pipeline settings")
        for svc in self.MEDIA_STOP:
            container(svc).stop(timeout=5)
            self._say(f"Stopped {svc}")

        removed = clear_owned_flows(DOMAIN, (FLOW_A, FLOW_B, FLOW_AUDIO))
        self._say(f"Cleared {len(removed)} lab-owned MXL flows")

        container("ndi-ingest").start()
        self._say("Started ndi-ingest, waiting for Flow A")
        if not self._wait_flow(FLOW_A):
            raise RuntimeError("Flow A did not appear. Check the ndi-ingest log and the source name.")
        self._say("Flow A is up")
        if self._wait_flow(FLOW_AUDIO, timeout=5):
            self._say("Audio flow is up")
        else:
            self._say("No audio flow yet. The source may not be sending audio. Carrying on.")

        container("ndi-out-original").start()
        self._say("Started ndi-out-original")
        container("autoframe").start()
        self._say("Started autoframe, waiting for Flow B")
        if not self._wait_flow(FLOW_B):
            raise RuntimeError("Flow B did not appear. Check the autoframe log.")
        self._say("Flow B is up")

        container("ndi-out-vertical").start()
        self._say("Started ndi-out-vertical")
        if pc.audio_settings()["enabled"]:
            container("aes67-out").start()
            self._say("Started aes67-out")

    def _run(self, settings, first=None, audio=False):
        try:
            if first is not None:
                first(self._say)
            if settings is not None:
                self._restart_media(settings)
            elif audio:
                self._audio_only()
            self.result = "ok"
        except HTTPException as exc:
            self._say(f"Failed: {exc.detail}")
            self.result = "failed"
        except Exception as exc:  # noqa: BLE001  a background job must report every failure
            self._say(f"Failed: {exc}")
            self.result = "failed"
            traceback.print_exc()
        finally:
            with self.lock:
                self.running = False

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "result": self.result,
                    "log": list(self.log), "kind": self.kind}


def restart_after_step(job, size):
    """A clock step outside an apply: restart the media functions in order."""
    if job.snapshot()["running"]:
        return   # an apply is already restarting everything
    settings = pipeline_settings()

    def first(say):
        say(f"The clock stepped by {size:+.3f} s, so the running MXL flows are out of step. "
            "Restarting the media functions.")
    try:
        job.start(settings if settings["NDI_SRC"] else None, first, kind="ptp", audio=True)
    except HTTPException:
        pass


def wait_for_ptp(ptpw, say, timeout=90):
    """Log the port state as it changes, until ptp4l is following or grandmaster."""
    end, last = time.time() + timeout, None
    while time.time() < end:
        snap = ptpw.snapshot()
        state = snap["summary"].get("port_state") or ("no answer" if snap["error"] else "starting")
        if state != last:
            say(f"PTP port state: {state}")
            last = state
        if ptpw.locked():
            s = snap["summary"]
            if s.get("gm_is_self"):
                say("This VM is the grandmaster")
            else:
                off = s.get("offset_ns")
                say(f"Following {s.get('gm_identity') or 'a grandmaster'}"
                    + (f", offset {off / 1000:.1f} us" if off is not None else ""))
            return True
        time.sleep(1)
    say(f"PTP did not settle within {timeout} s. Starting the media functions anyway.")
    return False



STARTUP_WINDOW_S = 180   # a PTP container younger than this means a boot or a restart


def docker_time(text):
    """Docker's StartedAt (RFC 3339 with nanoseconds) as a Unix time, or None."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z?$", text or "")
    if not m:
        return None
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return base.timestamp() + float(m.group(2) or 0)


def startup_sequence(job, ptpw):
    """
    After a VM boot every container starts at once, before PTP has stepped the
    clock and set the TAI offset. When the PTP function has only just started,
    wait for it and then restart the media functions in order.
    """
    time.sleep(3)
    if pc.ptp_settings()["mode"] == "off":
        return
    try:
        started = container("ptp").attrs.get("State", {}).get("StartedAt", "")
    except HTTPException:
        return
    started_at = docker_time(started)
    if started_at is None or time.time() - started_at > STARTUP_WINDOW_S:
        return
    settings = pipeline_settings()

    def first(say):
        say("PTP function started recently (VM boot or restart). Waiting for it before starting media.")
        wait_for_ptp(ptpw, say)
    try:
        job.start(settings if settings["NDI_SRC"] else None, first, kind="ptp", audio=True)
    except HTTPException:
        pass
