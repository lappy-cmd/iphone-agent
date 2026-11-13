"""Web tools: page parsing, search, forms, private-address blocking, and the agent's web tools."""
import httpx
import pytest

from agent.web import Web, WebError

PUBLIC = {"example.com": ["93.184.216.34"], "html.duckduckgo.com": ["52.1.2.3"]}


def resolve(host):
    if host in PUBLIC:
        return PUBLIC[host]
    if host == "localhost":
        return ["127.0.0.1"]
    return [host]  # IP literals


PAGE = """<html><head><title>Hello</title><style>.x{color:red}</style></head><body>
<h1>Welcome</h1><p>Some &amp; text</p><script>alert(1)</script>
<a href="/next">Next page</a>
<form action="/search" method="get"><input type="hidden" name="t" value="1"><input name="q"><button>Go</button></form>
</body></html>"""

DDG = (
    '<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews&rut=x">'
    'Big <b>News</b></a><a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2F'
    'example.com%2Fnews">It happened today.</a></div>'
)


def handler(request):
    path = request.url.path
    if request.url.host == "example.com":
        if path == "/":
            return httpx.Response(200, html=PAGE)
        if path == "/search":
            p = request.url.params
            return httpx.Response(200, text=f"results for {p['q']} t={p['t']}")
        if path == "/login":
            assert request.method == "POST"
            return httpx.Response(200, text="posted " + request.content.decode())
        if path == "/hop":
            return httpx.Response(302, headers={"location": "http://localhost/admin"})
        if path == "/img":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    if request.url.host == "html.duckduckgo.com":
        return httpx.Response(200, html=DDG)
    raise AssertionError(f"unexpected request to {request.url}")


def make():
    return Web(http=httpx.Client(transport=httpx.MockTransport(handler)), resolve=resolve)


def test_read_page_text_links_and_forms():
    page = make().read("http://example.com/")
    assert page["title"] == "Hello"
    assert "Welcome" in page["text"] and "Some & text" in page["text"]
    assert "alert" not in page["text"] and "color" not in page["text"]
    assert {"text": "Next page", "url": "http://example.com/next"} in page["links"]
    form = page["forms"][0]
    assert form["method"] == "get" and form["action"] == "http://example.com/search"
    assert {"name": "t", "type": "hidden", "value": "1"} in form["fields"]
    assert {"name": "q", "type": "text", "value": ""} in form["fields"]


def test_submit_get_and_post_forms():
    web = make()
    assert web.submit("http://example.com/search", "get", {"t": "1", "q": "cats"})["text"] == "results for cats t=1"
    assert web.submit("http://example.com/login", "post", {"user": "me"})["text"] == "posted user=me"


def test_search_results_are_unwrapped():
    results = make().search("news")
    assert results[0] == {"title": "Big News", "url": "https://example.com/news", "snippet": "It happened today."}


@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://127.0.0.1:8000/health",
    "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.1/",
    "http://[::ffff:127.0.0.1]/",
    "file:///etc/passwd",
    "http://user:pw@example.com/",
])
def test_private_and_odd_addresses_are_blocked(url):
    with pytest.raises(WebError):
        make().read(url)


def test_redirect_to_private_address_is_blocked():
    with pytest.raises(WebError, match="private"):
        make().read("http://example.com/hop")


def test_non_pages_are_refused():
    with pytest.raises(WebError, match="not a readable web page"):
        make().read("http://example.com/img")


# ---------------------------------------------------------------- agent tools
class FakeWeb:
    def __init__(self):
        self.submitted = []

    def search(self, query):
        return [{"title": "T", "url": "https://example.com", "snippet": f"about {query}"}]

    def read(self, url, start=0):
        if "broken" in url:
            raise WebError("the site answered with error 500")
        return {"url": url, "title": "T", "text": "page text", "links": [], "forms": []}

    def check_url(self, url):
        if "localhost" in url:
            raise WebError("localhost is a private or local address, which I can't open")

    def submit(self, url, method, fields):
        self.submitted.append((url, method, fields))
        return {"url": url, "title": "", "text": "thanks", "links": [], "forms": []}


def last_result(agent):
    return agent.llm.results[-1]


def test_web_not_configured(agent):
    agent.llm.turn(("call", "web_search", {"query": "weather"}))
    agent.handle("weather?")
    assert "isn't set up" in last_result(agent)["error"]


def test_search_and_read(agent):
    agent.web = FakeWeb()
    agent.llm.turn(("call", "web_search", {"query": "giants score"}),
                   ("call", "read_webpage", {"url": "https://example.com"}), ("say", "they won"))
    agent.handle("did the giants win")
    search, page = agent.llm.results[-2:]
    assert search["results"][0]["snippet"] == "about giants score" and "untrusted" in search["note"]
    assert page["text"] == "page text" and "untrusted" in page["note"]
    agent.llm.turn(("call", "read_webpage", {"url": "https://broken.example"}))
    agent.handle("open it")
    assert "500" in last_result(agent)["error"]


def test_get_form_runs_right_away(agent):
    agent.web = FakeWeb()
    agent.llm.turn(("call", "submit_web_form", {"url": "https://example.com/s", "method": "get",
                                                "fields": [{"name": "q", "value": "cats"}]}))
    agent.handle("search that site for cats")
    assert last_result(agent)["text"] == "thanks"
    assert agent.web.submitted == [("https://example.com/s", "get", {"q": "cats"})]


def test_post_form_needs_confirmation_in_a_later_message(agent):
    agent.web = FakeWeb()
    form = {"url": "https://example.com/signup", "method": "post",
            "fields": [{"name": "email", "value": "me@x.com"}]}
    agent.llm.turn(("call", "submit_web_form", form), ("call", "confirm_pending", {}), ("say", "ok?"))
    agent.handle("sign me up")
    assert agent.llm.results[-2]["status"].startswith("prepared")
    assert "Not confirmed yet" in last_result(agent)["error"]
    assert agent.web.submitted == []
    agent.llm.turn(("call", "confirm_pending", {}), ("say", "done"))
    agent.handle("yes")
    assert last_result(agent)["status"] == "submitted"
    assert agent.web.submitted == [("https://example.com/signup", "post", {"email": "me@x.com"})]


def test_post_form_to_private_address_refused(agent):
    agent.web = FakeWeb()
    agent.llm.turn(("call", "submit_web_form", {"url": "http://localhost/x", "method": "post"}))
    agent.handle("post it")
    assert "private" in last_result(agent)["error"]
    agent.llm.turn(("call", "confirm_pending", {}))
    agent.handle("yes")
    assert "Nothing is waiting" in last_result(agent)["error"]
