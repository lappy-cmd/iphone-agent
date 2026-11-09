from datetime import datetime

from agent.core import sanitize

from conftest import FakeGeocoder


# ---------------------------------------------------------------- basics
def test_sanitize_prefixes_and_defuses_trigger():
    out = sanitize("Text agent: hi")
    assert out.startswith("🤖 ")
    assert "agent:" not in out.lower()


def test_help_and_prefix_stripping(agent):
    assert "Commands" in agent.handle("agent: help")
    assert "Commands" in agent.handle("AGENT:")


def test_general_question_goes_to_gemini(agent):
    assert agent.handle("agent: what is 2+2") == "🤖 answer to: what is 2+2"


def test_gemini_failure_is_reported(agent):
    agent.llm.fail = True
    assert "limit" in agent.handle("agent: hello")


# ---------------------------------------------------------------- email
def test_summary(agent):
    assert "1 emails summarized" in agent.handle("agent: summarize my email")


def test_email_requires_confirmation(agent):
    reply = agent.handle("agent: email jane@x.com saying I'll be late")
    assert "Send it?" in reply and agent.gmail.sent == []
    assert "Sent to jane@x.com" in agent.handle("agent: yes")
    assert agent.gmail.sent == [("jane@x.com", "Hi", "Body: I'll be late")]


def test_email_cancel(agent):
    agent.handle("agent: email jane@x.com saying hi")
    assert "Cancelled" in agent.handle("agent: no")
    assert agent.gmail.sent == []


def test_confirmation_expires(agent, clock):
    agent.handle("agent: email jane@x.com saying hi")
    clock.advance(minutes=11)
    assert "Nothing is waiting" in agent.handle("agent: yes")
    assert agent.gmail.sent == []


# ---------------------------------------------------------------- location
def test_location_offline_helper(agent):
    assert "offline" in agent.handle("agent: put me at 37.33, -122.03")


def test_location_coords_flow(online, clock):
    a = online
    reply = a.handle("agent: put me at 37.334900, -122.009000")
    assert "37.334900, -122.009000" in reply and "agent​: yes" in reply  # defused in reply
    assert "Moving your phone" in a.handle("agent: yes")
    cmd = a.helper_poll()
    assert cmd["action"] == "set" and cmd["payload"]["lat"] == 37.3349
    a.helper_result(cmd["id"], True, "ok")
    assert "Location set" in a.gmail.notes[-1]
    assert "37.33490" in a.handle("agent: where am i")


def test_location_address_with_street_warning(online):
    online.geocoder = FakeGeocoder({"lat": 1.0, "lon": 2.0, "label": "Oak St", "kind": "road"})
    reply = online.handle("agent: put me at Oak St, San Jose")
    assert "matched a road" in reply


def test_location_address_not_found(online):
    assert "Couldn't find" in online.handle("agent: put me at nowhere-land 999")


def test_location_failure_reported(online):
    online.handle("agent: put me at 1, 2")
    online.handle("agent: yes")
    cmd = online.helper_poll()
    online.helper_result(cmd["id"], False, "No device connected")
    assert "No device connected" in online.gmail.notes[-1]


def test_clear_location(online):
    assert "Restoring" in online.handle("agent: real location")
    cmd = online.helper_poll()
    assert cmd["action"] == "clear"
    online.helper_result(cmd["id"], True, "")
    assert "restored" in online.gmail.notes[-1]


def test_stale_helper_commands_are_dropped(online, clock):
    online.handle("agent: real location")
    clock.advance(minutes=3)
    assert online.helper_poll() is None


def test_helper_goes_offline(online, clock):
    clock.advance(seconds=31)
    assert not online.helper_online()


def test_where_am_i_shows_ip(agent):
    reply = agent.handle("agent: where am i", client_ip="1.2.3.4")
    assert "San Jose" in reply and "1.2.3.4" in reply


# ---------------------------------------------------------------- reminders
def test_reminder_with_heads_up_and_firing(agent, clock):
    agent.llm.reminder = {"task": "bio essay", "due_local": datetime(2026, 11, 13, 9, 0)}
    reply = agent.handle("agent: remind me bio essay due Fri 9am")
    assert "Reminder #1 set: bio essay, Fri Nov 13 9:00 AM" in reply and "heads-up" in reply
    assert "#1" in agent.handle("agent: reminders")

    clock.t = datetime.fromisoformat("2026-11-12T17:00:00+00:00")  # Thu 9am PT
    agent.tick()
    assert any("Heads-up: bio essay" in n for n in agent.gmail.notes)

    clock.t = datetime.fromisoformat("2026-11-13T17:00:30+00:00")
    agent.tick()
    assert any("Reminder: bio essay" in n for n in agent.gmail.notes)
    n = len(agent.gmail.notes)
    agent.tick()
    assert len(agent.gmail.notes) == n  # not sent twice
    assert "No upcoming" in agent.handle("agent: reminders")


def test_reminder_in_past_rejected(agent):
    agent.llm.reminder = {"task": "x", "due_local": datetime(2026, 11, 1, 9, 0)}
    assert "already past" in agent.handle("agent: remind me x nov 1")


def test_reminder_falls_back_to_dateparser(agent):
    agent.llm.fail = True
    reply = agent.handle("agent: remind me call mom tomorrow at 5pm")
    assert "Reminder #1 set" in reply and "Wed Nov 11 5:00 PM" in reply


def test_cancel_reminder(agent):
    agent.llm.reminder = {"task": "x", "due_local": datetime(2026, 11, 10, 15, 0)}
    agent.handle("agent: remind me x 3pm")
    assert "cancelled" in agent.handle("agent: cancel reminder 1")
    assert "No upcoming" in agent.handle("agent: cancel reminder 1")


# ---------------------------------------------------------------- digest
def test_morning_digest_once_per_day(agent, clock):
    clock.t = datetime.fromisoformat("2026-11-11T15:31:00+00:00")  # 7:31 PT
    agent.tick()
    agent.tick()
    digests = [n for n in agent.gmail.notes if "Morning email digest" in n]
    assert len(digests) == 1


def test_no_digest_outside_window(agent, clock):
    clock.t = datetime.fromisoformat("2026-11-11T22:00:00+00:00")  # 2pm PT
    agent.tick()
    assert agent.gmail.notes == []


# ---------------------------------------------------------------- codegen
def test_codegen_pushes_to_github(agent):
    reply = agent.handle("agent: make me an agent that tracks my grades")
    assert "Working on it" in reply
    folder, files = agent.github.pushed[0]
    assert folder.endswith("demo-bot") and files[0]["path"] == "main.py"
    assert "github.com" in agent.gmail.notes[-1]
