"""Settings loaded from environment variables (see server/.env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    agent_key: str = ""
    helper_key: str = ""
    my_email: str = ""
    notify_email: str = ""  # where reminder/status emails go (an @icloud.com address is push); default my_email
    timezone: str = "America/Los_Angeles"
    telegram_bot_token: str = ""  # from @BotFather; turns on Telegram texting
    telegram_owner_id: str = ""  # your numeric Telegram user ID (the bot tells you on first message)
    public_url: str = ""  # this server's https URL, for the Telegram webhook (Render sets RENDER_EXTERNAL_URL)
    self_repo: str = ""  # "owner/repo" of this agent's own code, for change_own_code
    self_github_token: str = ""  # token with Contents read/write + Actions read on self_repo
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    gemini_api_key: str = ""  # optional backup if Claude fails (or the only AI if no Claude key)
    gemini_model: str = "gemini-3.8-flash"
    db_path: str = "agent.db"
    turso_url: str = ""  # set to use a hosted Turso database instead of the db_path file
    turso_token: str = ""
    gmail_token_path: str = "token.json"
    gmail_token_json: str = ""  # token.json contents; written to gmail_token_path if that file is missing
    github_token: str = ""
    github_repo: str = ""  # "owner/repo"
    digest_time: str = "07:30"  # HH:MM local, empty disables
    heads_up_hours: int = 24  # 0 disables day-before heads-up
    confirm_ttl_minutes: int = 10
    helper_offline_seconds: int = 30
    owner_phone: str = ""  # your cell number; set it to turn on Google Voice texting (others are ignored)
    voice_poll_seconds: int = 5  # how often to check Gmail for Google Voice texts
    location_wait_seconds: int = 20  # wait this long for the laptop before replying inline
    history_messages: int = 20  # conversation turns the AI sees
    style_every: int = 10  # refresh the "how you text" profile every N messages

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        if env_file is None:
            env_file = Path(__file__).resolve().parent.parent / ".env"
        _load_dotenv(Path(env_file))
        e = os.environ.get
        return cls(
            agent_key=e("AGENT_KEY", ""),
            helper_key=e("HELPER_KEY", ""),
            my_email=e("MY_EMAIL", ""),
            notify_email=e("NOTIFY_EMAIL", ""),
            owner_phone=e("OWNER_PHONE", ""),
            voice_poll_seconds=int(e("VOICE_POLL_SECONDS", "5")),
            timezone=e("TIMEZONE", "America/Los_Angeles"),
            telegram_bot_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_owner_id=e("TELEGRAM_OWNER_ID", ""),
            public_url=e("PUBLIC_URL", "") or e("RENDER_EXTERNAL_URL", ""),
            self_repo=e("SELF_REPO", ""),
            self_github_token=e("SELF_GITHUB_TOKEN", ""),
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            claude_model=e("CLAUDE_MODEL", "claude-opus-5-5"),
            gemini_api_key=e("GEMINI_API_KEY", ""),
            gemini_model=e("GEMINI_MODEL", "gemini-3.8-flash"),
            db_path=e("DB_PATH", "agent.db"),
            turso_url=e("TURSO_DATABASE_URL", ""),
            turso_token=e("TURSO_AUTH_TOKEN", ""),
            gmail_token_path=e("GMAIL_TOKEN_PATH", "token.json"),
            gmail_token_json=e("GMAIL_TOKEN_JSON", ""),
            github_token=e("GITHUB_TOKEN", ""),
            github_repo=e("GITHUB_REPO", ""),
            digest_time=e("DIGEST_TIME", "07:30"),
            heads_up_hours=int(e("HEADS_UP_HOURS", "24")),
            confirm_ttl_minutes=int(e("CONFIRM_TTL_MINUTES", "10")),
            helper_offline_seconds=int(e("HELPER_OFFLINE_SECONDS", "30")),
        )
