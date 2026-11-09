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
    assert client.post("/message", json={"text": "hi"}).status_code == 401
    assert client.post("/message", json={"text": "hi", "key": "wrong"}).status_code == 401


def test_message_with_header_and_body_key(client):
    r = client.post("/message", json={"text": "hi"}, headers={"X-Agent-Key": "AK"})
    assert r.status_code == 200 and "answer to: hi" in r.text.replace("​", "")
    r = client.post("/message", json={"text": "hi", "key": "AK"})
    assert r.status_code == 200


def test_own_reply_gets_empty_response(client):
    r = client.post("/message", json={"text": "h​i", "key": "AK"})
    assert r.status_code == 200 and r.text == ""


def test_forwarded_ip_used(client, agent):
    agent.llm.turn(("call", "where_am_i", {}))
    client.post(
        "/message",
        json={"text": "where am i", "key": "AK"},
        headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"},
    )
    assert agent.llm.results[-1]["ip_location"]["query"] == "9.9.9.9"


def test_helper_endpoints(client, agent):
    assert client.get("/helper/poll").status_code == 401
    assert client.get("/helper/poll", headers={"X-Helper-Key": "AK"}).status_code == 401
    r = client.get("/helper/poll", headers={"X-Helper-Key": "HK"})
    assert r.json() == {"command": None}
    agent.db.queue_helper_command("clear", {}, agent.now())
    cmd = client.get("/helper/poll", headers={"X-Helper-Key": "HK"}).json()["command"]
    assert cmd["action"] == "clear"
    r = client.post("/helper/result", headers={"X-Helper-Key": "HK"},
                    json={"id": cmd["id"], "ok": True, "detail": ""})
    assert r.json() == {"ok": True}
    assert client.get("/health").json()["helper_online"] is True


def test_census_then_osm_chain():
    import httpx

    from agent.geo import CensusGeocoder, ChainGeocoder

    def census(request):
        assert "onelineaddress" in request.url.path
        if "1600" in request.url.params["address"]:
            return httpx.Response(200, json={"result": {"addressMatches": [
                {"matchedAddress": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
                 "coordinates": {"x": -77.0365, "y": 38.8977}}]}})
        return httpx.Response(200, json={"result": {"addressMatches": []}})

    class OSM:
        def __init__(self):
            self.queries = []

        def lookup(self, q):
            self.queries.append(q)
            return {"lat": 1.0, "lon": 2.0, "label": "osm", "kind": "attraction"}

    osm = OSM()
    chain = ChainGeocoder(CensusGeocoder(httpx.Client(transport=httpx.MockTransport(census))), osm)
    hit = chain.lookup("1600 Pennsylvania Ave NW, Washington DC")
    assert (hit["lat"], hit["lon"], hit["kind"]) == (38.8977, -77.0365, "house") and osm.queries == []
    assert chain.lookup("999 Nowhere Rd")["label"] == "osm"  # census miss -> OSM
    assert chain.lookup("Times Square")["label"] == "osm"  # not a street address -> OSM only
    assert osm.queries == ["999 Nowhere Rd", "Times Square"]
