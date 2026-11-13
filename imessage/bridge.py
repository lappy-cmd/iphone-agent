"""iMessage bridge. Runs on the Mac next to BlueBubbles Server.

New iMessage from you -> the agent server -> reply sent back as an iMessage.
Also delivers the server's reminders/notifications. Only outgoing connections:
nothing on the Mac is exposed to the internet. Standard library only.

Run: python3 bridge.py   (settings in imessage/.env, see .env.example)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("bridge")
HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def norm(handle: str) -> str:
    """Phone numbers compare by their last 10 digits, emails case-insensitively."""
    handle = (handle or "").strip().lower()
    if "@" in handle:
        return handle
    digits = re.sub(r"\D", "", handle)
    return digits[-10:]


def http_json(method: str, url: str, body=None, headers=None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "null")


class Bridge:
    def __init__(self, server: str, agent_key: str, bb_url: str, bb_password: str,
                 owners: list[str], http=http_json, clock=time.time):
        self.server = server.rstrip("/")
        self.agent_key = agent_key
        self.bb_url = bb_url.rstrip("/")
        self.bb_password = bb_password
        self.owners = {norm(o) for o in owners if o.strip()}
        self.http = http
        self.clock = clock
        state = self._load_state()
        # Start from "now" on first run so old messages are never answered.
        self.cursor: int = state.get("cursor") or int(clock() * 1000)
        self.owner_chat: str | None = state.get("owner_chat")
        self.seen: list[str] = state.get("seen", [])

    # ---- state ----
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        STATE.write_text(json.dumps({"cursor": self.cursor, "owner_chat": self.owner_chat,
                                     "seen": self.seen[-200:]}), encoding="utf-8")

    # ---- BlueBubbles ----
    def _bb(self, method: str, path: str, body=None):
        q = urllib.parse.urlencode({"password": self.bb_password})
        return self.http(method, f"{self.bb_url}/api/v1/{path}?{q}", body)

    def new_messages(self) -> list[dict]:
        resp = self._bb("POST", "message/query", {
            "limit": 50, "sort": "ASC", "after": self.cursor, "with": ["chat", "handle"],
        })
        return (resp or {}).get("data") or []

    def send(self, chat_guid: str, text: str) -> None:
        self._bb("POST", "message/text", {
            "chatGuid": chat_guid, "tempGuid": str(uuid.uuid4()),
            "message": text, "method": "apple-script",
        })

    # ---- agent server ----
    def _server(self, method: str, path: str, body=None, timeout: float = 30):
        return self.http(method, f"{self.server}{path}", body,
                         {"X-Agent-Key": self.agent_key}, timeout)

    def ask(self, text: str) -> str:
        """Get the agent's reply; retry while the server restarts or wakes up."""
        for attempt in range(4):
            try:
                return (self._server("POST", "/imessage/message", {"text": text}, 150) or {}).get("reply", "")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.warning("server unreachable (try %d): %s", attempt + 1, exc)
                time.sleep(5 * (attempt + 1))
        return "Sorry, I can't reach my brain right now. Try again in a minute."

    # ---- one round ----
    def handle_incoming(self) -> int:
        answered = 0
        for m in self.new_messages():
            self.cursor = max(self.cursor, int(m.get("dateCreated") or 0))
            guid = m.get("guid")
            if not guid or guid in self.seen:
                continue
            self.seen.append(guid)
            if m.get("isFromMe"):
                continue
            chats = m.get("chats") or []
            chat = chats[0].get("guid") if chats else None
            sender = norm(((m.get("handle") or {}).get("address")) or "")
            text = (m.get("text") or "").strip()
            if not chat or ";+;" in chat or sender not in self.owners or not text:
                continue  # group chats, other people, stickers/reactions without text
            self.owner_chat = chat
            self._save_state()  # before the slow part, so a crash can't answer twice
            log.info("answering a text")
            reply = self.ask(text)
            if reply:
                self.send(chat, reply)
            answered += 1
        self._save_state()
        return answered

    def deliver_outbox(self) -> int:
        msgs = (self._server("GET", "/imessage/outbox") or {}).get("messages") or []
        if msgs and not self.owner_chat:
            log.warning("have %d notification(s) but no chat yet: text the agent once first", len(msgs))
            return 0
        for text in msgs:
            self.send(self.owner_chat, text)
        return len(msgs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(HERE / ".env")
    e = os.environ.get
    missing = [k for k in ("SERVER_URL", "AGENT_KEY", "BB_PASSWORD", "OWNER") if not e(k)]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in imessage/.env (see .env.example)")
    bridge = Bridge(e("SERVER_URL"), e("AGENT_KEY"), e("BB_URL", "http://localhost:1234"),
                    e("BB_PASSWORD"), e("OWNER").split(","))
    log.info("bridge running")
    last_outbox = 0.0
    while True:
        try:
            bridge.handle_incoming()
        except Exception as exc:  # BlueBubbles restarting, etc.
            log.warning("reading messages failed: %s", exc)
            time.sleep(5)
        if time.time() - last_outbox >= 5:  # also keeps the free Render server awake
            last_outbox = time.time()
            try:
                bridge.deliver_outbox()
            except Exception as exc:
                log.warning("outbox check failed: %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    main()
