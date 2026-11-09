import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Settings  # noqa: E402
from agent.core import Agent  # noqa: E402
from agent.db import DB  # noqa: E402
from agent.llm import LLMError  # noqa: E402

# Tue 2026-11-10 10:00 America/Los_Angeles == 18:00 UTC
START = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=START):
        self.t = now

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeLLM:
    """Scripted stand-in for Gemini. Queue a turn with .turn(("call", name, args), ..., ("say", text))."""

    def __init__(self):
        self.fail = False
        self.turns = []  # list of step lists, one per converse() call
        self.systems = []
        self.histories = []
        self.results = []  # tool results seen, in order
        self.style_calls = []

    def _check(self):
        if self.fail:
            raise LLMError("429 quota")

    def turn(self, *steps):
        self.turns.append(list(steps))

    def converse(self, system, history, tools, call_tool):
        self._check()
        self.systems.append(system)
        self.histories.append(history)
        steps = self.turns.pop(0) if self.turns else [("say", f"answer to: {history[-1]['text']}")]
        for step in steps:
            if step[0] == "call":
                self.results.append(call_tool(step[1], dict(step[2])))
            else:
                return step[1]
        return "ok"

    def summarize_emails(self, emails):
        self._check()
        return f"{len(emails)} emails summarized"

    def style_profile(self, user_messages, previous):
        self._check()
        self.style_calls.append(list(user_messages))
        return "short lowercase texts, no emoji"

    def generate_code(self, request):
        self._check()
        return {"name": "demo-bot", "summary": "A demo bot.", "files": [{"path": "main.py", "content": "print(1)"}]}


class FakeGmail:
    def __init__(self):
        self.sent = []
        self.notes = []

    def recent(self):
        return [{"from": "a", "subject": "s", "date": "d", "snippet": "x", "unread": True}]

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def notify_self(self, text):
        self.notes.append(text)


class FakeGeocoder:
    def __init__(self, hit=None):
        self.hit = hit

    def lookup(self, q):
        return self.hit


class FakeIP:
    def locate(self, ip):
        return {"city": "San Jose", "regionName": "California", "country": "United States",
                "query": ip, "isp": "AT&T"}


class FakeGitHub:
    def __init__(self):
        self.pushed = []

    def push_project(self, folder, files, message):
        self.pushed.append((folder, files))
        return f"https://github.com/me/repo/tree/HEAD/{folder}"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings():
    return Settings(agent_key="AK", helper_key="HK", my_email="me@gmail.com")


def fake_turso_client():
    """httpx client whose transport mimics Turso's /v2/pipeline API on top of in-memory SQLite."""
    import sqlite3

    import httpx

    conn = sqlite3.connect(":memory:", check_same_thread=False)

    def arg(v):
        t = v["type"]
        if t == "null":
            return None
        if t == "integer":
            assert isinstance(v["value"], str), "integers must be sent as strings"
            return int(v["value"])
        if t == "float":
            return float(v["value"])
        assert t == "text", t
        return v["value"]

    def val(x):
        if x is None:
            return {"type": "null"}
        if isinstance(x, int):
            return {"type": "integer", "value": str(x)}
        if isinstance(x, float):
            return {"type": "float", "value": x}
        return {"type": "text", "value": x}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/pipeline"
        assert request.headers["authorization"] == "Bearer TOKEN"
        results = []
        for req in json.loads(request.content)["requests"]:
            if req["type"] == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = req["stmt"]
            try:
                cur = conn.execute(stmt["sql"], [arg(a) for a in stmt.get("args", [])])
                rows = cur.fetchall()
                conn.commit()
            except sqlite3.Error as exc:
                results.append({"type": "error", "error": {"message": str(exc)}})
                continue
            cols = [{"name": d[0]} for d in (cur.description or [])]
            results.append({"type": "ok", "response": {"type": "execute", "result": {
                "cols": cols,
                "rows": [[val(x) for x in r] for r in rows],
                "affected_row_count": max(cur.rowcount, 0),
                "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None,
            }}})
        return httpx.Response(200, json={"baton": None, "base_url": None, "results": results})

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_db(backend: str) -> DB:
    if backend == "turso":
        return DB(turso_url="libsql://test-db.turso.io", turso_token="TOKEN", http_client=fake_turso_client())
    return DB(":memory:")


@pytest.fixture(params=["sqlite", "turso"])
def agent(request, settings, clock):
    return Agent(
        settings,
        make_db(request.param),
        llm=FakeLLM(),
        gmail=FakeGmail(),
        geocoder=FakeGeocoder(),
        iplocator=FakeIP(),
        github=FakeGitHub(),
        clock=clock,
        background=lambda fn: fn(),  # run "background" work inline
        sleep=lambda seconds: None,  # tests drive the laptop helper explicitly
    )


@pytest.fixture
def online(agent):
    """Mark the laptop helper as online."""
    agent.helper_poll()
    return agent
