"""How the assistant talks, the tools it can call, and the loop-guard marker on its texts."""
from __future__ import annotations

# Every text the server produces carries this invisible character. The iPhone automation fires on
# every message in the self-chat, including the agent's own replies; the server ignores any
# incoming text containing the marker, so replies never loop.
MARK = "​"


def mark(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    return text[0] + MARK + text[1:] + MARK


def is_ours(text: str) -> bool:
    return MARK in (text or "")


BASE = """You are the user's personal assistant, and you talk with them by text message (iMessage).

Voice: start out warm and polished, with friendly, complete sentences. Then continuously adapt to how
the user texts: mirror their message length, capitalization, punctuation, emoji use, slang and
tone. If they're short and lowercase, be short and lowercase. If they're formal, be formal. Sound
like a person texting, never like a robot. Don't mention being an AI or a language model unless
asked. Keep replies short, like texts. Plain text only: no markdown, no headings, no bullet
symbols like * or #.

Acting: use the tools to do things. Never say you did something unless a tool result confirms it.
If a tool returns an error, explain it simply and suggest what to do.

Confirmations: sending an email and changing the phone's location need the user's OK.
- First call draft_email or set_location. That only prepares the action.
- Then tell the user exactly what will happen (for email: recipient, subject and the full body;
  for location: the place found) and ask if you should go ahead.
- Only when the user's NEXT message agrees, call confirm_pending. If they say no, call
  cancel_pending. If they ask for changes, prepare a new draft or place.

Reminders: work out the due date/time yourself from the current local time below and pass it to
add_reminder as YYYY-MM-DDTHH:MM in the user's timezone. If they give no time, use 09:00.

Email: to answer questions about their inbox, call check_email and summarize what matters
(deadlines, people waiting on replies, money, security alerts) before the noise.

Code: when they ask you to build/make/write a program, bot, script, app or agent, call
make_code_project with a clear description; the result is pushed to their GitHub and they get a
text with the link when it's ready."""


def system_prompt(
    now_local: str,
    timezone: str,
    helper_online: bool,
    pending: dict | None,
    style: str,
) -> str:
    parts = [
        BASE,
        f"Current local date/time: {now_local} ({timezone}).",
        "Laptop location helper: "
        + (
            "online (location changes work)."
            if helper_online
            else "OFFLINE. Location changes won't work until the user turns on the laptop, "
            "plugs in the phone and starts the helper."
        ),
    ]
    if pending:
        parts.append(f"Waiting for the user's confirmation on: {pending['kind']} {pending['summary']}")
    if style:
        parts.append("Notes on how this user texts (mirror this):\n" + style)
    return "\n\n".join(parts)


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or []}


TOOLS = [
    {
        "name": "set_location",
        "description": "Find a place and PREPARE moving the phone's reported GPS location there "
        "(shown in Find My). Accepts a street address, landmark, 'lat, lon' coordinates or a "
        "maps link. Needs the user's confirmation afterwards.",
        "parameters": _obj({"place": {"type": "string"}}, ["place"]),
    },
    {
        "name": "clear_location",
        "description": "Stop faking the location and restore the phone's real location.",
        "parameters": _obj(),
    },
    {
        "name": "where_am_i",
        "description": "Report the fake location currently set (if any) and the location of the "
        "user's IP address (useful for VPN comparisons).",
        "parameters": _obj(),
    },
    {
        "name": "check_email",
        "description": "Fetch the user's Gmail messages from the last 2 days (sender, subject, "
        "preview, unread).",
        "parameters": _obj(),
    },
    {
        "name": "draft_email",
        "description": "PREPARE an email from the user's Gmail. You write the subject and body in "
        "the user's voice (no placeholder names). Needs the user's confirmation afterwards.",
        "parameters": _obj(
            {
                "to": {"type": "string", "description": "recipient email address"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            ["to", "subject", "body"],
        ),
    },
    {
        "name": "confirm_pending",
        "description": "Carry out the prepared email or location change. Only call this when the "
        "user's latest message clearly agrees to it.",
        "parameters": _obj(),
    },
    {
        "name": "cancel_pending",
        "description": "Drop the prepared email or location change.",
        "parameters": _obj(),
    },
    {
        "name": "add_reminder",
        "description": "Schedule a reminder text.",
        "parameters": _obj(
            {
                "task": {"type": "string", "description": "what to remind them about"},
                "due_local": {
                    "type": "string",
                    "description": "local due time, format YYYY-MM-DDTHH:MM",
                },
            },
            ["task", "due_local"],
        ),
    },
    {
        "name": "list_reminders",
        "description": "List upcoming reminders with their ids.",
        "parameters": _obj(),
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel an upcoming reminder by id.",
        "parameters": _obj({"id": {"type": "integer"}}, ["id"]),
    },
    {
        "name": "make_code_project",
        "description": "Generate a small code project (program, bot, script, agent, site...) and "
        "push it to the user's GitHub. Runs in the background; the user gets a text with the link.",
        "parameters": _obj({"request": {"type": "string"}}, ["request"]),
    },
]
