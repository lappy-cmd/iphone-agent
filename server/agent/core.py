"""The agent: routes texted commands to handlers, runs scheduled jobs, talks to the laptop helper."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import DB
from .geo import GeoError, parse_coordinates
from .github import GitHubError
from .gmail import GmailAuthError, GmailError
from .llm import LLMError

log = logging.getLogger("agent")

PREFIX = "🤖 "
HELPER_COMMAND_MAX_AGE = timedelta(minutes=2)
DIGEST_WINDOW = timedelta(hours=2)
EXACT_PLACE_KINDS = {"house", "building", "residential", "apartments", "house_number", "place"}

HELP = """Commands (start each with "agent:"):
put me at <address | lat, lon | maps link>
real location
where am i
summarize my email
email <address> saying <what to say>
remind me <task> <when>
reminders / cancel reminder <id>
make me an agent that <does something>
yes / no (confirm the pending action)
Anything else = ask me a question."""

_R = lambda p: re.compile(p, re.IGNORECASE | re.DOTALL)  # noqa: E731
RE_PREFIX = _R(r"^\s*agent\s*[:\-,]?\s*")
RE_YES = _R(r"^(yes|y|yep|yeah|confirm|send it|do it|ok|okay)[.!]*$")
RE_NO = _R(r"^(no|n|nope|cancel|stop|don'?t)[.!]*$")
RE_HELP = _R(r"^(help|commands|\?)$")
RE_CLEAR_LOC = _R(r"^((my )?(real|actual|true) location|(clear|reset|stop) (my )?(fake )?location)[.!]*$")
RE_WHERE = _R(r"^where am i\??$")
RE_SET_LOC = _R(r"^(?:put|set|move|send|teleport|place) me (?:at|to|in) (.+)$|^set (?:my )?location (?:to |at )?(.+)$")
RE_SUMMARY = _R(r"^(summari[sz]e|check|read|go through)\b.*\b(e-?mails?|inbox|mail)\b")
RE_EMAIL = _R(r"^(?:send )?(?:an )?e-?mail (?:to )?(\S+@\S+?)[,:]? (?:saying|that says|that|about|to say|telling (?:them|him|her))\s*(.+)$")
RE_REMIND = _R(r"^remind me (?:to |about |that )?(.+)$")
RE_LIST_REM = _R(r"^((list|show) (my )?)?reminders\??$")
RE_CANCEL_REM = _R(r"^(?:cancel|delete|remove) reminder #?(\d+)$")
RE_CODE = _R(r"^(?:make|build|create|write|generate|code)\b.*\b(agent|bot|script|program|app|code|tool|website|site|function|game)\b")


ZWSP = "​"  # zero-width space: looks like "agent:" but doesn't match "contains agent:"


def sanitize(text: str) -> str:
    """Prefix replies and make sure they can never re-trigger the 'agent:' automation."""
    text = re.sub(r"(?i)(agent)\s*:", lambda m: m.group(1) + ZWSP + ":", text.strip())
    return text if text.startswith(PREFIX) else PREFIX + text


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _threaded(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class Agent:
    def __init__(
        self,
        settings: Settings,
        db: DB,
        llm=None,
        gmail=None,
        geocoder=None,
        iplocator=None,
        github=None,
        clock: Callable[[], datetime] = utcnow,
        background: Callable[[Callable[[], None]], None] = _threaded,
    ):
        self.s = settings
        self.db = db
        self.llm = llm
        self.gmail = gmail
        self.geocoder = geocoder
        self.iplocator = iplocator
        self.github = github
        self.clock = clock
        self.background = background
        self.tz = ZoneInfo(settings.timezone)

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.clock()

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def fmt(self, dt: datetime) -> str:
        """e.g. 'Fri Nov 13 9:00 AM' (portable: no %-d, which Windows lacks)."""
        loc = self.local(dt)
        return f"{loc:%a %b} {loc.day} {loc.strftime('%I:%M %p').lstrip('0')}"

    def notify(self, text: str) -> None:
        """Proactive text: email to self -> iPhone automation -> iMessage."""
        if self.gmail is None:
            log.warning("notify without gmail: %s", text)
            return
        try:
            self.gmail.notify_self(sanitize(text))
        except Exception:  # never let a notification failure kill the scheduler
            log.exception("notify failed")

    def helper_online(self) -> bool:
        seen = self.db.get("helper_last_seen")
        if not seen:
            return False
        return self.now() - datetime.fromisoformat(seen) < timedelta(
            seconds=self.s.helper_offline_seconds
        )

    # ------------------------------------------------------------------ entry point
    def handle(self, raw: str, client_ip: str | None = None) -> str:
        text = RE_PREFIX.sub("", raw or "", count=1).strip()
        try:
            return sanitize(self._route(text, client_ip))
        except LLMError as exc:
            log.warning("gemini error: %s", exc)
            return sanitize("Gemini is unavailable or the free daily limit was hit. Try again later.")
        except GmailAuthError:
            return sanitize("Gmail sign-in expired. Re-run scripts/gmail_auth.py and copy token.json to the server.")
        except (GmailError, GeoError, GitHubError) as exc:
            return sanitize(f"That failed: {exc}")
        except Exception as exc:
            log.exception("handler crashed")
            return sanitize(f"Something broke ({type(exc).__name__}): {exc}")

    def _route(self, text: str, client_ip: str | None) -> str:
        if not text or RE_HELP.match(text):
            return HELP
        if RE_YES.match(text):
            return self._confirm(True)
        if RE_NO.match(text):
            return self._confirm(False)
        if RE_CLEAR_LOC.match(text):
            return self._clear_location()
        if RE_WHERE.match(text):
            return self._where(client_ip)
        if m := RE_SET_LOC.match(text):
            return self._set_location((m.group(1) or m.group(2)).strip())
        if RE_SUMMARY.match(text):
            return self._summary()
        if m := RE_EMAIL.match(text):
            return self._email(m.group(1), m.group(2).strip())
        if RE_LIST_REM.match(text):
            return self._list_reminders()
        if m := RE_CANCEL_REM.match(text):
            return self._cancel_reminder(int(m.group(1)))
        if m := RE_REMIND.match(text):
            return self._remind(m.group(1).strip())
        if RE_CODE.match(text):
            return self._codegen(text)
        return self._require(self.llm, "Gemini").chat(text)

    @staticmethod
    def _require(service, name: str):
        if service is None:
            raise RuntimeError(f"{name} is not configured on the server")
        return service

    # ------------------------------------------------------------------ confirmations
    def _ask_confirm(self, kind: str, payload: dict) -> None:
        self.db.set_pending(
            kind, payload, self.now() + timedelta(minutes=self.s.confirm_ttl_minutes)
        )

    def _confirm(self, yes: bool) -> str:
        pending = self.db.pop_pending(self.now())
        if pending is None:
            return "Nothing is waiting for confirmation (confirmations expire after %d min)." % (
                self.s.confirm_ttl_minutes
            )
        if not yes:
            return "Cancelled."
        p = pending["payload"]
        if pending["kind"] == "email":
            self._require(self.gmail, "Gmail").send(p["to"], p["subject"], p["body"])
            return f"Sent to {p['to']}."
        if pending["kind"] == "location":
            if not self.helper_online():
                return self._helper_offline_msg()
            self.db.queue_helper_command("set", p, self.now())
            return f"Moving your phone to {p['label']}... I'll text you when it's set."
        return "Unknown pending action; cancelled."

    # ------------------------------------------------------------------ location
    def _helper_offline_msg(self) -> str:
        return (
            "Laptop helper is offline, so I can't change your location. Turn on the laptop, "
            "plug in / pair your phone, and start location_helper.py."
        )

    def _set_location(self, place: str) -> str:
        coords = parse_coordinates(place)
        warning = ""
        if coords:
            lat, lon = coords
            label = f"{lat:.6f}, {lon:.6f}"
        else:
            hit = self._require(self.geocoder, "Geocoder").lookup(place)
            if hit is None:
                return (
                    f"Couldn't find \"{place}\". Send exact coordinates instead: long-press the spot "
                    "in Apple/Google Maps, copy the coordinates or share link, and text "
                    "\"agent: put me at <that>\"."
                )
            lat, lon, label = hit["lat"], hit["lon"], hit["label"]
            if hit.get("kind") and hit["kind"] not in EXACT_PLACE_KINDS:
                warning = (
                    f"\nNote: this matched a {hit['kind']}, not an exact house. For the exact spot, "
                    "send a maps pin or coordinates."
                )
        if not self.helper_online():
            return self._helper_offline_msg()
        self._ask_confirm("location", {"lat": lat, "lon": lon, "label": label})
        return (
            f"Found: {label}\n({lat:.6f}, {lon:.6f}){warning}\n"
            f"Set your location there? Reply \"agent: yes\" or \"agent: no\"."
        )

    def _clear_location(self) -> str:
        if not self.helper_online():
            return self._helper_offline_msg()
        self.db.queue_helper_command("clear", {}, self.now())
        return "Restoring your real location... I'll text you when it's done."

    def _where(self, client_ip: str | None) -> str:
        last = self.db.get("last_location")
        lines = [
            f"Phone location set by me: {last['label']} ({last['lat']:.5f}, {last['lon']:.5f})"
            if last
            else "Phone location: real (I haven't set a fake one)."
        ]
        if client_ip and self.iplocator:
            info = self.iplocator.locate(client_ip)
            if info:
                lines.append(
                    f"Your IP location: {info.get('city')}, {info.get('regionName')}, "
                    f"{info.get('country')} (IP {info.get('query')}, {info.get('isp')})"
                )
            else:
                lines.append(f"Your IP ({client_ip}) couldn't be located.")
        lines.append("Turn Proton VPN on/off and ask again to compare.")
        return "\n".join(lines)

    # ---- laptop helper API ----
    def helper_poll(self) -> dict | None:
        self.db.set("helper_last_seen", self.now().isoformat())
        while (cmd := self.db.next_helper_command()) is not None:
            created = self.db.helper_command_created(cmd["id"])
            if self.now() - created > HELPER_COMMAND_MAX_AGE:
                self.db.finish_helper_command(cmd["id"], False, "expired before helper picked it up")
                continue
            return cmd
        return None

    def helper_result(self, cid: int, ok: bool, detail: str) -> None:
        cmd = self.db.finish_helper_command(cid, ok, detail)
        if cmd is None:
            return
        if cmd["action"] == "set":
            if ok:
                self.db.set("last_location", cmd["payload"])
                self.notify(f"Location set: {cmd['payload']['label']}. Check Find My to see it.")
            else:
                self.notify(f"Couldn't set location: {detail}")
        elif cmd["action"] == "clear":
            if ok:
                self.db.set("last_location", None)
                self.notify("Real location restored.")
            else:
                self.notify(f"Couldn't clear location: {detail}. Restarting the phone also clears it.")

    # ------------------------------------------------------------------ email
    def _summary(self) -> str:
        emails = self._require(self.gmail, "Gmail").recent()
        return self._require(self.llm, "Gemini").summarize_emails(emails)

    def _email(self, to: str, instruction: str) -> str:
        draft = self._require(self.llm, "Gemini").draft_email(to, instruction)
        self._ask_confirm("email", {"to": to, **draft})
        return (
            f"Draft to {to}\nSubject: {draft['subject']}\n\n{draft['body']}\n\n"
            f"Send it? Reply \"agent: yes\" or \"agent: no\"."
        )

    # ------------------------------------------------------------------ reminders
    def _parse_when(self, text: str) -> tuple[str, datetime]:
        now_local = self.local(self.now())
        try:
            parsed = self._require(self.llm, "Gemini").parse_reminder(text, now_local)
            task, due_local = parsed["task"], parsed["due_local"]
        except (LLMError, RuntimeError):
            task, due_local = self._dateparser_fallback(text, now_local)
        if due_local.tzinfo is None:
            due_local = due_local.replace(tzinfo=self.tz)
        return task, due_local.astimezone(timezone.utc)

    def _dateparser_fallback(self, text: str, now_local: datetime) -> tuple[str, datetime]:
        from dateparser.search import search_dates

        found = search_dates(
            text,
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now_local.replace(tzinfo=None),
            },
        )
        if not found:
            raise ValueError("no date found")
        phrase, when = found[-1]
        task = text.replace(phrase, "").strip(" ,.-") or text
        return task, when

    def _remind(self, text: str) -> str:
        try:
            task, due = self._parse_when(text)
        except ValueError:
            return 'I couldn\'t tell when. Try: "agent: remind me bio essay Friday 9am".'
        now = self.now()
        if due <= now:
            return f"That time ({self.fmt(due)}) is already past. Give me a future time."
        heads_up = None
        if self.s.heads_up_hours and due - now > timedelta(hours=self.s.heads_up_hours):
            heads_up = due - timedelta(hours=self.s.heads_up_hours)
        rid = self.db.add_reminder(task, due, heads_up)
        extra = f" (heads-up {self.fmt(heads_up)})" if heads_up else ""
        return f"Reminder #{rid} set: {task}, {self.fmt(due)}{extra}."

    def _list_reminders(self) -> str:
        items = self.db.upcoming_reminders()
        if not items:
            return "No upcoming reminders."
        return "Upcoming:\n" + "\n".join(
            f"#{r['id']} {self.fmt(r['due_at'])}: {r['text']}" for r in items
        )

    def _cancel_reminder(self, rid: int) -> str:
        return f"Reminder #{rid} cancelled." if self.db.delete_reminder(rid) else f"No upcoming reminder #{rid}."

    # ------------------------------------------------------------------ code generation
    def _codegen(self, request: str) -> str:
        llm = self._require(self.llm, "Gemini")

        def work() -> None:
            try:
                project = llm.generate_code(request)
            except LLMError as exc:
                self.notify(f"Code generation failed (Gemini): {exc}")
                return
            folder = f"{self.local(self.now()).strftime('%Y%m%d-%H%M')}-{project['name']}"
            names = ", ".join(f["path"] for f in project["files"][:8])
            if self.github is None:
                self.notify(
                    f"Wrote {project['name']} ({names}) but GitHub isn't configured, so I couldn't "
                    "save it. Set GITHUB_TOKEN and GITHUB_REPO."
                )
                return
            try:
                url = self.github.push_project(folder, project["files"], f"agent: {project['name']}")
            except GitHubError as exc:
                self.notify(f"Wrote {project['name']} but pushing to GitHub failed: {exc}")
                return
            self.notify(f"Done: {project['summary']}\nFiles: {names}\n{url}\nReview it before running it.")

        self.background(work)
        return "Working on it. I'll text you the GitHub link when the code is ready."

    # ------------------------------------------------------------------ scheduler
    def tick(self) -> None:
        """Called every ~30s by the scheduler thread."""
        now = self.now()
        for r in self.db.due_heads_ups(now):
            self.db.mark_heads_up_sent(r["id"])
            self.notify(f"Heads-up: {r['text']} is due {self.fmt(r['due_at'])}.")
        for r in self.db.due_reminders(now):
            self.db.mark_reminder_sent(r["id"])
            self.notify(f"Reminder: {r['text']}")
        self._maybe_digest(now)

    def _maybe_digest(self, now: datetime) -> None:
        if not self.s.digest_time or self.gmail is None or self.llm is None:
            return
        local = self.local(now)
        hh, mm = (int(x) for x in self.s.digest_time.split(":"))
        target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        today = local.date().isoformat()
        if not (target <= local < target + DIGEST_WINDOW):
            return
        if self.db.get("last_digest") == today:
            return
        self.db.set("last_digest", today)
        try:
            summary = self.llm.summarize_emails(self.gmail.recent())
            self.notify("Morning email digest:\n" + summary)
        except Exception as exc:
            log.warning("digest failed: %s", exc)
            self.notify(f"Morning digest failed: {exc}")
