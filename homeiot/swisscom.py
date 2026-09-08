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

Everything it finds is printed. That output is what the rest of this
integration should be written against.

The MQTT service the box advertises is Swisscom's own, for their mesh
repeaters; it needs credentials this cannot obtain, so it is left alone.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import model, net

SOURCE = "swisscom"
TIMEOUT = 5.0
DEFAULT_ADDRESS = "192.168.1.1"
ASSET_LIMIT = 20  # scripts to follow; a router's web app is not large
ASSET_BYTES = 3_000_000

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

FINGERPRINTS = ("internet-box", "internetbox", "swisscom", "arcadyan")

# What an endpoint looks like inside a bundle: a quoted absolute path, and the
# API-ish prefixes worth reporting.
PATH_PATTERN = re.compile(r"""['"`](/(?:api|ws|data|sysbus|cgi|rest|graphql|v1|json)[^'"`\s]{0,120})['"`]""")
ASSET_PATTERN = re.compile(r"""(?:src|href)\s*=\s*['"]([^'"]+\.(?:js|mjs))['"]""", re.IGNORECASE)
TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class SwisscomError(Exception):
    pass


# --- asking politely ---------------------------------------------------------


def _try(ip: str, method: str, path: str, payload: Any = None,
         timeout: float = TIMEOUT) -> dict[str, Any]:
    """One request, described rather than raised."""
    url = path if path.startswith("http") else f"http://{ip}{path}"
    try:
        status, body = net.request(method, url, payload=payload, timeout=timeout)
        return {"path": path, "method": method, "status": status, "body": body, "reached": True}
    except net.HttpError as error:
        # A 401 or 404 is still an answer: the server is there.
        return {
            "path": path,
            "method": method,
            "status": error.status or None,
            "body": error.body,
            "reached": bool(error.status),
            "why": "" if error.status else str(error).rpartition("-> ")[2],
        }


def _text(body: Any) -> str:
    return body if isinstance(body, str) else ""


# --- reading the box's own web app -------------------------------------------


def assets_of(ip: str, timeout: float = TIMEOUT) -> list[str]:
    """The scripts the front page pulls in."""
    root = _try(ip, "GET", "/", timeout=timeout)
    page = _text(root.get("body"))
    if not page:
        return []
    found = []
    for reference in ASSET_PATTERN.findall(page):
        if reference.startswith(("http://", "https://")):
            if urlsplit(reference).hostname not in (ip.split(":")[0], None):
                continue  # only this box's own assets
            found.append(reference)
        else:
            found.append(urljoin(f"http://{ip}/", reference))
    return list(dict.fromkeys(found))[:ASSET_LIMIT]


def endpoints_in(sources: list[str]) -> list[str]:
    """Every absolute, API-shaped path mentioned in those scripts."""
    seen: dict[str, None] = {}
    for text in sources:
        for path in PATH_PATTERN.findall(text[:ASSET_BYTES]):
            seen.setdefault(path.split("?")[0], None)
    return list(seen)


def read_assets(urls: list[str], timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return list(pool.map(lambda url: _try("", "GET", url, timeout=timeout), urls))


def survey(ip: str = DEFAULT_ADDRESS, timeout: float = TIMEOUT) -> dict[str, Any]:
    """Everything that can be learned without a password."""
    root = _try(ip, "GET", "/", timeout=timeout)
    page = _text(root.get("body"))
    title = (TITLE_PATTERN.search(page).group(1).strip() if TITLE_PATTERN.search(page) else "")

    tried = [
        {**result, "json": isinstance(result.get("body"), dict),
         "names_itself": any(mark in _text(result.get("body")).lower() for mark in FINGERPRINTS),
         "sample": _sample(result.get("body"))}
        for result in _in_parallel(ip, CANDIDATES, timeout)
    ]

    assets = assets_of(ip, timeout)
    fetched = read_assets(assets, timeout)
    mentioned = endpoints_in([_text(item.get("body")) for item in fetched])

    # Whatever the app names, ask for it: that is the point of reading the app.
    # A path already tried above is not asked for twice -- its answer is reused,
    # so this section is the complete picture of what the app calls.
    already = {result["path"]: result for result in tried}
    fresh = [("GET", path, None) for path in mentioned if path not in already][:25]
    asked = [
        {**result, "json": isinstance(result.get("body"), dict), "sample": _sample(result.get("body"))}
        for result in _in_parallel(ip, fresh, timeout)
    ]
    by_path = {**{item["path"]: item for item in asked}, **already}
    discovered = [by_path[path] for path in mentioned if path in by_path]

    return {
        "address": ip,
        "title": title,
        "reachable": root["reached"],
        "candidates": tried,
        "assets": [{"url": item["path"], "status": item["status"], "bytes": len(_text(item.get("body")))}
                   for item in fetched],
        "mentioned": mentioned,
        "discovered": discovered,
    }


def _in_parallel(ip: str, requests: Any, timeout: float) -> list[dict[str, Any]]:
    requests = list(requests)
    if not requests:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(requests))) as pool:
        return list(pool.map(lambda item: _try(ip, item[0], item[1], item[2], timeout), requests))


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
    """Recognise the box from its own front page, without logging in."""
    root = _try(ip, "GET", "/", timeout=timeout)
    if not root["reached"]:
        return None
    page = _text(root.get("body"))
    title_match = TITLE_PATTERN.search(page)
    title = title_match.group(1).strip() if title_match else ""
    haystack = f"{title} {page[:4000]}".lower()
    if not any(mark in haystack for mark in FINGERPRINTS):
        return None
    return {
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

    lines += ["", "  known paths:"]
    for item in found["candidates"]:
        if not item["reached"]:
            lines.append(f"    {item['method']:<5} {item['path']:<32} no answer"
                         + (f" ({item.get('why', '')[:60]})" if item.get("why") else ""))
            continue
        marks = " ".join(filter(None, ["JSON" if item["json"] else "",
                                       "names itself" if item.get("names_itself") else ""]))
        lines.append(f"    {item['method']:<5} {item['path']:<32} HTTP {item['status']} {marks}")
        if item["status"] and item["status"] < 400 and item["sample"]:
            lines.append(f"          {item['sample'][:160]}")

    lines += ["", f"  scripts read: {len(found['assets'])}"]
    for asset in found["assets"]:
        lines.append(f"    {asset['status']} {asset['bytes']:>8} bytes  {asset['url']}")

    lines += ["", f"  paths named inside those scripts: {len(found['mentioned'])}"]
    for path in found["mentioned"][:60]:
        lines.append(f"    {path}")

    answered = [item for item in found["discovered"] if item["reached"] and item["status"] and item["status"] < 400]
    lines += ["", f"  of those, {len(answered)} answered:"]
    for item in answered:
        lines.append(f"    HTTP {item['status']} {'JSON' if item['json'] else '    '} {item['path']}")
        if item["sample"]:
            lines.append(f"          {item['sample'][:160]}")

    lines += ["", "Paste this back and the integration can be written against it."]
    return "\n".join(lines)
