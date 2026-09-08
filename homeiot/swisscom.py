"""The Swisscom Internet-Box.

Swisscom does not publish this box's local API, and it is not the same across
generations: the Internet-Box 4 is an Arcadyan PRV65AX, not the Sagemcom-based
boxes whose ``/ws`` RPC is documented in the wild.  On a real IB4, ``/ws`` does
not answer at all and ``/api/v1/...`` returns the box's own 404 page -- so the
web server is there and the API simply lives somewhere else.

Rather than guess at a third shape, the prober reads the box's own web
interface: it fetches the page, follows the scripts it loads, and pulls the
request paths out of them.  A single-page app has to name its endpoints
somewhere, and that somewhere is its JavaScript.

    python3 -m homeiot --probe-box 10.0.0.1

Reading a bundler's output takes more than looking for ``"/api/..."``: a
modern build joins a base onto a relative path, so this collects every
string that could be part of a URL and, more usefully, quotes the code
around each ``fetch`` and ``new WebSocket`` so the joining itself is visible.

Two answers here are not failures but findings.  A path that resets the
connection while every unknown path returns a tidy 404 is a path something
is listening on -- ``/ws`` behaves exactly as a WebSocket route does when
sent a plain POST -- so the prober offers it a real WebSocket handshake, and
asks for a path that certainly does not exist as a control, to tell a
route's refusal apart from the server's general dislike of a method.

Everything it finds is printed. That output is what the rest of this
integration should be written against.

The MQTT service the box advertises is Swisscom's own, for their mesh
repeaters; it needs credentials this cannot obtain, so it is left alone.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import model, net

SOURCE = "swisscom"
TIMEOUT = 5.0
DEFAULT_ADDRESS = "192.168.1.1"
# The name the box redirects to.  It is resolved by *never* resolving it: we
# connect to the box's own address and pass this as the Host header, so the
# request stays in the house.
CANONICAL_HOST = "internetbox.swisscom.ch"
ASSET_LIMIT = 20  # scripts to follow; a router's web app is not large
ASSET_BYTES = 4_000_000

# Paths worth trying directly.  The first three are what older Swisscom boxes
# answered; the rest are the shapes Arcadyan firmware tends to use.
CANDIDATES: tuple[tuple[str, str, Any], ...] = (
    ("POST", "/ws", {"service": "DeviceInfo", "method": "get", "parameters": {}}),
    ("GET", "/api/v1/general/deviceinfo", None),
    ("GET", "/api/v1/general/status", None),
    ("GET", "/api/v1/system/deviceinfo", None),
    ("GET", "/api/system/deviceinfo", None),
    ("GET", "/api/status", None),
    ("GET", "/data/status.json", None),
    ("GET", "/data/DeviceInfo.json", None),
    ("GET", "/data/deviceinfo.json", None),
    ("GET", "/cgi/status.json", None),
    ("POST", "/sysbus/DeviceInfo:get", {"parameters": {}}),
    ("GET", "/login", None),
    ("GET", "/api/v1/login", None),
)

# A path that cannot exist, asked for both ways: its answer is the baseline
# every other answer is read against.
CONTROLS: tuple[tuple[str, str, Any], ...] = (
    ("GET", "/homeiot-probe-no-such-path", None),
    ("POST", "/homeiot-probe-no-such-path", {"probe": True}),
)

FINGERPRINTS = ("internet-box", "internetbox", "swisscom", "arcadyan")

# The SoftAtHome JSON-RPC the older boxes speak.  A plain JSON POST to /ws is
# refused by it; the content type is part of the protocol, not decoration, so
# the earlier probes could not have told a wrong dialect from a missing route.
SAH_HEADERS = {"Content-Type": "application/x-sah-ws-4-call+json", "Authorization": "X-Sah-Login"}
SAH_CALLS: tuple[tuple[str, Any], ...] = (
    ("/ws", {"service": "sah.Device.Information", "method": "createContext",
             "parameters": {"applicationName": "webui", "username": "guest", "password": "guest"}}),
    ("/ws", {"service": "DeviceInfo", "method": "get", "parameters": {}}),
    ("/ws/NeMo/Intf/data", {"service": "NeMo.Intf.data", "method": "getMIBs", "parameters": {}}),
)

# Every quoted string in a bundle, then judged rather than matched: a build
# tool joins a base onto a relative path, so requiring a leading `/api` finds
# nothing on a box that works perfectly well.
STRING_PATTERN = re.compile(r"""['"`]([^'"`\\\r\n]{2,160})['"`]""")
ASSET_PATTERN = re.compile(r"""(?:src|href)\s*=\s*['"]([^'"]+\.(?:js|mjs))['"]""", re.IGNORECASE)
CHUNK_PATTERN = re.compile(r"""['"]([^'"\s]{1,120}\.m?js)['"]""")
TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

ABSOLUTE_PATH = re.compile(r"/[A-Za-z0-9_.\-/:{}$%~+]*(?:\?[A-Za-z0-9_.\-/:{}$%=&]*)?")
# A trailing slash matters: `"v1/"` is a fragment waiting to be joined, and
# dropping it loses the middle of every URL the app builds.
RELATIVE_PATH = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-:{}$%~+]+)*/?")

# Words that mark a string as worth putting at the top of the list.
API_WORDS = (
    "api", "ws", "rpc", "sysbus", "sah", "nemo", "cgi", "rest", "graphql", "json", "data",
    "auth", "login", "logout", "session", "token", "user", "password",
    "device", "system", "status", "info", "config", "setting", "state",
    "network", "wan", "lan", "wifi", "wlan", "dhcp", "topology", "host", "port",
    "firewall", "guest", "voip", "phone", "usb", "reboot", "diagnostic",
)
ASSET_SUFFIXES = (".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico",
                  ".woff", ".woff2", ".ttf", ".eot", ".map", ".html", ".htm", ".txt")
MIME_HEADS = ("text", "image", "application", "audio", "video", "font", "multipart", "model", "message")

# Where a bundle actually names its endpoints: at the call site.
CALL_MARKERS = ("fetch(", "new WebSocket", "WebSocket(", "EventSource(", "XMLHttpRequest",
                '.open("', ".open('", "baseURL", "axios", "basePath", "apiUrl", "API_URL")


class SwisscomError(Exception):
    pass


# --- asking politely ---------------------------------------------------------
# Written on raw sockets rather than urllib, for three reasons this box makes
# necessary: the reply headers are evidence (a Location names the hostname the
# box wants to be called by), the Host header has to be ours to set, and a
# redirect must NOT be followed -- chasing one to internetbox.swisscom.ch would
# leave the house and ask Swisscom about a box sitting in the next room.


def where(ip: str, scheme: str = "http", host: str = "") -> dict[str, str]:
    """A place to ask: a scheme, an address, and the name to ask it by."""
    return {"scheme": scheme, "ip": ip, "host": host}


def describe(target: dict[str, str]) -> str:
    named = f" as {target['host']}" if target["host"] else ""
    return f"{target['scheme']}://{target['ip']}{named}"


def _dechunk(raw: bytes) -> bytes:
    pieces, rest = [], raw
    while True:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(size_line.split(b";")[0] or b"0", 16)
        except ValueError:
            return b"".join(pieces) or raw
        if size == 0:
            break
        pieces.append(rest[:size])
        rest = rest[size + 2:]
    return b"".join(pieces)


def _body(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _ask(target: dict[str, str], method: str, path: str, payload: Any = None,
         timeout: float = TIMEOUT, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """One request, described rather than raised."""
    scheme, ip, host = target["scheme"], target["ip"], target["host"]
    if path.startswith("http"):  # an absolute URL carries its own destination
        split = urlsplit(path)
        scheme, ip, path = split.scheme, split.netloc, split.path or "/"
    name, _, port = ip.partition(":")
    sent = b"" if payload is None else json.dumps(payload).encode()
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host or ip}", "Accept: */*",
             "User-Agent: homeiot-probe", "Connection: close",
             *(f"{key}: {value}" for key, value in (headers or {}).items())]
    if sent:
        lines += [f"Content-Length: {len(sent)}"]
        if not (headers or {}).get("Content-Type"):
            lines += ["Content-Type: application/json"]
    request = ("\r\n".join(lines) + "\r\n\r\n").encode() + sent

    missed = {"path": path, "method": method, "status": None, "headers": {},
              "body": None, "reached": False}
    try:
        link = socket.create_connection((name, int(port or (443 if scheme == "https" else 80))),
                                        timeout=timeout)
        if scheme == "https":
            link = net.INSECURE_CONTEXT.wrap_socket(link, server_hostname=host or name)
        with link:
            link.sendall(request)
            chunks: list[bytes] = []
            while sum(map(len, chunks)) < ASSET_BYTES:
                piece = link.recv(65536)
                if not piece:
                    break
                chunks.append(piece)
    except (OSError, ValueError) as error:
        return {**missed, "why": str(error)}

    head, _, rest = b"".join(chunks).partition(b"\r\n\r\n")
    lines_back = head.decode("utf-8", "replace").splitlines()
    if not lines_back:
        return {**missed, "why": "closed without answering"}
    first = lines_back[0].split()
    answered = {key.strip().lower(): value.strip()
                for key, _, value in (line.partition(":") for line in lines_back[1:])}
    if answered.get("transfer-encoding", "").lower() == "chunked":
        rest = _dechunk(rest)
    return {
        "path": path,
        "method": method,
        "status": int(first[1]) if len(first) > 1 and first[1].isdigit() else None,
        "headers": answered,
        "body": _body(rest),
        "reached": True,
        "why": "",
    }


def _try(ip: str, method: str, path: str, payload: Any = None,
         timeout: float = TIMEOUT) -> dict[str, Any]:
    return _ask(where(ip), method, path, payload, timeout)


def _text(body: Any) -> str:
    return body if isinstance(body, str) else ""


def _title(page: str) -> str:
    found = TITLE_PATTERN.search(page)
    return found.group(1).strip() if found else ""


def hosts_in(results: list[dict[str, Any]]) -> list[str]:
    """The names the box redirects to: it is telling us what to call it."""
    found: dict[str, None] = {}
    for item in results:
        location = (item.get("headers") or {}).get("location", "")
        hostname = urlsplit(location).hostname if location.startswith("http") else ""
        if hostname:
            found.setdefault(hostname, None)
    return list(found)


def origins(ip: str, hints: list[str] | None = None) -> list[dict[str, str]]:
    """Every way the box might want to be spoken to, plainest first."""
    names = ["", *dict.fromkeys([*(hints or []), CANONICAL_HOST])]
    return [where(ip, scheme, host) for host in names for scheme in ("http", "https")]


def front_pages(ip: str, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    """Ask each of them for the front page and see which is really the box.

    A first pass by bare IP, then a second pass using whatever hostname the
    first pass redirected to -- the box names itself in its own Location.
    """
    plain = [{"target": target, **_ask(target, "GET", "/", timeout=timeout)}
             for target in origins(ip)[:2]]
    hinted = origins(ip, hosts_in(plain))
    seen = {describe(item["target"]) for item in plain}
    rest = [{"target": target, **_ask(target, "GET", "/", timeout=timeout)}
            for target in hinted if describe(target) not in seen]
    return [{**item, "title": _title(_text(item.get("body")))} for item in plain + rest]


def pick(pages: list[dict[str, Any]]) -> dict[str, str]:
    """The one that answers with the box's own page, preferring the plainest."""
    named = [item for item in pages if item["status"] == 200 and item["title"]]
    working = named or [item for item in pages if item["status"] == 200]
    return (working[0] if working else pages[0])["target"]


# --- reading the box's own web app -------------------------------------------


def assets_of(target: dict[str, str] | str, page: str = "", timeout: float = TIMEOUT) -> list[str]:
    """The scripts the front page pulls in."""
    spot = where(target) if isinstance(target, str) else target
    if not page:
        page = _text(_ask(spot, "GET", "/", timeout=timeout).get("body"))
    if not page:
        return []
    base = f"{spot['scheme']}://{spot['ip']}/"
    own = (spot["ip"].split(":")[0], spot["host"], None)
    found = []
    for reference in ASSET_PATTERN.findall(page):
        if reference.startswith(("http://", "https://")):
            if urlsplit(reference).hostname not in own:
                continue  # only this box's own assets
            found.append(reference)
        else:
            found.append(urljoin(base, reference))
    return list(dict.fromkeys(found))[:ASSET_LIMIT]


def looks_like_path(text: str) -> bool:
    """Could this string be part of a URL the app requests?

    Judged, not matched: absolute paths are obvious, but a bundle more often
    holds the tail of one (``v1/system/deviceinfo``) to be joined onto a base.
    Both are wanted; MIME types, file names and CSS fragments are not.
    """
    if not text or any(space in text for space in " \t<>()[]{}|"):
        return False
    if text.lower().endswith(ASSET_SUFFIXES):
        return False
    if text.startswith("//"):  # protocol-relative, or a stripped comment
        return False
    if text.startswith("/"):
        return len(text) > 1 and ABSOLUTE_PATH.fullmatch(text) is not None
    if "/" not in text or text.split("/")[0].lower() in MIME_HEADS:
        return False
    return RELATIVE_PATH.fullmatch(text) is not None


def _api_ish(path: str) -> bool:
    words = re.split(r"[^a-z0-9]+", path.lower())
    return any(word in API_WORDS for word in words)


def endpoints_in(sources: list[str]) -> list[str]:
    """Every string in those scripts that could be a request path.

    Ordered so the report reads usefully: absolute before relative, and the
    ones naming something an API would name before the rest.
    """
    seen: dict[str, None] = {}
    for text in sources:
        for found in STRING_PATTERN.findall(text[:ASSET_BYTES]):
            candidate = found.strip()
            if looks_like_path(candidate):
                seen.setdefault(candidate, None)
    ranked = sorted(seen, key=lambda path: (not _api_ish(path), not path.startswith("/"),
                                            len(path), path))
    return ranked


def joined(mentioned: list[str], limit: int = 40) -> list[str]:
    """Put the pieces back together.

    ``fetch("/api/"+V+"system/deviceinfo")`` leaves three strings in the
    bundle and no whole URL anywhere.  Asking for each fragment finds
    nothing; asking for the plausible joins finds the API.
    """
    absolute = [path for path in mentioned if path.startswith("/")]
    relative = [path for path in mentioned if not path.startswith("/")]
    bases = [path for path in absolute if path.endswith("/")][:6]
    middles = [path for path in relative if path.endswith("/")][:4]
    tails = [path for path in relative if not path.endswith("/") and _api_ish(path)][:24]

    known = set(absolute)
    built: dict[str, None] = {}
    for base in [*bases, "/"]:
        for tail in tails:
            built.setdefault(base + tail, None)
            for middle in middles:
                built.setdefault(base + middle + tail, None)
    fresh = [path for path in built if path not in known]
    return sorted(fresh, key=lambda path: (path.count("/"), len(path)))[:limit]


def _occurrences(text: str, marker: str, most: int) -> list[int]:
    found, at = [], text.find(marker)
    while at >= 0 and len(found) < most:
        found.append(at)
        at = text.find(marker, at + len(marker))
    return found


def snippets_in(sources: list[str], limit: int = 18, apart: int = 150) -> list[str]:
    """The code around each network call, so the URL joining is visible.

    Minified, but a minifier keeps the strings: seeing ``fetch(x+"v1/status")``
    says more about the API than any list of paths can.  Calls sitting close
    together share one window rather than printing the same code five times.
    """
    seen: dict[str, None] = {}
    for text in sources:
        body = text[:ASSET_BYTES]
        spots = sorted({at for marker in CALL_MARKERS for at in _occurrences(body, marker, 8)})
        last = -apart
        for at in spots:
            if at - last < apart or len(seen) >= limit:
                continue
            last = at
            seen.setdefault(" ".join(body[max(0, at - 80):at + 190].split()), None)
    return list(seen)[:limit]


def chunks_in(target: dict[str, str] | str, sources: list[str], known: list[str]) -> list[str]:
    """Scripts the bundle loads for itself, which the page never mentions."""
    spot = where(target) if isinstance(target, str) else target
    base = f"{spot['scheme']}://{spot['ip']}/"
    already = set(known)
    found: dict[str, None] = {}
    for text in sources:
        for reference in CHUNK_PATTERN.findall(text[:ASSET_BYTES]):
            if reference.startswith(("http://", "https://")):
                continue
            url = urljoin(base, reference.lstrip("./"))
            if url not in already:
                found.setdefault(url, None)
    return list(found)[:ASSET_LIMIT]


def websocket_probe(target: dict[str, str] | str, path: str = "/ws",
                    timeout: float = TIMEOUT) -> dict[str, Any]:
    """Offer a real handshake, since a POST is not what that route wants.

    HTTP 101 means the API is a socket and the whole integration changes
    shape; anything else is still an answer worth reading -- a redirect here
    is how this box told us which name and scheme it answers on.
    """
    spot = where(target) if isinstance(target, str) else target
    scheme, ip, host = spot["scheme"], spot["ip"], spot["host"] or spot["ip"]
    name, _, port = ip.partition(":")
    handshake = "\r\n".join((
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}",
        "Sec-WebSocket-Version: 13",
        f"Origin: {scheme}://{host}",
        "", "",
    )).encode()
    described = {"path": path, "origin": describe(spot)}
    try:
        link = socket.create_connection((name, int(port or (443 if scheme == "https" else 80))),
                                        timeout=timeout)
        if scheme == "https":
            link = net.INSECURE_CONTEXT.wrap_socket(link, server_hostname=host)
        with link:
            link.sendall(handshake)
            answer = link.recv(4096).decode("utf-8", "replace")
    except (OSError, ValueError) as error:
        return {**described, "status": None, "why": str(error), "headers": []}
    head = answer.split("\r\n\r\n")[0].splitlines()
    first = head[0].split() if head else []
    status = int(first[1]) if len(first) > 1 and first[1].isdigit() else None
    return {**described, "status": status,
            "why": "" if head else "closed without answering",
            "headers": [line for line in head[:14] if line.strip()]}


def sah_probe(target: dict[str, str], timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    """Speak the older boxes' dialect properly, in case this one still does."""
    return [_named(_ask(target, "POST", path, payload, timeout, SAH_HEADERS), target)
            for path, payload in SAH_CALLS]


def _named(result: dict[str, Any], target: dict[str, str]) -> dict[str, Any]:
    return {**result, "origin": describe(target), "json": isinstance(result.get("body"), dict),
            "sample": _sample(result.get("body"))}


def socket_paths(mentioned: list[str]) -> list[str]:
    """/ws first, then anything in the app that reads like a socket route."""
    likely = [path for path in mentioned
              if path.startswith("/") and re.search(r"(^/ws$|/ws/|socket|stream|events?$)", path.lower())]
    return list(dict.fromkeys(["/ws", *likely]))[:5]


def read_assets(target: dict[str, str], urls: list[str], timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return list(pool.map(lambda url: _ask(target, "GET", url, timeout=timeout), urls))


def survey(ip: str = DEFAULT_ADDRESS, timeout: float = TIMEOUT) -> dict[str, Any]:
    """Everything that can be learned without a password."""
    pages = front_pages(ip, timeout)
    spot = pick(pages)
    root = next((item for item in pages if item["target"] is spot), pages[0])
    page = _text(root.get("body"))
    title = root.get("title") or _title(page)

    tried = [
        {**result, "json": isinstance(result.get("body"), dict),
         "names_itself": any(mark in _text(result.get("body")).lower() for mark in FINGERPRINTS),
         "sample": _sample(result.get("body"))}
        for result in _in_parallel(spot, CANDIDATES, timeout)
    ]

    controls = [{**result, "sample": _sample(result.get("body"))}
                for result in _in_parallel(spot, CONTROLS, timeout)]

    # The page's own scripts, then the scripts those load for themselves: a
    # bundler splits the app, and the split-off half is where the API often is.
    assets = assets_of(spot, page, timeout)
    fetched = read_assets(spot, assets, timeout)
    bodies = [_text(item.get("body")) for item in fetched]
    extra = chunks_in(spot, bodies, assets)
    if extra:
        fetched += read_assets(spot, extra, timeout)
        bodies = [_text(item.get("body")) for item in fetched]

    mentioned = endpoints_in(bodies)
    snippets = snippets_in(bodies)
    # Both ways round: the box may answer a socket only on the name and scheme
    # it redirects to, which is not the one its front page answers on.
    places = list(dict.fromkeys([describe(spot), *(describe(item["target"]) for item in pages)]))
    lookup = {describe(item["target"]): item["target"] for item in pages}
    sockets = [websocket_probe(lookup.get(place, spot), path, min(timeout, 3.0))
               for path in socket_paths(mentioned) for place in places[:3]]
    dialect = [item for place in places[:3]
               for item in sah_probe(lookup.get(place, spot), timeout)]

    # Whatever the app names, ask for it: that is the point of reading the app.
    # A path already tried above is not asked for twice -- its answer is reused,
    # so this section is the complete picture of what the app calls.
    already = {result["path"]: result for result in tried}
    rebuilt = joined(mentioned)
    askable = [path for path in mentioned if path.startswith("/") and _api_ish(path)] + rebuilt
    fresh = [("GET", path, None) for path in askable if path not in already][:70]
    asked = [
        {**result, "json": isinstance(result.get("body"), dict), "sample": _sample(result.get("body"))}
        for result in _in_parallel(spot, fresh, timeout)
    ]
    by_path = {**{item["path"]: item for item in asked}, **already}
    discovered = [by_path[path] for path in askable if path in by_path]

    return {
        "address": ip,
        "title": title,
        "reachable": root["reached"],
        "spoken_to": describe(spot),
        "origins": [{"origin": describe(item["target"]), "status": item["status"],
                     "title": item["title"], "bytes": len(_text(item.get("body"))),
                     "server": (item.get("headers") or {}).get("server", ""),
                     "location": (item.get("headers") or {}).get("location", ""),
                     "why": item.get("why", "")}
                    for item in pages],
        "candidates": tried,
        "controls": controls,
        "assets": [{"url": item["path"], "status": item["status"], "bytes": len(_text(item.get("body")))}
                   for item in fetched],
        "dialect": dialect,
        "mentioned": mentioned,
        "rebuilt": rebuilt,
        "snippets": snippets,
        "sockets": sockets,
        "discovered": discovered,
    }


def _in_parallel(target: dict[str, str], requests: Any, timeout: float) -> list[dict[str, Any]]:
    requests = list(requests)
    if not requests:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(requests))) as pool:
        return list(pool.map(lambda item: _ask(target, item[0], item[1], item[2], timeout), requests))


def _sample(body: Any) -> str:
    if isinstance(body, dict):
        return str(body)[:300]
    return " ".join(_text(body).split())[:300]


# --- identifying the box -----------------------------------------------------


def gateway_candidates() -> list[str]:
    """Where a home router usually sits: our own subnet's .1, then the defaults."""
    here = net.local_ip()
    guesses = [f"{here.rsplit('.', 1)[0]}.1", "192.168.1.1", "10.0.0.1"]
    return list(dict.fromkeys(guesses))


def probe(ip: str = DEFAULT_ADDRESS, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """Recognise the box from its own front page, without logging in.

    Over whichever scheme and hostname it answers on: this box redirects to a
    name of its own, and a bare-IP request gets a different server's opinion.
    """
    pages = front_pages(ip, timeout)
    if not any(item["reached"] for item in pages):
        return None
    spot = pick(pages)
    root = next((item for item in pages if item["target"] is spot), pages[0])
    page = _text(root.get("body"))
    title = root.get("title") or ""
    named = " ".join(hosts_in(pages))
    haystack = f"{title} {named} {page[:4000]}".lower()
    if not any(mark in haystack for mark in FINGERPRINTS):
        return None
    return {
        "origin": describe(spot),
        "id": f"swisscom-{ip.replace('.', '-').replace(':', '-')}",
        "source": SOURCE,
        "ip": ip,
        "name": title or "Internet-Box",
        "model": title or "Swisscom Internet-Box",
        "password": "",
        "protected": True,  # anything past identification wants the password
    }


def discover(deep: bool = False) -> list[dict[str, Any]]:
    """The box is the gateway, so there are only a few places to look."""
    for address in gateway_candidates():
        found = probe(address, timeout=2.5)
        if found:
            return [found]
    return []


# --- reading -----------------------------------------------------------------


def snapshot(device: dict[str, Any]) -> dict[str, Any]:
    root = _try(device["ip"], "GET", "/", timeout=TIMEOUT)
    if not root["reached"]:
        raise SwisscomError("the box did not answer")
    detail = _read_with_password(device) if device.get("password") else {}
    return {"reachable": True, "detail": detail, "seen": time.time()}


def _read_with_password(device: dict[str, Any]) -> dict[str, Any]:
    """Try the login shapes these boxes are known to accept.

    None of them is confirmed for the Internet-Box 4; if none is accepted the
    dashboard says the box was found but could not be read, rather than
    inventing a status.
    """
    ip, password = device["ip"], device["password"]
    attempts = (
        ("POST", "/api/v1/login", {"username": "admin", "password": password}),
        ("POST", "/api/login", {"username": "admin", "password": password}),
        ("POST", "/ws", {"service": "sah.Device.Information", "method": "createContext",
                         "parameters": {"applicationName": "webui", "username": "admin",
                                        "password": password}}),
    )
    for method, path, payload in attempts:
        result = _try(ip, method, path, payload, TIMEOUT)
        if result["reached"] and result["status"] and result["status"] < 400 and isinstance(result["body"], dict):
            return {"dialect": path, "login": result["body"]}
    return {"error": "none of the known login shapes was accepted"}


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    detail = raw.get("detail") or {}
    readable = bool(detail) and "error" not in detail
    readings = [
        {"kind": "presence", "label": "Reachable", "value": bool(raw.get("reachable")),
         "display": "Yes" if raw.get("reachable") else "No", "valid": True},
        {"kind": "activity", "label": "Local API", "value": readable,
         "display": "Signed in" if readable
         else ("Password not accepted" if device.get("password") else "No password set"),
         "valid": True},
    ]
    return {
        "devices": [
            {
                "id": model.make_id(device["id"], "router", device["id"], SOURCE),
                "rid": device["id"],
                "source": SOURCE,
                "bridge": device["id"],
                "kind": "router",
                "name": device.get("name", "Internet-Box"),
                "product": device.get("model", "Swisscom Internet-Box"),
                "manufacturer": "Swisscom",
                "model": device.get("model", ""),
                "software": "",
                "archetype": "router",
                "room": None,
                "room_name": None,
                "reachable": bool(raw.get("reachable")),
                "controllable": False,
                "capabilities": [],
                "state": {"on": bool(raw.get("reachable")), "brightness": None, "hex": None},
                "readings": readings,
                "buttons": [],
                "battery": None,
                "lights": [],
                "scenes": [],
                "note": (
                    "This box's local API is not published, and the Internet-Box 4 does not answer "
                    "the shapes the older ones did. Run `python3 -m homeiot --probe-box "
                    f"{device.get('ip', '')}` — it reads the box's own web app and reports the "
                    "endpoints it calls, which is what the rest of this needs."
                ),
            }
        ],
        "groups": [],
        "scenes": [],
    }


