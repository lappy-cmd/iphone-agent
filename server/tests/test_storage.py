import json

import pytest
from conftest import fake_turso_client

from agent.app import build_agent
from agent.config import Settings
from agent.db import DB, DBError, TursoConn


def test_turso_url_conversion():
    c = TursoConn("libsql://my-db-me.turso.io", "T")
    assert c._url == "https://my-db-me.turso.io/v2/pipeline"


def test_turso_roundtrip_types():
    db = DB(turso_url="libsql://x.turso.io", turso_token="TOKEN", http_client=fake_turso_client())
    db.set("k", {"a": 1, "b": None})
    assert db.get("k") == {"a": 1, "b": None}
    assert db.get("missing", 5) == 5


def test_turso_error_raises():
    import httpx

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad token")))
    with pytest.raises(DBError):
        DB(turso_url="libsql://x.turso.io", turso_token="T", http_client=client)


def test_gmail_token_written_from_env(tmp_path):
    token = tmp_path / "token.json"
    s = Settings(my_email="me@gmail.com", db_path=":memory:", gmail_token_path=str(token),
                 gmail_token_json=json.dumps({"refresh_token": "r"}))
    build_agent(s)
    assert json.loads(token.read_text())["refresh_token"] == "r"


def test_gmail_token_file_not_overwritten(tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"refresh_token": "newer"}')
    s = Settings(my_email="me@gmail.com", db_path=":memory:", gmail_token_path=str(token), gmail_token_json='{"refresh_token": "old"}')
    build_agent(s)
    assert "newer" in token.read_text()
