"""Look things up on the web: search, read pages, follow links and submit simple forms.

Only public http(s) addresses are fetched (never localhost or private networks, checked again on
every redirect), no credentials are ever sent, and page contents are untrusted text for the AI to
read, never instructions. Pages that need JavaScript (games, web apps) can't be driven.
"""
from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class WebError(Exception):
    pass


MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
TEXT_CHARS = 6000
MAX_LINKS = 40
MAX_FORMS = 5
MAX_FIELDS = 30
MAX_RESULTS = 8
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section",
              "article", "header", "footer", "form", "table", "ul", "ol", "main", "nav"}


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _tidy(text: str) -> str:
    lines = (_clean(line) for line in (text or "").split("\n"))
    return "\n".join(line for line in lines if line)


def _decode(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _default_resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


class _Page(HTMLParser):
    """Collects the readable text, title, links and forms of an HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self.links: list[dict] = []
        self.forms: list[dict] = []
        self._skip = 0
        self._in_title = False
        self._link: dict | None = None
        self._select: dict | None = None
        self._textarea: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and a.get("href"):
            self._link = {"href": a["href"], "text": "", "class": a.get("class", "")}
        elif tag == "form":
            self.forms.append({"action": a.get("action", ""),
                               "method": (a.get("method") or "get").lower(), "fields": []})
        elif tag in ("input", "button") and self.forms and a.get("name"):
            kind = (a.get("type") or ("submit" if tag == "button" else "text")).lower()
            field = {"name": a["name"], "type": kind, "value": a.get("value", "")}
            if kind in ("checkbox", "radio"):
                field["checked"] = "checked" in a
            self.forms[-1]["fields"].append(field)
        elif tag == "textarea" and self.forms and a.get("name"):
            self._textarea = {"name": a["name"], "type": "textarea", "value": ""}
            self.forms[-1]["fields"].append(self._textarea)
        elif tag == "select" and self.forms and a.get("name"):
            self._select = {"name": a["name"], "type": "select", "value": "", "options": []}
            self.forms[-1]["fields"].append(self._select)
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            self._select["options"].append(value)
            if "selected" in a or len(self._select["options"]) == 1:
                self._select["value"] = value

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._link is not None:
            self.links.append(self._link)
            self._link = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        if self._textarea is not None:
            self._textarea["value"] += data
            return
        self.parts.append(data)
        if self._link is not None:
            self._link["text"] += data

    def text(self) -> str:
        return _tidy("".join(self.parts))


class Web:
    SEARCH_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, http=None, resolve: Callable[[str], list[str]] | None = None):
        if http is None:
            import httpx

            http = httpx.Client(
                timeout=15,
                follow_redirects=False,  # redirects are followed by hand so each hop is checked
                headers={"User-Agent": "Mozilla/5.0 (compatible; personal-assistant)"},
            )
        self._http = http
        self._resolve = resolve or _default_resolve

    # ------------------------------------------------------------------ safety
    def check_url(self, url: str) -> None:
        """Raise WebError unless url is a public http(s) address."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise WebError(f"only http(s) web addresses can be opened, not {url[:100]}")
        if parts.username or parts.password:
            raise WebError("web addresses with a login in them aren't allowed")
        host = parts.hostname
        try:
            addrs = self._resolve(host)
        except (OSError, UnicodeError) as exc:
            raise WebError(f"couldn't find the site {host}") from exc
        if not addrs:
            raise WebError(f"couldn't find the site {host}")
        for addr in addrs:
            try:
                ip = ipaddress.ip_address(str(addr).split("%")[0])
            except ValueError as exc:
                raise WebError(f"couldn't find the site {host}") from exc
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if not ip.is_global:
                raise WebError(f"{host} is a private or local address, which I can't open")

    def _request(self, method: str, url: str, data: dict | None = None) -> tuple[str, str, str]:
        """(final url, content type, text). Every redirect hop is checked again."""
        for _ in range(MAX_REDIRECTS + 1):
            self.check_url(url)
            try:
                with self._http.stream(method, url, data=data if method == "POST" else None) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        if r.status_code in (301, 302, 303):
                            method, data = "GET", None
                        continue
                    if r.status_code >= 400:
                        raise WebError(f"the site answered with error {r.status_code}")
                    ctype = r.headers.get("content-type", "").lower()
                    if ctype and not any(t in ctype for t in ("html", "text", "json", "xml")):
                        raise WebError(f"that's not a readable web page ({ctype.split(';')[0]})")
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) >= MAX_BYTES:
                            break
                    return str(r.url), ctype, _decode(bytes(body[:MAX_BYTES]), r.charset_encoding)
            except WebError:
                raise
            except Exception as exc:
                raise WebError(f"couldn't load {url[:100]} ({type(exc).__name__})") from exc
        raise WebError("too many redirects")

    # ------------------------------------------------------------------ pages
    def _page(self, url: str, ctype: str, body: str, start: int = 0) -> dict:
        title, links, forms = "", [], []
        if "html" in ctype or (not ctype and body.lstrip()[:1] == "<"):
            p = _Page()
            p.feed(body)
            p.close()
            title, text = _clean(p.title), p.text()
            seen = {url.split("#")[0]}
            for link in p.links:
                target = urljoin(url, link["href"]).split("#")[0]
                if urlsplit(target).scheme not in ("http", "https") or target in seen:
                    continue
                seen.add(target)
                links.append({"text": _clean(link["text"])[:80], "url": target})
                if len(links) >= MAX_LINKS:
                    break
            for form in p.forms[:MAX_FORMS]:
                fields = []
                for f in form["fields"][:MAX_FIELDS]:
                    f = dict(f)
                    if "options" in f:
                        f["options"] = f["options"][:20]
                    fields.append(f)
                forms.append({"method": form["method"], "action": urljoin(url, form["action"] or url),
                              "fields": fields})
        else:
            text = _tidy(body)
        start = max(0, int(start or 0))
        out = {"url": url, "title": title, "text": text[start:start + TEXT_CHARS],
               "total_chars": len(text), "links": links, "forms": forms}
        if start + TEXT_CHARS < len(text):
            out["more"] = f"call read_webpage again with start={start + TEXT_CHARS} for more"
        return out

    def read(self, url: str, start: int = 0) -> dict:
        final, ctype, body = self._request("GET", url)
        return self._page(final, ctype, body, start)

    def submit(self, url: str, method: str, fields: dict) -> dict:
        if method.lower() == "post":
            final, ctype, body = self._request("POST", url, dict(fields))
        else:
            parts = urlsplit(url)
            query = urlencode(parse_qsl(parts.query) + list(fields.items()))
            final, ctype, body = self._request("GET", urlunsplit(parts._replace(query=query, fragment="")))
        return self._page(final, ctype, body)

    def search(self, query: str) -> list[dict]:
        final, _ctype, body = self._request("GET", self.SEARCH_URL + "?" + urlencode({"q": query}))
        p = _Page()
        p.feed(body)
        p.close()
        titles = [l for l in p.links if "result__a" in l["class"].split()]
        snippets = [l for l in p.links if "result__snippet" in l["class"].split()]
        results = []
        for i, link in enumerate(titles):
            target = urljoin(final, link["href"])
            wrapped = parse_qs(urlsplit(target).query).get("uddg")
            if wrapped:
                target = wrapped[0]
            if "duckduckgo.com/y.js" in target or urlsplit(target).scheme not in ("http", "https"):
                continue  # ads
            results.append({"title": _clean(link["text"]), "url": target,
                            "snippet": _clean(snippets[i]["text"]) if i < len(snippets) else ""})
            if len(results) >= MAX_RESULTS:
                break
        return results
