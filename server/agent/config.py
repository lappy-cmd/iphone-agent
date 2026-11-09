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
    timezone: str = "America/Los_Angeles"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    db_path: str = "agent.db"
    gmail_token_path: str = "token.json"
    github_token: str = ""
    github_repo: str = ""  # "owner/repo"
    digest_time: str = "07:30"  # HH:MM local, empty disables
    heads_up_hours: int = 24  # 0 disables day-before heads-up
    confirm_ttl_minutes: int = 10
    helper_offline_seconds: int = 30

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
            timezone=e("TIMEZONE", "America/Los_Angeles"),
            gemini_api_key=e("GEMINI_API_KEY", ""),
            gemini_model=e("GEMINI_MODEL", "gemini-2.5-flash"),
            db_path=e("DB_PATH", "agent.db"),
            gmail_token_path=e("GMAIL_TOKEN_PATH", "token.json"),
            github_token=e("GITHUB_TOKEN", ""),
            github_repo=e("GITHUB_REPO", ""),
            digest_time=e("DIGEST_TIME", "07:30"),
            heads_up_hours=int(e("HEADS_UP_HOURS", "24")),
            confirm_ttl_minutes=int(e("CONFIRM_TTL_MINUTES", "10")),
            helper_offline_seconds=int(e("HELPER_OFFLINE_SECONDS", "30")),
        )
