"""One tunnel + one location-simulation channel to the iPhone, all in-process (no admin, no tunneld).

Adapted from whereverwhenever's Session (MIT, https://github.com/angadsingharora/whereverwhenever).
Over USB by default. With `host` set (the phone's IP, e.g. its Tailscale 100.x address) it connects over
the network instead, using the trust pairing made over USB. The phone honours the fake location only
while this channel stays open.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack

log = logging.getLogger("helper")


def _network_provider(host: str):
    """Replacement for pymobiledevice3's USB tunnel-provider lookup: lockdown over TCP to `host`."""

    async def provider(serial, autopair, remotepairing_fallback=True):
        from pymobiledevice3.lockdown import create_using_tcp
        from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy

        lockdown = await create_using_tcp(host, identifier=serial, autopair=False, keep_alive=True)
        try:
            return await CoreDeviceTunnelProxy.create(lockdown), lockdown
        except BaseException:
            await lockdown.close()
            raise

    return provider


async def enable_network_access(udid: str | None = None) -> str:
    """Over USB: allow this computer to reach the phone over the network. Returns the phone's UDID."""
    from pymobiledevice3.lockdown import create_using_usbmux

    lockdown = await create_using_usbmux(serial=udid)
    try:
        await lockdown.set_enable_wifi_connections(True)
        return lockdown.udid
    finally:
        await lockdown.close()


class PhoneSession:
    def __init__(self, udid: str | None = None, host: str | None = None):
        if host and not udid:
            raise ValueError("connecting over the network needs the phone's UDID (run --setup-remote)")
        self.udid = udid
        self.host = host
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
            userspace_tunnel._create_no_root_tunnel_provider = _network_provider(self.host)
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
