import asyncio

import motion
from location_helper import Driver


class FakePhone:
    def __init__(self, fail_open=0):
        self.is_open = False
        self.fail_open = fail_open
        self.points = []
        self.cleared = 0

    async def open(self):
        if self.fail_open:
            self.fail_open -= 1
            raise ConnectionError("No device connected")
        self.is_open = True

    async def set(self, lat, lng):
        if not self.is_open:
            raise ConnectionError("link lost")
        self.points.append((lat, lng))

    async def clear(self):
        self.cleared += 1

    async def close(self):
        self.is_open = False


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def offline_build(stops, mode, loop=False, log=None):
    return motion.build(stops, mode, seed=1, loop=loop, online=False)


def run(coro):
    return asyncio.run(coro)


def test_set_pins_and_holds():
    phone, clock = FakePhone(), Clock()
    d = Driver(phone, clock=clock)
    ok, detail = run(d.handle({"action": "set", "payload": {"lat": 1.5, "lon": 2.5}}))
    assert ok and phone.points == [(1.5, 2.5)]
    run(d.step())
    assert phone.points == [(1.5, 2.5)]  # unchanged point is not resent


def test_trip_moves_over_time_and_reports_duration():
    phone, clock = FakePhone(), Clock()
    d = Driver(phone, clock=clock, build=offline_build)
    stops = [{"lat": 37.8716, "lng": -122.2727, "name": "A", "dwell_s": 0},
             {"lat": 37.8044, "lng": -122.2712, "name": "B", "dwell_s": 60}]
    ok, detail = run(d.handle({"action": "trip", "payload": {"stops": stops, "mode": "drive", "loop": False}}))
    assert ok and "min" in detail
    start = phone.points[-1]
    assert abs(start[0] - 37.8716) < 0.001
    clock.t += 60
    run(d.step())
    assert phone.points[-1][0] < start[0] - 0.001  # heading south toward B
    clock.t += 10_000  # long after arrival: parked near B
    run(d.step())
    assert abs(phone.points[-1][0] - 37.8044) < 0.001


def test_clear_stops_trip_and_clears_phone():
    phone, clock = FakePhone(), Clock()
    d = Driver(phone, clock=clock)
    run(d.handle({"action": "set", "payload": {"lat": 1.0, "lon": 2.0}}))
    ok, _ = run(d.handle({"action": "clear", "payload": {}}))
    assert ok and phone.cleared == 1
    n = len(phone.points)
    run(d.step())
    assert len(phone.points) == n


def test_phone_missing_reports_failure():
    d = Driver(FakePhone(fail_open=1), clock=Clock())
    ok, detail = run(d.handle({"action": "set", "payload": {"lat": 1.0, "lon": 2.0}}))
    assert not ok and "No device connected" in detail


def test_reconnects_and_reapplies_after_drop():
    phone, clock = FakePhone(), Clock()
    d = Driver(phone, clock=clock)
    run(d.handle({"action": "set", "payload": {"lat": 1.0, "lon": 2.0}}))
    phone.is_open = False  # cable wiggle
    clock.t += 10  # keepalive re-send notices it
    run(d.step())  # fails, closes session
    run(d.step())  # reopens and re-applies
    assert phone.points[-1] == (1.0, 2.0) and len(phone.points) == 2


def test_unknown_action():
    ok, detail = run(Driver(FakePhone(), clock=Clock()).handle({"action": "dance", "payload": {}}))
    assert not ok and "unknown" in detail
