from datetime import datetime

from conftest import FakeGeocoder

from agent.persona import MARK, is_ours, mark


def plain(text: str) -> str:
    return text.replace(MARK, "")


def last_result(agent):
    return agent.llm.results[-1]


# ---------------------------------------------------------------- loop guard + basics
def test_replies_are_marked_and_own_texts_ignored(agent):
    reply = agent.handle("hey what's up")
    assert is_ours(reply) and plain(reply) == "answer to: hey what's up"
    assert agent.handle(reply) == ""  # our reply echoing back through the self-chat
    assert agent.handle(mark("Reminder: x")) == ""  # a forwarded notification
    assert len(agent.llm.histories) == 1


def test_empty_and_old_prefix(agent):
    assert agent.handle("   ") == ""
    assert plain(agent.handle("agent: hello")) == "answer to: hello"


def test_conversation_memory_is_passed(agent):
    agent.handle("my name is sam")
    agent.handle("what's my name")
    history = agent.llm.histories[-1]
    assert [m["role"] for m in history] == ["user", "model", "user"]
    assert history[0]["text"] == "my name is sam"


def test_gemini_failure_is_reported(agent):
    agent.llm.fail = True
    assert "unavailable" in agent.handle("hello")


def test_system_prompt_has_time_and_helper_status(agent):
    agent.handle("hi")
    system = agent.llm.systems[-1]
    assert "Tuesday 2026-11-10 10:00" in system and "OFFLINE" in system


def test_style_profile_learned_every_n_messages(agent):
    agent.s.style_every = 3
    for i in range(3):
        agent.handle(f"yo {i}")
    assert agent.llm.style_calls == [["yo 0", "yo 1", "yo 2"]]
    agent.handle("again")
    assert "short lowercase texts" in agent.llm.systems[-1]


# ---------------------------------------------------------------- email
def test_check_email_tool(agent):
    agent.llm.turn(("call", "check_email", {}), ("say", "you have 1 email"))
    agent.handle("anything in my inbox?")
    assert last_result(agent)["emails"][0]["subject"] == "s"


def test_email_needs_confirmation_in_a_later_message(agent):
    draft = {"to": "jane@x.com", "subject": "Late", "body": "Running late!"}
    # the model tries to send in the same turn: refused by the server
    agent.llm.turn(("call", "draft_email", draft), ("call", "confirm_pending", {}), ("say", "ok?"))
    agent.handle("email jane@x.com that i'm late")
    assert "Not confirmed yet" in last_result(agent)["error"]
    assert agent.gmail.sent == []
    agent.llm.turn(("call", "confirm_pending", {}), ("say", "sent!"))
    assert plain(agent.handle("yeah send it")) == "sent!"
    assert agent.gmail.sent == [("jane@x.com", "Late", "Running late!")]
    assert "Waiting for the user's confirmation on: email" in agent.llm.systems[-1]


def test_email_cancel_and_bad_address(agent):
    agent.llm.turn(("call", "draft_email", {"to": "jane", "subject": "a", "body": "b"}))
    agent.handle("email jane hi")
    assert "isn't an email address" in last_result(agent)["error"]
    agent.llm.turn(("call", "draft_email", {"to": "j@x.com", "subject": "a", "body": "b"}))
    agent.handle("email j@x.com hi")
    agent.llm.turn(("call", "cancel_pending", {}))
    agent.handle("nah")
    assert last_result(agent)["status"] == "cancelled"
    agent.llm.turn(("call", "confirm_pending", {}))
    agent.handle("actually yes")
    assert "Nothing is waiting" in last_result(agent)["error"]
    assert agent.gmail.sent == []


def test_confirmation_expires(agent, clock):
    agent.llm.turn(("call", "draft_email", {"to": "j@x.com", "subject": "a", "body": "b"}))
    agent.handle("email j@x.com hi")
    clock.advance(minutes=11)
    agent.llm.turn(("call", "confirm_pending", {}))
    agent.handle("yes")
    assert "Nothing is waiting" in last_result(agent)["error"]


# ---------------------------------------------------------------- location
def test_location_offline_helper(agent):
    agent.llm.turn(("call", "set_location", {"place": "37.33, -122.03"}))
    agent.handle("put me at 37.33, -122.03")
    assert "offline" in last_result(agent)["error"]


def finish_helper_on_sleep(agent, ok=True, detail="ok"):
    """Simulate the laptop: the first time the server waits, the helper runs the command."""

    def sleep(_):
        cmd = agent.helper_poll()
        if cmd:
            agent.helper_result(cmd["id"], ok, detail)

    agent.sleep = sleep


def test_location_set_inline_reply(online):
    a = online
    a.llm.turn(("call", "set_location", {"place": "37.334900, -122.009000"}), ("say", "go there?"))
    a.handle("put me at 37.334900, -122.009000")
    assert last_result(a)["lat"] == 37.3349
    finish_helper_on_sleep(a)
    a.llm.turn(("call", "confirm_pending", {}), ("say", "done"))
    a.handle("yes")
    assert last_result(a)["status"] == "done"
    assert a.gmail.notes == []  # reported inline, no extra email
    a.llm.turn(("call", "where_am_i", {}))
    a.handle("where am i")
    assert last_result(a)["fake_location"]["lat"] == 37.3349


def test_location_slow_helper_texts_later(online):
    a = online
    a.llm.turn(("call", "set_location", {"place": "1, 2"}))
    a.handle("put me at 1, 2")
    a.llm.turn(("call", "confirm_pending", {}))
    a.handle("yes")
    assert last_result(a)["status"] == "still working"
    cmd = a.helper_poll()
    a.helper_result(cmd["id"], True, "ok")
    assert "Location set" in plain(a.gmail.notes[-1]) and is_ours(a.gmail.notes[-1])


