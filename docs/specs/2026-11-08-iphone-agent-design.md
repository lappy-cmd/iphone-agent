# iPhone Personal Agent — Design Spec

Date: 2026-11-08
Status: Approved in chat, implementation started

## 1. Purpose

A personal assistant you control by texting from your iPhone. Its replies show up in
iMessage. It is built for a university project presentation about the weaknesses in
modern location tracking, and it doubles as a day-to-day assistant.

### Goals
- Text it commands from iMessage and get replies in iMessage.
- Read Gmail and summarize the important items; send email from your Gmail, but only after you confirm.
- Text you homework and deadline reminders, plus a morning email digest.
- Generate code, including new agents, and save it to a GitHub repo for review. The agent never runs the code it generates.
- Set the iPhone's reported location (shown in Find My) to an exact address or coordinates, and clear it.
- Show your IP-based location so the VPN effect can be demoed (Proton VPN free tier).

### Constraints
- **$0 running cost.** Gemini free tier, an always-free cloud VM, free DuckDNS + Let's Encrypt for HTTPS,
  free OpenStreetMap Nominatim geocoding, free ip-api.com IP lookup.
- **No paid SMS.** AT&T has shut down its email-to-text gateway, so iPhone Shortcuts automations carry
  messages both ways.
- **Single user.** Every request must carry a secret key.

### Decisions made in chat
| Topic | Decision |
|---|---|
| Messaging channel | iMessage self-thread via Shortcuts automations (free) |
| LLM | Google Gemini free tier (accepted: free-tier data may be used by Google) |
| Location control | Option A: by text, relayed through the cloud to a laptop helper |
| Location precision | Exact address (Nominatim), raw coordinates, or a maps link/pin; always confirm first |
| Actions that need confirmation | Sending email, changing location |
| Morning digest | 7:30 local time |
| Generated code | Pushed to a GitHub repo, never executed by the server |

## 2. Architecture

```
iPhone (Messages + Shortcuts)
  ├─ Automation "Agent In": a message containing "agent:" → POST /message → reply → Send Message to self
  └─ Automation "Agent Out": an email with subject "[agent]" → Send Message to self (proactive texts)

Cloud server (Oracle/Google always-free VM, Python FastAPI behind Caddy HTTPS)
  ├─ Router: parses the command → handler
  ├─ Gemini (llm.py): chat, email summary, email drafting, reminder time parsing, code generation
  ├─ Gmail (gmail.py): list recent/unread mail, send mail, send "[agent]" notification to self
  ├─ Scheduler thread: reminders, day-before heads-ups, morning digest
  ├─ SQLite: reminders, pending confirmation, location command queue, key/value state
  └─ Helper API: /helper/poll, /helper/result (laptop connects out; nothing is exposed on the laptop)

Windows laptop: location helper
  └─ polls the server → runs pymobiledevice3 (developer DVT simulate-location) → reports the result
```

Why each piece:
- **Inbound path:** the Shortcut sends the text to the server and receives the reply in the same HTTP
  response. That makes interactive replies fast and keeps email out of them.
- **Outbound proactive path:** the server cannot start an iMessage by itself. Instead it emails you a
  `[agent]` message, and the Mail-triggered automation on your phone turns that into an iMessage.
- **Loop safety:** every reply starts with `🤖`, and any `agent:` text inside a reply gets an invisible
  zero-width space before the colon. It still reads "agent: yes", but it doesn't match the inbound
  automation's "contains agent:" filter, so neither automation can trigger on its own output.

## 3. Commands

All commands start with `agent:` (the automation filters on it).

| Command | Behavior |
|---|---|
| `put me at <address \| lat, lon \| maps link>` | Resolve the place → ask for confirmation → queue `set` for the helper |
| `real location` | Queue `clear` for the helper (no confirmation needed) |
| `where am i` | Last location set by the agent + IP geolocation of the requesting phone |
| `summarize my email` | Gemini summarizes unread + recent mail (last 2 days, up to 25) |
| `email <addr> saying <what>` | Gemini drafts subject + body → ask for confirmation → send |
| `remind me <task> <when>` | Parse the time → store → reminder at that time (+ heads-up 24h before if due >24h away) |
| `reminders` | List upcoming reminders with their ids |
| `cancel reminder <id>` | Delete the reminder |
| `make me an agent that …` / `write code that …` | Gemini generates files → pushed to GitHub → reply with link |
| `yes` / `no` | Confirm or cancel the single pending action |
| `help` | List the commands |
| anything else | General Gemini answer |

Confirmations: there is one pending slot. A newer action replaces it, and it expires after 10 minutes.

## 4. Location

- **Geocoding:** Nominatim `/search` (1 request/sec, generic User-Agent `iphone-agent-student-project`).
  Accepts `lat, lon` text, Google Maps links (`@lat,lon` / `q=lat,lon`) and Apple Maps links (`ll=`/`q=`).
- **Helper:** long-polls `/helper/poll` every 3 seconds and sends a heartbeat. The server considers it offline
  after 30 seconds without a poll and says so.
- **Setting the location:** the helper keeps `pymobiledevice3 developer dvt simulate-location set -- LAT LON` running
  as a subprocess. Location simulation over DVT can end when the connection closes, so the process is kept alive.
  A new `set` kills the old process first. `clear` kills it and runs `... simulate-location clear`.
- **Prerequisites:** Developer Mode on the iPhone, USB pairing once, and `pymobiledevice3 remote tunneld` running as
  Administrator (iOS 17+ needs a tunnel). The helper reports pymobiledevice3 errors verbatim.
- **Safety:** turn off Developer Mode after the demo. A phone reboot also clears simulation. Install
  pymobiledevice3 only from PyPI.

## 5. Error handling

Every failure produces a `🤖` text and is never silent:
- Helper offline → "Laptop helper is offline…"
- Helper command failure → pymobiledevice3 stderr summarized and forwarded
- Gemini error or quota → "Gemini limit hit / unavailable, try later". Reminders still fire (no Gemini needed).
  Reminder time parsing falls back to `dateparser`.
- Gmail auth failure → "Gmail sign-in expired; re-run scripts/gmail_auth.py"
- Geocode miss → ask for coordinates or a maps pin
- GitHub push failure → the code summary is still texted, along with the error
- A wrong or missing key → HTTP 401, no reply text

## 6. Security

- `AGENT_KEY` (shared with the Shortcut) and `HELPER_KEY` (shared with the laptop) are long random secrets, compared
  in constant time.
- Server traffic is HTTPS-only through Caddy + DuckDNS. The app listens only on localhost.
- Secrets live in `.env` and `token.json` and are git-ignored.
- Gmail scopes: `gmail.readonly` and `gmail.send` only.
- Generated code is never executed. The GitHub token is fine-grained and limited to one repo.

## 7. Testing

- pytest unit tests with fakes for Gemini, Gmail, the geocoder, GitHub, the IP lookup and the clock. They cover:
  routing, confirmations (including expiry), reminders + heads-up + digest scheduling, the helper queue + offline
  detection, maps-link/coordinate parsing, loop-safety sanitizing, and API auth.
- Helper `--dry-run` mode logs the commands without touching the phone.
- Pre-presentation live checklist in `docs/SETUP.md`.

## 8. Out of scope
- Paid SMS, real iMessage APIs, and multiple users.
- Executing generated code.
- Routing the agent's own traffic through a VPN. The VPN is used manually on the phone for the demo.
