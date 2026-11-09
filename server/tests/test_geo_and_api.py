import pytest
from fastapi.testclient import TestClient

from agent.app import create_app
from agent.geo import parse_coordinates


@pytest.mark.parametrize(
    "text,expected",
    [
        ("37.3349, -122.0090", (37.3349, -122.009)),
        ("37.3349 -122.0090", (37.3349, -122.009)),
        ("https://www.google.com/maps/@37.3349,-122.009,17z", (37.3349, -122.009)),
        ("https://www.google.com/maps/place/Foo/@37.1,-121.9,15z/data=x", (37.1, -121.9)),
        ("https://maps.apple.com/?ll=37.33,-122.03&q=Pin", (37.33, -122.03)),
        ("https://maps.google.com/?q=37.33,-122.03", (37.33, -122.03)),
        ("123 Oak St, San Jose", None),
        ("95.0, 10.0", None),  # invalid latitude
    ],
)
def test_parse_coordinates(text, expected):
    assert parse_coordinates(text) == expected


@pytest.fixture
def client(agent):
    return TestClient(create_app(agent, run_scheduler=False))


def test_message_requires_key(client):
    assert client.post("/message", json={"text": "agent: help"}).status_code == 401
    assert client.post("/message", json={"text": "hi", "key": "wrong"}).status_code == 401


def test_message_with_header_and_body_key(client):
    r = client.post("/message", json={"text": "agent: help"}, headers={"X-Agent-Key": "AK"})
    assert r.status_code == 200 and "Commands" in r.text
    r = client.post("/message", json={"text": "agent: help", "key": "AK"})
    assert r.status_code == 200


def test_forwarded_ip_used(client):
    r = client.post(
        "/message",
        json={"text": "agent: where am i", "key": "AK"},
        headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"},
    )
    assert "9.9.9.9" in r.text


def test_helper_endpoints(client, agent):
    assert client.get("/helper/poll").status_code == 401
    assert client.get("/helper/poll", headers={"X-Helper-Key": "AK"}).status_code == 401
    r = client.get("/helper/poll", headers={"X-Helper-Key": "HK"})
    assert r.json() == {"command": None}
    agent.handle("agent: real location")
    cmd = client.get("/helper/poll", headers={"X-Helper-Key": "HK"}).json()["command"]
    assert cmd["action"] == "clear"
    r = client.post("/helper/result", headers={"X-Helper-Key": "HK"},
                    json={"id": cmd["id"], "ok": True, "detail": ""})
    assert r.json() == {"ok": True}
    assert client.get("/health").json()["helper_online"] is True
