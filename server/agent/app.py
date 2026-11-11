"""HTTP API. Run: uvicorn agent.app:app --host 127.0.0.1 --port 8000 (Caddy or the host provides HTTPS)."""
from __future__ import annotations

import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .config import Settings
from .core import Agent
from .db import DB

log = logging.getLogger("agent")
TICK_SECONDS = 30


def build_agent(settings: Settings) -> Agent:
    """Wire real services; anything not configured is left as None and reported when used."""
    from .geo import CensusGeocoder, ChainGeocoder, IPLocator, Nominatim

    llm = gmail = github = None
    if settings.gemini_api_key:
        from .llm import GeminiLLM

        llm = GeminiLLM(settings.gemini_api_key, settings.gemini_model)
    if settings.my_email:
        from .gmail import Gmail

        if settings.gmail_token_json and not os.path.exists(settings.gmail_token_path):
            with open(settings.gmail_token_path, "w", encoding="utf-8") as fh:
                fh.write(settings.gmail_token_json)

        gmail = Gmail(settings.gmail_token_path, settings.my_email, settings.notify_email)
    if settings.github_token and settings.github_repo:
        from .github import GitHub

        github = GitHub(settings.github_token, settings.github_repo)
    return Agent(
        settings,
        DB(settings.db_path, turso_url=settings.turso_url, turso_token=settings.turso_token),
        llm=llm,
        gmail=gmail,
        geocoder=ChainGeocoder(CensusGeocoder(), Nominatim()),
        iplocator=IPLocator(),
        github=github,
    )


def _check(given: str | None, expected: str) -> None:
    if not expected or not given or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


class MessageIn(BaseModel):
    text: str
    key: str | None = None  # alternatively sent as X-Agent-Key header


class ResultIn(BaseModel):
    id: int
    ok: bool
    detail: str = ""


def create_app(agent: Agent, run_scheduler: bool = True) -> FastAPI:
    stop = threading.Event()

    def scheduler() -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                agent.tick()
            except Exception:
                log.exception("tick failed")

    def voice_poller() -> None:
        while not stop.wait(max(agent.s.voice_poll_seconds, 2)):
            try:
                agent.poll_voice()
            except Exception:
                log.exception("google voice poll failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_scheduler:
            threading.Thread(target=scheduler, daemon=True).start()
            if agent.voice_enabled():
                threading.Thread(target=voice_poller, daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="iPhone Agent", lifespan=lifespan, docs_url=None, redoc_url=None)

    def helper_auth(x_helper_key: str | None = Header(default=None)) -> None:
        _check(x_helper_key, agent.s.helper_key)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "helper_online": agent.helper_online(), "voice": agent.voice_status}

    @app.post("/message", response_class=PlainTextResponse)
    def message(body: MessageIn, request: Request, x_agent_key: str | None = Header(default=None)):
        _check(x_agent_key or body.key, agent.s.agent_key)
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
        return agent.handle(body.text, client_ip=ip)

    @app.get("/helper/poll", dependencies=[Depends(helper_auth)])
    def helper_poll() -> dict:
        return {"command": agent.helper_poll()}

    @app.post("/helper/result", dependencies=[Depends(helper_auth)])
    def helper_result(body: ResultIn) -> dict:
        agent.helper_result(body.id, body.ok, body.detail)
        return {"ok": True}

    return app


def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not settings.agent_key or not settings.helper_key:
        raise SystemExit("Set AGENT_KEY and HELPER_KEY in server/.env (run scripts/make_keys.py)")
    return create_app(build_agent(settings))


def __getattr__(name: str):  # lazy so tests can import create_app without a .env
    if name == "app":
        return _default_app()
    raise AttributeError(name)