def test_location_failure_inline(online):
    a = online
    a.llm.turn(("call", "set_location", {"place": "1, 2"}))
    a.handle("put me at 1, 2")
    finish_helper_on_sleep(a, ok=False, detail="No device connected")
    a.llm.turn(("call", "confirm_pending", {}))
    a.handle("do it")
    assert last_result(a) == {"status": "failed", "detail": "No device connected"}


def test_location_address_warning_and_not_found(online):
    online.geocoder = FakeGeocoder({"lat": 1.0, "lon": 2.0, "label": "Oak St", "kind": "road"})
    online.llm.turn(("call", "set_location", {"place": "Oak St, San Jose"}))
    online.handle("put me on oak st")
    assert "matched a road" in last_result(online)["warning"]
    online.geocoder = FakeGeocoder(None)
    online.llm.turn(("call", "set_location", {"place": "nowhere 999"}))
    online.handle("put me at nowhere 999")
    assert "Couldn't find" in last_result(online)["error"]


def test_clear_location(online):
    finish_helper_on_sleep(online)
    online.llm.turn(("call", "clear_location", {}))
    online.handle("give me my real location back")
    assert last_result(online)["status"] == "done"


def test_stale_helper_commands_are_dropped(online, clock):
    online.llm.turn(("call", "clear_location", {}))
    online.handle("real location")
    clock.advance(minutes=3)
    assert online.helper_poll() is None


def test_helper_goes_offline(online, clock):
    clock.advance(seconds=31)
    assert not online.helper_online()


def test_where_am_i_shows_ip(agent):
    agent.llm.turn(("call", "where_am_i", {}))
    agent.handle("where am i", client_ip="1.2.3.4")
    r = last_result(agent)
    assert r["ip_location"]["city"] == "San Jose" and r["fake_location"] is None


# ---------------------------------------------------------------- reminders
def test_reminder_with_heads_up_and_firing(agent, clock):
    agent.llm.turn(("call", "add_reminder", {"task": "bio essay", "due_local": "2026-11-13T09:00"}))
    agent.handle("remind me about my bio essay fri 9am")
    r = last_result(agent)
    assert r["id"] == 1 and r["due"] == "Fri Nov 13 9:00 AM" and "heads_up" in r
    agent.llm.turn(("call", "list_reminders", {}))
    agent.handle("what reminders do i have")
    assert last_result(agent)["reminders"][0]["task"] == "bio essay"

    clock.t = datetime.fromisoformat("2026-11-12T17:00:00+00:00")  # Thu 9am PT
    agent.tick()
    assert any("Heads-up: bio essay" in plain(n) for n in agent.gmail.notes)

    clock.t = datetime.fromisoformat("2026-11-13T17:00:30+00:00")
    agent.tick()
    assert any("Reminder: bio essay" in plain(n) for n in agent.gmail.notes)
    n = len(agent.gmail.notes)
    agent.tick()
    assert len(agent.gmail.notes) == n  # not sent twice


def test_reminder_validation(agent):
    agent.llm.turn(("call", "add_reminder", {"task": "x", "due_local": "2026-11-01T09:00"}))
    agent.handle("remind me x nov 1")
    assert "past" in last_result(agent)["error"]
    agent.llm.turn(("call", "add_reminder", {"task": "x", "due_local": "friday"}))
    agent.handle("remind me x friday")
    assert "YYYY-MM-DD" in last_result(agent)["error"]


def test_cancel_reminder(agent):
    agent.llm.turn(("call", "add_reminder", {"task": "x", "due_local": "2026-11-10T15:00"}))
    agent.handle("remind me x 3pm")
    agent.llm.turn(("call", "cancel_reminder", {"id": 1}))
    agent.handle("cancel that")
    assert last_result(agent)["status"] == "cancelled"
    agent.llm.turn(("call", "cancel_reminder", {"id": 1}))
    agent.handle("cancel it again")
    assert last_result(agent)["status"] == "no such upcoming reminder"


def test_unknown_tool_and_bad_args(agent):
    agent.llm.turn(("call", "fly", {}), ("call", "add_reminder", {"task": "x"}))
    agent.handle("hm")
    assert "unknown tool" in agent.llm.results[-2]["error"]
    assert "bad arguments" in agent.llm.results[-1]["error"]


# ---------------------------------------------------------------- digest
def test_morning_digest_once_per_day(agent, clock):
    clock.t = datetime.fromisoformat("2026-11-11T15:31:00+00:00")  # 7:31 PT
    agent.tick()
    agent.tick()
    digests = [n for n in agent.gmail.notes if "Morning email digest" in plain(n)]
    assert len(digests) == 1


def test_no_digest_outside_window(agent, clock):
    clock.t = datetime.fromisoformat("2026-11-11T22:00:00+00:00")  # 2pm PT
    agent.tick()
    assert agent.gmail.notes == []


# ---------------------------------------------------------------- codegen
def test_codegen_pushes_to_github(agent):
    agent.llm.turn(("call", "make_code_project", {"request": "an agent that tracks my grades"}))
    agent.handle("make me an agent that tracks my grades")
    assert "started" in last_result(agent)["status"]
    folder, files = agent.github.pushed[0]
    assert folder.endswith("demo-bot") and files[0]["path"] == "main.py"
    assert "github.com" in agent.gmail.notes[-1]
