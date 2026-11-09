"""Place resolution (coordinates, maps links, addresses via Nominatim) and IP geolocation."""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

USER_AGENT = "iphone-agent-student-project/1.0"

_NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
_COORD_RE = re.compile(rf"^\s*({_NUM})\s*[, ]\s*({_NUM})\s*$")
_AT_RE = re.compile(rf"@({_NUM}),({_NUM})")


class GeoError(Exception):
    pass


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Return (lat, lon) if `text` is raw coordinates or a Google/Apple Maps link."""
    text = text.strip()
    m = _COORD_RE.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon) if _valid(lat, lon) else None

    url_match = re.search(r"https?://\S+", text)
    if not url_match:
        return None
    url = unquote(url_match.group(0))
    m = _AT_RE.search(url)  # google.com/maps/@37.1,-122.1,17z  or /place/.../@lat,lon
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon) if _valid(lat, lon) else None
    qs = parse_qs(urlparse(url).query)
    for key in ("ll", "q", "query", "sll", "coordinate"):  # apple: ll= / q= ; google: q= / query=
        for value in qs.get(key, []):
            m = _COORD_RE.match(value)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                if _valid(lat, lon):
                    return (lat, lon)
    return None


class Nominatim:
    """OpenStreetMap geocoder. Usage policy: max 1 request/second, identifying User-Agent."""

    URL = "https://nominatim.openstreetmap.org/search"

    def __init__(self, http=None):
        import httpx

        self._http = http or httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT})
        self._lock = threading.Lock()
        self._last = 0.0

    def lookup(self, query: str) -> dict | None:
        with self._lock:
            wait = 1.0 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self._http.get(
                    self.URL,
                    params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
                )
                r.raise_for_status()
                results = r.json()
            except Exception as exc:
                raise GeoError(f"address lookup failed: {exc}") from exc
            finally:
                self._last = time.monotonic()
        if not results:
            return None
        top = results[0]
        return {
            "lat": float(top["lat"]),
            "lon": float(top["lon"]),
            "label": top.get("display_name", query),
            "kind": top.get("addresstype") or top.get("type", ""),
        }


class CensusGeocoder:
    """US Census Bureau geocoder: free, no key, accurate house-number matches for US addresses."""

    URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

    def __init__(self, http=None):
        import httpx

        self._http = http or httpx.Client(timeout=12, headers={"User-Agent": USER_AGENT})

    def lookup(self, query: str) -> dict | None:
        try:
            r = self._http.get(
                self.URL,
                params={"address": query, "benchmark": "Public_AR_Current", "format": "json"},
            )
            r.raise_for_status()
            matches = r.json().get("result", {}).get("addressMatches", [])
        except Exception:
            return None  # fall back to the next geocoder
        if not matches:
            return None
        m = matches[0]
        c = m["coordinates"]
        return {
            "lat": float(c["y"]),
            "lon": float(c["x"]),
            "label": m.get("matchedAddress", query),
            "kind": "house",
        }


_STREET_ADDRESS = re.compile(r"^\s*\d+[A-Za-z]?\s+\S")


class ChainGeocoder:
    """Street addresses ("123 Main St ...") try the Census geocoder first, then OpenStreetMap."""

    def __init__(self, census, osm):
        self._census = census
        self._osm = osm

    def lookup(self, query: str) -> dict | None:
        if _STREET_ADDRESS.match(query):
            hit = self._census.lookup(query)
            if hit:
                return hit
        return self._osm.lookup(query)


class IPLocator:
    """Free IP geolocation via ip-api.com (non-commercial use)."""

    def __init__(self, http=None):
        import httpx

        self._http = http or httpx.Client(timeout=8, headers={"User-Agent": USER_AGENT})

    def locate(self, ip: str) -> dict | None:
        try:
            r = self._http.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,regionName,city,isp,query"},
            )
            d = r.json()
        except Exception:
            return None
        if d.get("status") != "success":
            return None
        return d
