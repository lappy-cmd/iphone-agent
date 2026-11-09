"""Laptop location helper: polls the agent server and drives pymobiledevice3.

Run (in a normal terminal, while `pymobiledevice3 remote tunneld` runs in an ADMIN terminal):
    python location_helper.py --server https://YOURNAME.duckdns.org --key HELPER_KEY
    python location_helper.py ... --dry-run     # log commands without touching the phone
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time

import httpx

log = logging.getLogger("helper")
POLL_SECONDS = 3
# DVT location simulation can stop when the connection closes, so `set` stays running.
SET_STARTUP_GRACE = 6  # seconds to watch for an immediate failure


def pmd3_base() -> list[str]:
    exe = shutil.which("pymobiledevice3")
    return [exe] if exe else [sys.executable, "-m", "pymobiledevice3"]


class Simulator:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.proc: subprocess.Popen | None = None
        self.mounted = False

    def _run(self, args: list[str], timeout: int = 60) -> tuple[bool, str]:
        cmd = pmd3_base() + args
        log.info("run: %s", " ".join(cmd))
        if self.dry_run:
            return True, "dry run"
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out: {' '.join(args)}"
        out = (r.stderr or r.stdout or "").strip()
        return r.returncode == 0, out[-400:]

    def _ensure_mounted(self) -> tuple[bool, str]:
        if self.mounted:
            return True, ""
        ok, out = self._run(["mounter", "auto-mount"], timeout=180)
        # "already mounted" is reported as an error by some versions
        if ok or "already" in out.lower():
            self.mounted = True
            return True, ""
        return False, f"developer disk image mount failed: {out}"

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def set(self, lat: float, lon: float) -> tuple[bool, str]:
        ok, err = self._ensure_mounted()
        if not ok:
            return False, err
        self._stop()
        cmd = pmd3_base() + ["developer", "dvt", "simulate-location", "set", "--", str(lat), str(lon)]
        log.info("start: %s", " ".join(cmd))
        if self.dry_run:
            return True, "dry run"
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        deadline = time.time() + SET_STARTUP_GRACE
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = (self.proc.stdout.read() if self.proc.stdout else "").strip()
                if self.proc.returncode == 0:
                    return True, "set (process exited cleanly)"
                return False, out[-400:] or f"exit code {self.proc.returncode}"
            time.sleep(0.5)
        return True, "set (simulation running)"

    def clear(self) -> tuple[bool, str]:
        self._stop()
        return self._run(["developer", "dvt", "simulate-location", "clear"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("AGENT_SERVER"), help="https://... server URL")
    ap.add_argument("--key", default=os.environ.get("HELPER_KEY"), help="HELPER_KEY from server .env")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.server or not args.key:
        ap.error("--server and --key are required (or AGENT_SERVER / HELPER_KEY env vars)")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sim = Simulator(args.dry_run)
    http = httpx.Client(
        base_url=args.server.rstrip("/"), headers={"X-Helper-Key": args.key}, timeout=15
    )
    log.info("helper running%s; Ctrl+C to stop (this clears the fake location)",
             " (DRY RUN)" if args.dry_run else "")
    try:
        while True:
            try:
                r = http.get("/helper/poll")
                if r.status_code == 401:
                    log.error("server rejected HELPER_KEY")
                    time.sleep(30)
                    continue
                r.raise_for_status()
                cmd = r.json().get("command")
            except httpx.HTTPError as exc:
                log.warning("server unreachable: %s", exc)
                time.sleep(POLL_SECONDS * 3)
                continue
            if cmd:
                log.info("command: %s", cmd)
                if cmd["action"] == "set":
                    ok, detail = sim.set(cmd["payload"]["lat"], cmd["payload"]["lon"])
                elif cmd["action"] == "clear":
                    ok, detail = sim.clear()
                else:
                    ok, detail = False, f"unknown action {cmd['action']}"
                log.info("result ok=%s %s", ok, detail)
                try:
                    http.post("/helper/result", json={"id": cmd["id"], "ok": ok, "detail": detail})
                except httpx.HTTPError as exc:
                    log.warning("could not report result: %s", exc)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        log.info("stopping; clearing simulated location")
        sim.clear()


if __name__ == "__main__":
    main()
