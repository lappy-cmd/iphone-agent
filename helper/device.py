"""One tunnel + one location-simulation channel to the iPhone, all in-process (no admin, no tunneld).

Adapted from whereverwhenever's Session (MIT, https://github.com/angadsingharora/whereverwhenever).
Over USB by default. With `host` set (the phone's IP, e.g. its Tailscale 100.x address) it connects over
the network through Apple's wireless developer pairing (RemotePairing, the kind Xcode uses), which
`--setup-remote` creates once over USB. The phone honours the fake location only while this channel
stays open.

Verified 2026-11-12 on iOS 26.7.3: the lockdown/CoreDeviceProxy path is refused over Wi-Fi and over
Tailscale, but RemotePairing on port 49152 works over both.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack

log = logging.getLogger("helper")


DEFAULT_PORT = 49152  # where the phone's RemotePairing service listened when tested


def _network_provider(host: str, port: int):
    """Replacement for pymobiledevice3's USB tunnel-provider lookup: RemotePairing straight to host:port."""

    async def provider(serial, autopair, remotepairing_fallback=True):
        from pymobiledevice3.remote.tunnel_service import create_core_device_tunnel_service_using_remotepairing

        return await create_core_device_tunnel_service_using_remotepairing(serial, host, port), None

    return provider


def _has_wireless_pairing(udid: str) -> bool:
    from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
    from pymobiledevice3.remote import tunnel_service as ts

    return any(ident == udid and rec.get(ts.PEER_ALT_IRK_KEY) is not None
               for ident, _path, rec in iter_remote_pair_records_by_identifier())


async def enable_network_access(udid: str | None = None) -> tuple[str, int]:
    """Over USB, once: create the wireless developer pairing (no prompt on the phone) and find the
    port its RemotePairing service listens on. Returns (udid, port)."""
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote import tunnel_service as ts

    lockdown = await create_using_usbmux(serial=udid)
    try:
        udid = lockdown.udid
        if not _has_wireless_pairing(udid):
            svc = await ts.RemotePairingLockdownService.create(lockdown)
            try:
                await svc.connect(autopair=True)
            except RemotePairingCompletedError:
                pass  # the phone hangs up once the pairing is written: that's success
            finally:
                await svc.close()
    finally:
        await lockdown.close()
    if not _has_wireless_pairing(udid):
        raise RuntimeError("wireless pairing wasn't saved; unlock the phone and try again")
    port = DEFAULT_PORT
    services = await ts.get_remote_pairing_tunnel_services(bonjour_timeout=6, udid=udid)
    if services:
        port = services[0].port
    for s in services:
        await s.close()
    return udid, port


class PhoneSession:
    def __init__(self, udid: str | None = None, host: str | None = None, port: int = DEFAULT_PORT):
        if host and not udid:
            raise ValueError("connecting over the network needs the phone's UDID (run --setup-remote)")
        self.udid = udid
        self.host = host
        self.port = port
        self.stack: AsyncExitStack | None = None
        self.loc = None

    async def open(self) -> None:
        from pymobiledevice3.exceptions import AlreadyMountedError
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import auto_mount

        await self.close()
        self.stack = AsyncExitStack()
        if self.host:
            from pymobiledevice3.remote import userspace_tunnel

            # One tunnel per process, so swapping the module's provider lookup is safe here.
            userspace_tunnel._create_no_root_tunnel_provider = _network_provider(self.host, self.port)
            log.info("connecting to the iPhone over the network at %s...", self.host)
        else:
            log.info("connecting to the iPhone over USB...")
        rsd = await self.stack.enter_async_context(UserspaceRsdTunnel(serial=self.udid))
        log.info("connected: %s iOS %s", rsd.product_type, rsd.product_version)
        try:
            dvt = await self.stack.enter_async_context(DvtProvider(rsd))
        except Exception as e:
            # Developer services need Apple's developer disk image; first time it's downloaded (~1 min).
            log.info("mounting the developer disk image (%s)...", type(e).__name__)
            try:
                await auto_mount(rsd)
            except AlreadyMountedError:
                raise e
            dvt = await self.stack.enter_async_context(DvtProvider(rsd))
        self.loc = await self.stack.enter_async_context(LocationSimulation(dvt))
        log.info("location channel open")

    async def set(self, lat: float, lng: float) -> None:
        await self.loc.set(lat, lng)

    async def clear(self) -> None:
        if self.loc is None:
            await self.open()
        await self.loc.clear()

    async def close(self) -> None:
        if self.stack:
            try:
                await self.stack.aclose()
            except Exception:
                pass
        self.stack = self.loc = None


class DryRunSession:
    """Logs instead of touching the phone."""

    async def open(self) -> None:
        log.info("(dry run) open")

    async def set(self, lat: float, lng: float) -> None:
        log.info("(dry run) location %.6f, %.6f", lat, lng)

    async def clear(self) -> None:
        log.info("(dry run) clear")

    async def close(self) -> None:
        pass
