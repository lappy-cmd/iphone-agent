import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge as B  # noqa: E402


class FakeHTTP:
    def __init__(self, inbox=None, replies=None, outbox=None):
        self.inbox = list(inbox or [])
        self.replies = replies or {}
        self.outbox = list(outbox or [])
        self.sent, self.calls = [], []

    def __call__(self, method, url, body=None, headers=None, timeout=30):
        self.calls.append((method, url, body, headers))
        if "/message/query" in url:
            assert "password=PW" in url
            msgs, self.inbox = self.inbox, []
            return {"status": 200, "data": msgs}
        if "/message/text" in url:
            assert body["method"] == "apple-script" and body["tempGuid"]
            self.sent.append((body["chatGuid"], body["message"]))
            return {"status": 200}
        if url.endswith("/imessage/message"):
            assert headers["X-Agent-Key"] == "AK"
            return {"reply": self.replies.get(body["text"], "")}
        if url.endswith("/imessage/outbox"):
            out, self.outbox = self.outbox, []
            return {"messages": out}
        raise AssertionError(url)


def msg(guid, text, sender="+1 (555) 123-4567", chat="iMessage;-;+15551234567", me=False, t=2000):
    return {"guid": guid, "text": text, "isFromMe": me, "dateCreated": t,
            "handle": {"address": sender}, "chats": [{"guid": chat}]}


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "STATE", tmp_path / "state.json")


def make(http):
    return B.Bridge("https://srv/", "AK", "http://localhost:1234", "PW", ["5551234567"],
                    http=http, clock=lambda: 1.0)


def test_answers_owner_only_once():
    http = FakeHTTP(
        inbox=[
            msg("a", "hi"),
            msg("b", "spam", sender="+15550000000", chat="iMessage;-;+15550000000"),
            msg("c", "group", chat="iMessage;+;chat123"),
            msg("d", "my own reply", me=True),
            msg("a", "hi"),  # duplicate
        ],
        replies={"hi": "hey!"},
    )
    br = make(http)
    assert br.handle_incoming() == 1
    assert http.sent == [("iMessage;-;+15551234567", "hey!")]
    assert br.cursor == 2000

    # cursor, chat and seen ids survive a restart
    br2 = make(FakeHTTP(inbox=[msg("a", "hi")], replies={"hi": "again?"}))
    assert br2.cursor == 2000 and br2.owner_chat == "iMessage;-;+15551234567"
    assert br2.handle_incoming() == 0


def test_email_owner_and_empty_reply():
    http = FakeHTTP(inbox=[msg("e", "yo", sender="Me@iCloud.com", chat="iMessage;-;me@icloud.com")])
    br = B.Bridge("https://srv", "AK", "http://localhost:1234", "PW", ["me@icloud.com", " "],
                  http=http, clock=lambda: 1.0)
    assert br.handle_incoming() == 1
    assert http.sent == []  # server returned '' -> send nothing


def test_outbox_needs_known_chat_then_delivers():
    http = FakeHTTP(outbox=["reminder: dentist"])
    br = make(http)
    assert br.deliver_outbox() == 0  # no chat known yet
    br.owner_chat = "iMessage;-;+15551234567"
    http.outbox = ["reminder: dentist"]
    assert br.deliver_outbox() == 1
    assert http.sent == [("iMessage;-;+15551234567", "reminder: dentist")]


def test_norm():
    assert B.norm("+1 (555) 123-4567") == B.norm("5551234567")
    assert B.norm(" A@B.com ") == "a@b.com"