def make_device(found: dict[str, Any], password: str = "") -> dict[str, Any]:
    return {**found, "password": password, "api": SOURCE, "added_at": time.time()}


# --- the report --------------------------------------------------------------


def report(found: dict[str, Any]) -> str:
    """The prober's output, written to be pasted back verbatim."""
    lines = [f"Internet-Box probe — {found['address']}", ""]
    lines.append(f"  front page: {'answered' if found['reachable'] else 'no answer'}"
                 + (f", titled {found['title']!r}" if found["title"] else ""))

    lines += ["", "  where it answers (a box that redirects is naming the scheme",
              "  and hostname its own UI uses; everything below is asked there):"]
    for item in found.get("origins", []):
        answer = f"HTTP {item['status']}" if item["status"] else f"no answer ({item['why'][:44]})"
        marks = " ".join(filter(None, [
            f"{item['bytes']}b" if item["bytes"] else "",
            f"titled {item['title']!r}" if item["title"] else "",
            f"server {item['server']}" if item["server"] else "",
            f"→ {item['location']}" if item["location"] else "",
        ]))
        lines.append(f"    {item['origin']:<48} {answer} {marks}".rstrip())
    lines.append(f"    chosen: {found.get('spoken_to', '')}")

    lines += ["", "  control (this path certainly does not exist — every other",
              "  answer means whatever differs from these two):"]
    for item in found.get("controls", []):
        lines.append(f"    {_answer_line(item)}")

    lines += ["", "  known paths:"]
    for item in found["candidates"]:
        marks = "" if not item["reached"] else " ".join(filter(None, [
            "JSON" if item["json"] else "", "names itself" if item.get("names_itself") else ""]))
        lines.append(f"    {_answer_line(item)} {marks}".rstrip())
        if item["reached"] and item["status"] and item["status"] < 400 and item["sample"]:
            lines.append(f"          {item['sample'][:160]}")

    lines += ["", "  websocket handshake (a route that resets a POST but takes an",
              "  upgrade is the API, not a dead end):"]
    for item in found.get("sockets", []):
        lines.append(f"    {item['path']:<10} {item.get('origin', ''):<42} "
                     + (f"HTTP {item['status']}" if item["status"] else f"no answer ({item['why'][:44]})"))
        for header in item["headers"][1:]:
            lines.append(f"          {header[:120]}")

    lines += ["", "  the SoftAtHome dialect (the content type is part of the",
              "  protocol, so a plain JSON POST proves nothing about /ws):"]
    for item in found.get("dialect", []):
        answer = f"HTTP {item['status']}" if item["reached"] else f"no answer ({item['why'][:44]})"
        lines.append(f"    {item['path']:<10} {item.get('origin', ''):<42} {answer}"
                     + (" JSON" if item.get("json") else ""))
        if item.get("sample"):
            lines.append(f"          {item['sample'][:200]}")

    lines += ["", f"  scripts read: {len(found['assets'])}"]
    for asset in found["assets"]:
        lines.append(f"    {asset['status']} {asset['bytes']:>8} bytes  {asset['url']}")

    snippets = found.get("snippets", [])
    lines += ["", f"  how the app builds its requests ({len(snippets)} call sites):"]
    for snippet in snippets:
        lines.append(f"    …{snippet[:230]}…")

    mentioned = found["mentioned"]
    lines += ["", f"  strings in those scripts that could be paths: {len(mentioned)}"]
    for path in mentioned[:80]:
        lines.append(f"    {path}")
    if len(mentioned) > 80:
        lines.append(f"    … and {len(mentioned) - 80} more")

    rebuilt = found.get("rebuilt", [])
    lines += ["", f"  paths rebuilt from pieces the app joins: {len(rebuilt)}"]
    for path in rebuilt[:30]:
        lines.append(f"    {path}")

    answered = [item for item in found["discovered"] if item["reached"] and item["status"] and item["status"] < 400]
    lines += ["", f"  of the API-shaped ones asked for, {len(answered)} answered:"]
    for item in answered:
        lines.append(f"    HTTP {item['status']} {'JSON' if item['json'] else '    '} {item['path']}")
        if item["sample"]:
            lines.append(f"          {item['sample'][:160]}")

    lines += ["", "Paste this back and the integration can be written against it."]
    return "\n".join(lines)


def _answer_line(item: dict[str, Any]) -> str:
    """One request, one line, the same shape everywhere in the report."""
    if not item["reached"]:
        why = item.get("why", "") or "no answer"
        return f"{item['method']:<5} {item['path']:<32} no answer ({why[:60]})"
    return f"{item['method']:<5} {item['path']:<32} HTTP {item['status']}"
