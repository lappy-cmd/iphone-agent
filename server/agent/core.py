"""The agent: a Gemini conversation with tools, scheduled jobs, and the laptop helper queue."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import DB
from .geo import GeoError, parse_coordinates
from .github import GitHubError
from .gmail import GmailAuthError, GmailError
from .llm import LLMError
from .persona import TOOLS, is_ours, mark, system_prompt

log = logging.getLogger("agent")

HELPER_COMMAND_MAX_AGE = timedelta(minutes=2)
DIGEST_WINDOW = timedelta(hours=2)
EXACT_PLACE_KINDS = {"house", "building", "residential", "apartments", "house_number", "place"}
RE_PREFIX = re.compile(r"^\s*agent\s*[:\-,]?\s*", re.IGNORECASE)  # old habit; harmless to strip
RE_EMAIL_ADDR = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STYLE_SAMPLE = 30

NO_BRAIN = (
    "Sorry, my AI brain is unavailable right now (Gemini's free daily limit or an outage). "
    "Try again in a bit."
)


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
        sleep: Callable[[float], None] = time.sleep,
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
        self.sleep = sleep
        self.tz = ZoneInfo(settings.timezone)
        self._lock = threading.Lock()
        self._awaiting: set[int] = set()  # helper commands a /message request is waiting on

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
        """Proactive text: through Google Voice when set up, else email -> 'Agent Out' -> iMessage."""
        if self.gmail is None:
            log.warning("notify without gmail: %s", text)
            return
        try:
            if self.voice_enabled() and self._voice_send(text):
                return
            self.gmail.notify_self(mark(text))
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
        """iPhone Shortcut path: one incoming text -> one marked reply ('' means: send nothing)."""
        if is_ours(raw):
            return ""  # our own reply/notification echoing back through the self-chat
        return mark(self.reply(raw, client_ip))

    def reply(self, raw: str, client_ip: str | None = None) -> str:
        """The assistant's answer to one text ('' for an empty text)."""
        text = RE_PREFIX.sub("", raw or "", count=1).strip()
        if not text:
            return ""
        if self.llm is None:
            return "My AI brain isn't set up yet (GEMINI_API_KEY is missing on the server)."

        turn = int(self.db.get("turn", 0)) + 1
        self.db.set("turn", turn)
        self.db.add_message("user", text, self.now())
        ctx = {"turn": turn, "client_ip": client_ip}
        try:
            reply = self.llm.converse(
                self._system(),
                self.db.recent_messages(self.s.history_messages),
                TOOLS,
                lambda name, args: self._run_tool(name, args, ctx),
            )
        except LLMError as exc:
            log.warning("gemini error: %s", exc)
            return NO_BRAIN
        self.db.add_message("model", reply, self.now())
        self._maybe_learn_style()
        return reply

    # ------------------------------------------------------------------ Google Voice texting
    def voice_enabled(self) -> bool:
        return bool(self.s.owner_phone and self.gmail is not None)

    def poll_voice(self) -> None:
        """Answer new Google Voice texts (forwarded to Gmail) from the owner's number."""
        if not self.voice_enabled():
            return
        since = self.db.get("voice_since")
        if since is None:  # first run: don't answer texts from before the server started
            since = int(self.now().timestamp())
            self.db.set("voice_since", since)
        for m in self.gmail.voice_messages(since - 60):
            if m["internal_ms"] < since * 1000 or not self.db.mark_seen(m["id"]):
                continue
            if not self._from_owner(m):
                log.info("ignoring a Google Voice text from a number that isn't OWNER_PHONE")
                continue
            self.db.set(
                "voice_thread",
                {
                    "to": parseaddr(m["from"])[1],
                    "subject": m["subject"],
                    "thread_id": m["thread_id"],
                    "message_id": m["message_id"],
                },
            )
            answer = self.reply(m["text"])
            if answer:
                self._voice_send(answer)

    def _from_owner(self, m: dict) -> bool:
        owner = re.sub(r"\D", "", self.s.owner_phone)[-10:]
        return bool(owner) and owner in re.sub(r"\D", "", m["from"] + " " + m["subject"])

    def _voice_send(self, text: str) -> bool:
        thread = self.db.get("voice_thread")
        if not thread:
            return False
        self.gmail.voice_reply(
            thread["to"], thread["subject"], text, thread["thread_id"], thread["message_id"]
        )
        return True

    def _system(self) -> str:
        pending = self.db.peek_pending(self.now())
        if pending:
            pending = {"kind": pending["kind"], "summary": pending["payload"].get("summary", "")}
        return system_prompt(
            now_local=self.local(self.now()).strftime("%A %Y-%m-%d %H:%M"),
            timezone=self.s.timezone,
            helper_online=self.helper_online(),
            pending=pending,
            style=self.db.get("style_profile", ""),
        )

    def _maybe_learn_style(self) -> None:
        count = int(self.db.get("style_count", 0)) + 1
        self.db.set("style_count", count)
        if count % self.s.style_every:
            return
        llm = self.llm

        def work() -> None:
            try:
                msgs = [m["text"] for m in self.db.recent_messages(STYLE_SAMPLE, role="user")]
                profile = llm.style_profile(msgs, self.db.get("style_profile", ""))
                self.db.set("style_profile", profile.strip()[:800])
            except Exception as exc:
                log.warning("style update failed: %s", exc)

        self.background(work)

    # ------------------------------------------------------------------ tools
    def _run_tool(self, name: str, args: dict, ctx: dict) -> dict:
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        log.info("tool %s %s", name, {k: v for k, v in args.items() if k != "body"})
        try:
            return fn(ctx, **args)
        except TypeError as exc:
            return {"error": f"bad arguments: {exc}"}
        except GmailAuthError:
            return {
                "error": "Gmail sign-in expired. The owner needs to re-run scripts/gmail_auth.py "
                "and paste the new token.json into GMAIL_TOKEN_JSON on Render."
            }
        except (GmailError, GeoError, GitHubError, LLMError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            log.exception("tool %s crashed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _helper_offline(self) -> dict:
        return {
            "error": "The laptop helper is offline, so the location can't change. Turn on the "
            "laptop, plug in the phone, and start the tunnel and location_helper.py."
        }

    def _prepare(self, kind: str, payload: dict, ctx: dict) -> None:
        payload = {**payload, "turn": ctx["turn"]}
        self.db.set_pending(kind, payload, self.now() + timedelta(minutes=self.s.confirm_ttl_minutes))

    # ---- location
    def _tool_set_location(self, ctx: dict, place: str) -> dict:
        coords = parse_coordinates(place)
        approximate = None
        if coords:
            lat, lon = coords
            label = f"{lat:.6f}, {lon:.6f}"
        else:
            if self.geocoder is None:
                return {"error": "no geocoder configured"}
            hit = self.geocoder.lookup(place)
            if hit is None:
                return {
                    "error": f"Couldn't find '{place}'. For an exact spot, the user can long-press "
                    "it in Apple/Google Maps and send the coordinates or the share link."
                }
            lat, lon, label = hit["lat"], hit["lon"], hit["label"]
            if hit.get("kind") and hit["kind"] not in EXACT_PLACE_KINDS:
                approximate = (
                    f"This matched a {hit['kind']}, not an exact building, so it may be off. "
                    "Coordinates or a maps pin are exact."
                )
        if not self.helper_online():
            return self._helper_offline()
        self._prepare("location", {"lat": lat, "lon": lon, "label": label, "summary": label}, ctx)
        out = {"status": "prepared, ask the user to confirm", "place": label, "lat": lat, "lon": lon}
        if approximate:
            out["warning"] = approximate
        return out

    def _tool_clear_location(self, ctx: dict) -> dict:
        if not self.helper_online():
            return self._helper_offline()
        return self._run_helper("clear", {})

    def _run_helper(self, action: str, payload: dict) -> dict:
        """Queue a laptop command and wait briefly so the reply can say how it went."""
        cid = self.db.queue_helper_command(action, payload, self.now())
        with self._lock:
            self._awaiting.add(cid)
        for _ in range(max(self.s.location_wait_seconds, 0)):
            self.sleep(1)
            cmd = self.db.get_helper_command(cid)
            if cmd and cmd["status"] in ("done", "failed"):
                break
        with self._lock:
            self._awaiting.discard(cid)
            cmd = self.db.get_helper_command(cid) or {"status": "queued", "detail": ""}
        if cmd["status"] == "done":
            return {"status": "done", "place": payload.get("label", "real location")}
        if cmd["status"] == "failed":
            return {"status": "failed", "detail": cmd["detail"]}
        return {
            "status": "still working",
            "note": "The laptop is taking longer than usual; the user gets a text when it's done.",
        }

    def _tool_where_am_i(self, ctx: dict) -> dict:
        last = self.db.get("last_location")
        out: dict = {
            "fake_location": (
                {"place": last["label"], "lat": last["lat"], "lon": last["lon"]} if last else None
            ),
            "note": "fake_location null means the phone shows its real location.",
        }
        ip = ctx.get("client_ip")
        if ip and self.iplocator:
            info = self.iplocator.locate(ip)
            out["ip_location"] = (
                {k: info.get(k) for k in ("city", "regionName", "country", "isp", "query")}
                if info
                else f"IP {ip} couldn't be located"
            )
        return out

    # ---- email
    def _tool_check_email(self, ctx: dict) -> dict:
        if self.gmail is None:
            return {"error": "Gmail isn't configured on the server"}
        emails = self.gmail.recent()[:15]
        return {
            "emails": [
                {k: e.get(k) for k in ("from", "subject", "date", "snippet", "unread")}
                for e in emails
            ]
        }

    def _tool_draft_email(self, ctx: dict, to: str, subject: str, body: str) -> dict:
        to = to.strip()
        if not RE_EMAIL_ADDR.match(to):
            return {"error": f"'{to}' isn't an email address. Ask the user for it."}
        if self.gmail is None:
            return {"error": "Gmail isn't configured on the server"}
        self._prepare(
            "email", {"to": to, "subject": subject, "body": body, "summary": f"email to {to}"}, ctx
        )
        return {"status": "prepared, show the draft and ask the user to confirm"}

    # ---- confirmations (enforced here, not just by the prompt)
    def _tool_confirm_pending(self, ctx: dict) -> dict:
        pending = self.db.peek_pending(self.now())
        if pending is None:
            return {"error": "Nothing is waiting for confirmation (it may have expired)."}
        p = pending["payload"]
        if p.get("turn") == ctx["turn"]:
            return {
                "error": "Not confirmed yet. Show the user the details and ask; they must agree in "
                "their next message."
            }
        self.db.pop_pending(self.now())
        if pending["kind"] == "email":
            self.gmail.send(p["to"], p["subject"], p["body"])
            return {"status": "sent", "to": p["to"]}
        if pending["kind"] == "location":
            if not self.helper_online():
                return self._helper_offline()
            return self._run_helper("set", {"lat": p["lat"], "lon": p["lon"], "label": p["label"]})
        return {"error": "unknown pending action"}

    def _tool_cancel_pending(self, ctx: dict) -> dict:
        return {"status": "cancelled" if self.db.pop_pending(self.now()) else "nothing was pending"}

    # ---- reminders
    def _tool_add_reminder(self, ctx: dict, task: str, due_local: str) -> dict:
        try:
            due = datetime.fromisoformat(due_local.strip())
        except ValueError:
            return {"error": "due_local must look like YYYY-MM-DDTHH:MM"}
        if due.tzinfo is None:
            due = due.replace(tzinfo=self.tz)
        due = due.astimezone(timezone.utc)
        now = self.now()
        if due <= now:
            return {"error": f"{self.fmt(due)} is already in the past"}
        heads_up = None
        if self.s.heads_up_hours and due - now > timedelta(hours=self.s.heads_up_hours):
            heads_up = due - timedelta(hours=self.s.heads_up_hours)
        rid = self.db.add_reminder(task, due, heads_up)
        out = {"id": rid, "task": task, "due": self.fmt(due)}
        if heads_up:
            out["heads_up"] = self.fmt(heads_up)
        return out

    def _tool_list_reminders(self, ctx: dict) -> dict:
        return {
            "reminders": [
                {"id": r["id"], "task": r["text"], "due": self.fmt(r["due_at"])}
                for r in self.db.upcoming_reminders()
            ]
        }

    def _tool_cancel_reminder(self, ctx: dict, id: int) -> dict:
        return {"status": "cancelled" if self.db.delete_reminder(int(id)) else "no such upcoming reminder"}

    # ---- code generation
    def _tool_make_code_project(self, ctx: dict, request: str) -> dict:
        llm = self.llm

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
                    f"I wrote {project['name']} ({names}) but GitHub isn't configured, so I couldn't "
                    "save it. Set GITHUB_TOKEN and GITHUB_REPO."
                )
                return
            try:
                url = self.github.push_project(folder, project["files"], f"agent: {project['name']}")
            except GitHubError as exc:
                self.notify(f"I wrote {project['name']} but pushing to GitHub failed: {exc}")
                return
            self.notify(f"Done: {project['summary']}\nFiles: {names}\n{url}\nReview it before running it.")

        self.background(work)
        return {"status": "started; the user gets a text with the GitHub link when it's ready"}

    # ------------------------------------------------------------------ laptop helper API
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
        with self._lock:
            cmd = self.db.finish_helper_command(cid, ok, detail)
            awaited = cid in self._awaiting
        if cmd is None:
            return
        if cmd["action"] == "set" and ok:
            self.db.set("last_location", cmd["payload"])
        elif cmd["action"] == "clear" and ok:
            self.db.set("last_location", None)
        if awaited:
            return  # the waiting /message request reports it in its reply
        if cmd["action"] == "set":
            self.notify(
                f"Location set: {cmd['payload']['label']}. Check Find My to see it."
                if ok
                else f"Couldn't set the location: {detail}"
            )
        elif cmd["action"] == "clear":
            self.notify(
                "Real location restored."
                if ok
                else f"Couldn't restore the real location: {detail}. Restarting the phone also clears it."
            )

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
