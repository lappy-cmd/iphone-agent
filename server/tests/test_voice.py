import base64

import pytest

from agent.gmail import _plain_body, voice_text
from agent.persona import MARK

GV_FROM = '"(408) 555-1234" <14085550000.14085551234.abcDEF@txt.voice.google.com>'


def gv(id, text, ms, sender=GV_FROM, subject="New text message from (408) 555-1234"):
    return {"id": id, "thread_id": "T1", "internal_ms": ms, "from": sender,
            "subject": subject, "message_id": f"<{id}@mail>", "text": text}


@pytest.fixture
def voice(agent):
    agent.gmail.voice_inbox = []
    agent.gmail.voice_sent = []
    agent.s.owner_phone = "+1 (408) 555-1234"
    agent.db.set("voice_since", int(agent.now().timestamp()))
    return agent


def now_ms(agent, plus_s=0):
    return int(agent.now().timestamp() + plus_s) * 1000


def test_owner_text_gets_threaded_plain_reply(voice):
    voice.gmail.voice_inbox = [gv("m1", "hey there", now_ms(voice, 5))]
    voice.poll_voice()
    [sent] = voice.gmail.voice_sent
    assert sent["text"] == "answer to: hey there" and MARK not in sent["text"]
    assert sent["to"] == "14085550000.14085551234.abcDEF@txt.voice.google.com"
    assert sent["thread_id"] == "T1" and sent["in_reply_to"] == "<m1@mail>"
    voice.poll_voice()  # same email again: not answered twice
    assert len(voice.gmail.voice_sent) == 1


def test_ignores_strangers_old_texts_and_disabled(voice):
    stranger = '"(650) 111-2222" <14085550000.16501112222.x@txt.voice.google.com>'
    voice.gmail.voice_inbox = [
        gv("old", "from before start", now_ms(voice, -600)),
        gv("s1", "hi", now_ms(voice, 5), sender=stranger, subject="New text message from (650) 111-2222"),
    ]
    voice.poll_voice()
    assert voice.gmail.voice_sent == []
    voice.s.owner_phone = ""
    voice.gmail.voice_inbox = [gv("m2", "hi", now_ms(voice, 5))]
    voice.poll_voice()
    assert voice.gmail.voice_sent == []


def test_first_run_records_start_time(agent):
    agent.s.owner_phone = "4085551234"
    agent.gmail.voice_inbox = []
    agent.poll_voice()
    assert agent.db.get("voice_since") == int(agent.now().timestamp())


def test_notifications_use_voice_once_a_thread_exists(voice):
    voice.notify("Reminder: x")  # no conversation yet -> email fallback
    assert voice.gmail.notes and voice.gmail.voice_sent == []
    voice.gmail.voice_inbox = [gv("m1", "hi", now_ms(voice, 5))]
    voice.poll_voice()
    voice.notify("Reminder: y")
    assert voice.gmail.voice_sent[-1]["text"] == "Reminder: y"
    assert len(voice.gmail.notes) == 1


def test_voice_text_and_plain_body():
    body = "omw, call u later\r\n\r\nYOUR ACCOUNT <https://voice.google.com> HELP CENTER\r\n"
    assert voice_text(body) == "omw, call u later"
    assert voice_text("To respond to this text message, reply") == "To respond to this text message, reply"
    data = base64.urlsafe_b64encode(b"hello\n\nTo respond to this text message, reply to this email").decode()
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>x</p>").decode()}},
        {"mimeType": "text/plain", "body": {"data": data}},
    ]}
    assert voice_text(_plain_body(payload)) == "hello"


def test_voice_status_reports_progress_and_errors(voice):
    stranger = '"(650) 111-2222" <14085550000.16501112222.x@txt.voice.google.com>'
    voice.gmail.voice_inbox = [gv("old", "x", now_ms(voice, -600)), gv("m1", "hi", now_ms(voice, 5)),
                               gv("s1", "hi", now_ms(voice, 5), sender=stranger, subject="New text message")]
    voice.poll_voice()
    st = voice.voice_status
    assert st["enabled"] and st["polls"] == 1 and st["emails_found"] == 3
    assert (st["answered"], st["ignored_other_number"], st["skipped_old"]) == (1, 1, 1)

    def boom(after_epoch):
        raise RuntimeError("no network")
    voice.gmail.voice_messages = boom
    with pytest.raises(RuntimeError):
        voice.poll_voice()
    assert st["last_error"].startswith("RuntimeError at ")


def test_failed_reply_is_resent_next_poll_without_rerunning_ai(voice):
    real_reply = voice.gmail.voice_reply
    calls = []

    def flaky(*a, **k):
        if not calls:
            calls.append(1)
            raise RuntimeError("smtp hiccup")
        return real_reply(*a, **k)
    voice.gmail.voice_reply = flaky
    voice.gmail.voice_inbox = [gv("m1", "hey there", now_ms(voice, 5))]
    with pytest.raises(RuntimeError):
        voice.poll_voice()
    assert voice.gmail.voice_sent == []
    voice.poll_voice()
    [sent] = voice.gmail.voice_sent
    assert sent["text"] == "answer to: hey there" and sent["in_reply_to"] == "<m1@mail>"
    assert voice.voice_status["answered"] == 1
    voice.poll_voice()
    assert len(voice.gmail.voice_sent) == 1
