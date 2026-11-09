# iPhone Agent

A personal assistant you text from iMessage. It summarizes and sends Gmail, texts you reminders and a
morning digest, writes code (including new agents) into a GitHub repo, and, for a university project on
location-tracking weaknesses, sets the iPhone's reported location (visible in Find My) to an exact address.

Runs for $0: Gemini free tier, an Oracle Always Free VM, DuckDNS + Caddy HTTPS, OpenStreetMap geocoding, and
iPhone Shortcuts for messaging. No paid SMS.

```
iPhone Shortcuts ──"agent: ..."──► cloud server (FastAPI + Gemini + Gmail + scheduler)
        ◄── reply / [agent] email ──┘           ▲
                                                │ polls (outbound only)
                          Windows laptop: location_helper.py → pymobiledevice3 → iPhone (USB)
```

- Design: [docs/specs/2026-11-08-iphone-agent-design.md](docs/specs/2026-11-08-iphone-agent-design.md)
- Setup: [docs/SETUP.md](docs/SETUP.md)
- Commands: text `agent: help`

## Development

```powershell
cd server
python -m venv .venv; .\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
```

## What the demo shows (for the presentation)

- **Find My trusts the location the phone reports.** A developer feature over USB can make the phone report
  any coordinates, and every app on the phone, including Find My, believes it. There is no attestation that a
  location came from real GPS.
- **IP location is easy to change.** A free VPN moves your apparent location to another city or country.
- **Exact-address precision.** Faking a location isn't limited to the right city; it can point at a specific
  house. That's why a location alone shouldn't be treated as proof of where someone is.
