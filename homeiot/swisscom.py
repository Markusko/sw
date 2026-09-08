"""The Swisscom Internet-Box.

Swisscom does not publish this box's local API, and the firmware has changed
its shape across generations, so this integration is written to find out
rather than to assume.  `PROBES` lists the request shapes these boxes are
known to answer -- the Sagemcom-style ``/ws`` RPC, and a couple of REST paths
-- and the first one that replies is the one used from then on.

Run ``python3 -m homeiot --probe-box <address>`` to see exactly what your box
answers.  That output is what any further support should be written against;
guessing past this point would only produce a dashboard that lies.

The box also advertises MQTT on the network.  That broker is Swisscom's own,
for their mesh repeaters, and needs credentials this cannot obtain -- so it is
noted and left alone.
"""

from __future__ import annotations

import time
from typing import Any

from . import model, net

SOURCE = "swisscom"
TIMEOUT = 5.0
DEFAULT_ADDRESS = "192.168.1.1"

# Ways of asking a box who it is.  Each is (label, method, path, payload).
PROBES: tuple[tuple[str, str, str, Any], ...] = (
    ("sah-rpc", "POST", "/ws", {"service": "DeviceInfo", "method": "get", "parameters": {}}),
    ("rest-deviceinfo", "GET", "/api/v1/general/deviceinfo", None),
    ("rest-status", "GET", "/api/v1/general/status", None),
    ("root", "GET", "/", None),
)

FINGERPRINTS = ("internet-box", "internetbox", "swisscom")


class SwisscomError(Exception):
    pass


def _try(ip: str, method: str, path: str, payload: Any, timeout: float) -> tuple[int, Any] | None:
    try:
        return net.request(method, f"http://{ip}{path}", payload=payload, timeout=timeout)
    except net.HttpError as error:
        # A 401 is still an answer: the endpoint is there, it just wants a login.
        return (error.status, error.body) if error.status else None


def survey(ip: str = DEFAULT_ADDRESS, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    """What each known request shape answers.  This is the honest part."""
    results = []
    for label, method, path, payload in PROBES:
        answer = _try(ip, method, path, payload, timeout)
        if answer is None:
            results.append({"probe": label, "path": path, "status": None, "answered": False})
            continue
        status, body = answer
        text = body if isinstance(body, str) else ""
        results.append(
            {
                "probe": label,
                "path": path,
                "status": status,
                "answered": True,
                "json": isinstance(body, dict),
                "looks_like_the_box": any(mark in text.lower() for mark in FINGERPRINTS),
                "sample": (text[:400] if text else body if isinstance(body, dict) else ""),
            }
        )
    return results


def probe(ip: str = DEFAULT_ADDRESS, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """Recognise a box without logging in."""
    answers = survey(ip, timeout)
    reachable = [item for item in answers if item["answered"]]
    if not reachable:
        return None
    if not any(item.get("looks_like_the_box") or item.get("json") for item in reachable):
        return None
    working = next((item["probe"] for item in reachable if item["status"] and item["status"] < 400), "")
    return {
        "id": f"swisscom-{ip.replace('.', '-')}",
        "source": SOURCE,
        "ip": ip,
        "name": "Internet-Box",
        "model": "Swisscom Internet-Box",
        "dialect": working,
        "password": "",
        "protected": True,  # anything beyond identification wants the password
    }


def discover(deep: bool = False) -> list[dict[str, Any]]:
    """The box is the gateway, so there is exactly one place to look."""
    found = probe(DEFAULT_ADDRESS)
    return [found] if found else []


# --- reading -----------------------------------------------------------------


def snapshot(device: dict[str, Any]) -> dict[str, Any]:
    """Identification always; the rest only if a password unlocks a dialect."""
    reachable = _try(device["ip"], "GET", "/", None, TIMEOUT) is not None
    if not reachable:
        raise SwisscomError("the box did not answer")

    detail: dict[str, Any] = {}
    if device.get("password"):
        detail = _read_with_password(device)
    return {"reachable": reachable, "detail": detail, "seen": time.time()}


def _read_with_password(device: dict[str, Any]) -> dict[str, Any]:
    """Try to log in the ways these boxes are known to accept.

    Nothing here is guesswork dressed as fact: if none of the shapes answers,
    the dashboard says the box was found but could not be read.
    """
    ip, password = device["ip"], device["password"]
    attempts = (
        ("POST", "/ws", {"service": "sah.Device.Information", "method": "createContext",
                         "parameters": {"applicationName": "webui", "username": "admin",
                                        "password": password}}),
        ("POST", "/api/v1/login", {"username": "admin", "password": password}),
    )
    for method, path, payload in attempts:
        answer = _try(ip, method, path, payload, TIMEOUT)
        if answer and answer[0] < 400 and isinstance(answer[1], dict):
            return {"dialect": path, "login": answer[1]}
    return {"error": "none of the known login shapes was accepted"}


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    detail = raw.get("detail") or {}
    readable = bool(detail) and "error" not in detail
    readings = [
        {"kind": "presence", "label": "Reachable", "value": bool(raw.get("reachable")),
         "display": "Yes" if raw.get("reachable") else "No", "valid": True},
        {"kind": "activity", "label": "Local API",
         "value": readable,
         "display": "Signed in" if readable else ("Password not accepted" if device.get("password")
                                                  else "No password set"),
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
                    "Swisscom does not document this box's local API. Run "
                    "`python3 -m homeiot --probe-box " + str(device.get("ip", "")) + "` and the output "
                    "will show exactly what yours answers, which is what further support needs."
                ),
            }
        ],
        "groups": [],
        "scenes": [],
    }


def make_device(found: dict[str, Any], password: str = "") -> dict[str, Any]:
    return {**found, "password": password, "api": SOURCE, "added_at": time.time()}
