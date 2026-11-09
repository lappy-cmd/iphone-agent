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
    def __init__(self):
        self.fail = False
        self.reminder = None  # {"task", "due_local"}

    def _check(self):
        if self.fail:
            raise LLMError("429 quota")

    def chat(self, text):
        self._check()
        return f"answer to: {text}"

    def summarize_emails(self, emails):
        self._check()
        return f"{len(emails)} emails summarized"

    def draft_email(self, to, instruction):
        self._check()
        return {"subject": "Hi", "body": f"Body: {instruction}"}

    def parse_reminder(self, text, now_local):
        self._check()
        return self.reminder

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


@pytest.fixture
def agent(settings, clock):
    return Agent(
        settings,
        DB(":memory:"),
        llm=FakeLLM(),
        gmail=FakeGmail(),
        geocoder=FakeGeocoder(),
        iplocator=FakeIP(),
        github=FakeGitHub(),
        clock=clock,
        background=lambda fn: fn(),  # run "background" work inline
    )


@pytest.fixture
def online(agent):
    """Mark the laptop helper as online."""
    agent.helper_poll()
    return agent
