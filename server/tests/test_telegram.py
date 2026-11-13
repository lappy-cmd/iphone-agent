import httpx
import pytest
from fastapi.testclient import TestClient

from agent.app import create_app
from agent.telegram import Telegram, chunks, webhook_secret


class FakeTG:
    secret = "S3CRET"

    def __init__(self):
        self.sent, self.typed = [], 0

    def send(self, chat, text):
        self.sent.append((chat, text))

    def typing(self, chat):
        self.typed += 1


def upd(uid, text, sender=42, chat_type="private"):
    return {"update_id": uid, "message": {"text": text, "from": {"id": sender},
                                          "chat": {"id": sender, "type": chat_type}}}


@pytest.fixture
def tg(agent):
    agent.telegram = FakeTG()
    agent.s.telegram_owner_id = "42"
    return agent.telegram


def test_owner_gets_reply_once(agent, tg):
    agent.telegram_update(upd(1, "hi"))
    agent.telegram_update(upd(1, "hi"))  # Telegram retry
    assert tg.sent == [(42, "answer to: hi")]  # plain text, no self-chat marker


def test_others_and_groups_ignored(agent, tg):
    agent.telegram_update(upd(2, "hi", sender=7))
    agent.telegram_update(upd(3, "hi", chat_type="group"))
    assert tg.sent == []


def test_first_contact_without_owner_tells_id(agent, tg):
    agent.s.telegram_owner_id = ""
    agent.telegram_update(upd(4, "hello", sender=99))
    assert "99" in tg.sent[0][1] and "TELEGRAM_OWNER_ID" in tg.sent[0][1]


def test_notify_goes_to_telegram_after_first_chat(agent, tg):
    agent.notify("early")  # no chat yet -> old route (email)
    assert tg.sent == []
    agent.telegram_update(upd(5, "/start"))
    agent.notify("reminder: dentist")
    assert tg.sent[-1] == (42, "reminder: dentist")


def test_webhook_checks_secret(agent, tg):
    client = TestClient(create_app(agent, run_scheduler=False))
    assert client.post("/telegram/webhook", json=upd(6, "hi")).status_code == 401
    r = client.post("/telegram/webhook", json=upd(6, "hi"),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "S3CRET"})
    assert r.status_code == 200 and tg.sent == [(42, "answer to: hi")]
    assert client.get("/health").json()["telegram"] is True


def test_client_and_helpers():
    calls = []

    def handler(req):
        calls.append((req.url.path, req.read()))
        return httpx.Response(200, json={"ok": True, "result": True})

    t = Telegram("123:ABC", client=httpx.Client(transport=httpx.MockTransport(handler)))
    t.send(5, "x" * 5000)
    assert [c[0] for c in calls] == ["/bot123:ABC/sendMessage"] * 2
    assert t.secret == webhook_secret("123:ABC") and t.secret.isalnum()
    assert "".join(chunks("a\n" * 3000)).count("a") == 3000

    bad = Telegram("1:X", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))))
    with pytest.raises(RuntimeError, match="Unauthorized") as e:
        bad.send(1, "hi")
    assert "1:X" not in str(e.value)
