"""Laptop location helper: polls the agent server and drives the iPhone's simulated location over USB.

Run in a normal terminal (no admin, no tunneld) with the iPhone plugged in and unlocked:
    python location_helper.py          # reads AGENT_SERVER and HELPER_KEY from helper/.env
    python location_helper.py --server https://iphone-agent-rhf4.onrender.com --key HELPER_KEY
    python location_helper.py ... --dry-run     # log commands without touching the phone

Commands from the server:
    set   {lat, lon}                      hold one spot
    trip  {stops:[{lat,lng,name,dwell_s}], mode, loop}
                                          move along real roads like a person (motion.py)
    clear {}                              back to the real location
Keep this running: the phone drops the fake location as soon as the connection closes.
Ctrl+C clears it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from typing import Callable

import motion

log = logging.getLogger("helper")
POLL_SECONDS = 3
TICK_SECONDS = 1
KEEPALIVE_SECONDS = 10  # re-send a still pin so a dropped cable is noticed


class Driver:
    """Holds what the phone should show (a pin or a moving trajectory) and keeps it applied."""

    def __init__(self, session, clock: Callable[[], float] = time.time, build=None):
        self.session = session
        self.clock = clock
        self.build = build or (lambda stops, mode, loop=False, log=None: motion.build(
            stops, mode, loop=loop, log=log))
        self.pin: tuple[float, float] | None = None
        self.traj = None
        self.t0 = 0.0
        self.current: tuple[float, float] | None = None
        self.connected = False
        self.sent_at = 0.0

    def target(self) -> tuple[float, float] | None:
        if self.traj is not None:
            el = self.clock() - self.t0
            if not self.traj.loop:
                el = min(el, self.traj.total_s)
            p = self.traj.pos_at(el)
            return p["lat"], p["lng"]
        return self.pin

    async def _apply(self) -> None:
        if not self.connected:
            await self.session.open()
            self.connected = True
            self.current = None  # re-apply after a reconnect
        pt = self.target()
        if pt is not None and (pt != self.current or self.clock() - self.sent_at >= KEEPALIVE_SECONDS):
            await self.session.set(*pt)
            self.current, self.sent_at = pt, self.clock()

    async def _drop(self) -> None:
        self.connected = False
        self.current = None
        await self.session.close()

    async def step(self) -> bool:
        """Push the current point to the phone. False if the link is down (caller backs off)."""
        if self.target() is None:
            return True
        try:
            await self._apply()
            return True
        except Exception as e:
            log.warning("phone link lost: %s", e)
            await self._drop()
            return False

    async def handle(self, cmd: dict) -> tuple[bool, str]:
        action, p = cmd.get("action"), cmd.get("payload") or {}
        try:
            if action == "set":
                self.traj, self.pin = None, (float(p["lat"]), float(p["lon"]))
                await self._apply()
                return True, "set"
            if action == "trip":
                stops = [motion.Stop(float(s["lat"]), float(s["lng"]), s.get("name", ""), float(s.get("dwell_s", 0)))
                         for s in p["stops"]]
                traj = await asyncio.to_thread(self.build, stops, p.get("mode", "drive"),
                                               loop=bool(p.get("loop")), log=log.info)
                self.pin, self.traj, self.t0 = None, traj, self.clock()
                await self._apply()
                straight = sum(1 for leg in traj.legs if leg.straight)
                detail = f"{p.get('mode', 'drive')}, about {max(1, round(traj.total_s / 60))} min"
                if p.get("loop"):
                    detail += ", looping"
                if straight:
                    detail += f", {straight} leg(s) in a straight line (no road route found)"
                return True, detail
            if action == "clear":
                self.pin = self.traj = None
                await self.session.clear()
                await self._drop()
                return True, "cleared"
            return False, f"unknown action {action}"
        except Exception as e:
            await self._drop()
            if action == "set":
                self.pin = None
            elif action == "trip":
                self.traj = None
            return False, f"{type(e).__name__}: {e}"[:400]


async def drive_loop(driver: Driver) -> None:
    backoff = 2
    while True:
        if await driver.step():
            backoff = 2
            await asyncio.sleep(TICK_SECONDS)
        else:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def server_loop(driver: Driver, http) -> None:
    import httpx

    while True:
        try:
            r = await asyncio.to_thread(http.get, "/helper/poll")
            if r.status_code == 401:
                log.error("server rejected HELPER_KEY")
                await asyncio.sleep(30)
                continue
            r.raise_for_status()
            cmd = r.json().get("command")
        except httpx.HTTPError as exc:
            log.warning("server unreachable: %s", exc)
            await asyncio.sleep(POLL_SECONDS * 3)
            continue
        if cmd:
            log.info("command: %s", cmd["action"])
            ok, detail = await driver.handle(cmd)
            log.info("result ok=%s %s", ok, detail)
            try:
                await asyncio.to_thread(
                    http.post, "/helper/result", json={"id": cmd["id"], "ok": ok, "detail": detail}
                )
            except httpx.HTTPError as exc:
                log.warning("could not report result: %s", exc)
        await asyncio.sleep(POLL_SECONDS)


async def amain(args) -> None:
    import httpx

    from device import DryRunSession, PhoneSession

    session = DryRunSession() if args.dry_run else PhoneSession(args.udid, args.host)
    driver = Driver(session)
    http = httpx.Client(
        base_url=args.server.rstrip("/"), headers={"X-Helper-Key": args.key}, timeout=15
    )
    log.info("helper running%s; Ctrl+C to stop (this clears the fake location)",
             " (DRY RUN)" if args.dry_run else "")
    try:
        await asyncio.gather(drive_loop(driver), server_loop(driver, http))
    finally:
        log.info("stopping; clearing simulated location")
        try:
            await session.clear()
        except Exception:
            pass
        await session.close()


ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def save_env_value(path: str, key: str, value: str) -> None:
    """Set KEY=value in helper/.env, keeping every other line."""
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except OSError:
        lines = []
    lines = [ln for ln in lines if ln.partition("=")[0].strip() != key] + [f"{key}={value}"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


async def setup_remote(udid: str | None) -> None:
    from device import enable_network_access

    udid = await enable_network_access(udid)
    save_env_value(ENV_PATH, "PHONE_UDID", udid)
    log.info("network access enabled and PHONE_UDID saved to helper/.env")
    log.info("now add PHONE_HOST=<your iPhone's Tailscale IP> to helper/.env")


def load_env_file(path: str) -> None:
    """Read KEY=VALUE lines (AGENT_SERVER, HELPER_KEY) from helper/.env without overriding real env vars."""
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    load_env_file(ENV_PATH)
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("AGENT_SERVER"), help="https://... server URL")
    ap.add_argument("--key", default=os.environ.get("HELPER_KEY"), help="HELPER_KEY from server .env")
    ap.add_argument("--udid", default=os.environ.get("PHONE_UDID"),
                    help="the phone's ID (needed for --host; --setup-remote saves it)")
    ap.add_argument("--host", default=os.environ.get("PHONE_HOST"),
                    help="connect over the network to this phone IP (its Tailscale 100.x address)")
    ap.add_argument("--usb", action="store_true", help="ignore PHONE_HOST and use the cable")
    ap.add_argument("--setup-remote", action="store_true",
                    help="one time, phone plugged in: allow network connections and save PHONE_UDID")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.usb:
        args.host = None
    if args.setup_remote:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        asyncio.run(setup_remote(args.udid))
        return
    if not args.server or not args.key:
        ap.error("--server and --key are required (or AGENT_SERVER / HELPER_KEY env vars)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
