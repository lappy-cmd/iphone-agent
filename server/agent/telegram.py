"""Telegram Bot API client (just what the agent needs)."""
from __future__ import annotations

import hashlib

import httpx

MAX_LEN = 4096  # Telegram's limit per message


def webhook_secret(bot_token: str) -> str:
    """Stable secret Telegram echoes back in X-Telegram-Bot-Api-Secret-Token (A-Z a-z 0-9 only)."""
    return hashlib.sha256(f"webhook:{bot_token}".encode()).hexdigest()[:48]


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + ([text] if text else [])


class Telegram:
    def __init__(self, bot_token: str, client: httpx.Client | None = None):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._http = client or httpx.Client(timeout=20)
        self.secret = webhook_secret(bot_token)

    def _call(self, method: str, **params) -> dict:
        resp = self._http.post(f"{self._base}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            # Never include the URL: it contains the bot token.
            raise RuntimeError(f"telegram {method} failed: {data.get('description', resp.status_code)}")
        return data.get("result")

    def send(self, chat_id: int, text: str) -> None:
        for part in chunks(text):
            self._call("sendMessage", chat_id=chat_id, text=part)

    def typing(self, chat_id: int) -> None:
        self._call("sendChatAction", chat_id=chat_id, action="typing")

    def set_webhook(self, url: str) -> None:
        self._call("setWebhook", url=url, secret_token=self.secret,
                   allowed_updates=["message"], drop_pending_updates=False)
