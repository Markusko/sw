"""Sonos speakers, over the local UPnP/SOAP API every player serves on :1400.

No account, no cloud, no key: a Sonos answers SSDP as a ZonePlayer, describes
itself at ``/xml/device_description.xml``, and takes SOAP calls on two
services -- AVTransport for what is playing and RenderingControl for how loud.
That is the whole protocol, and it has been stable for a decade.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from . import discovery, model, net

SOURCE = "sonos"
PORT = 1400
TIMEOUT = 5.0

AV_TRANSPORT = ("urn:schemas-upnp-org:service:AVTransport:1", "/MediaRenderer/AVTransport/Control")
RENDERING = ("urn:schemas-upnp-org:service:RenderingControl:1", "/MediaRenderer/RenderingControl/Control")

PLAYING_STATES = {"PLAYING", "TRANSITIONING"}


class SonosError(Exception):
    pass


# --- SOAP --------------------------------------------------------------------


def _envelope(service: str, action: str, arguments: dict[str, Any]) -> bytes:
    body = "".join(f"<{key}>{_escape(value)}</{key}>" for key, value in arguments.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()


def _escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def call(ip: str, endpoint: tuple[str, str], action: str, arguments: dict[str, Any] | None = None) -> dict[str, str]:
    """One SOAP call, returning the response's leaf values by tag name."""
    service, path = endpoint
    arguments = {"InstanceID": 0, **(arguments or {})}
    try:
        status, body = net.request(
            "POST",
            f"http://{ip}:{PORT}{path}",
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'},
            payload=None,
            timeout=TIMEOUT,
            raw_body=_envelope(service, action, arguments),
        )
    except net.HttpError as error:
        raise SonosError(_fault(error) or str(error)) from error
    return _leaves(body if isinstance(body, str) else "")


def _fault(error: net.HttpError) -> str:
    text = error.body if isinstance(error.body, str) else ""
    found = re.search(r"<errorCode>(\d+)</errorCode>", text)
    return f"the speaker refused that (UPnP error {found.group(1)})" if found else ""


def _leaves(xml: str) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return {}
    values = {}
    for element in root.iter():
        tag = element.tag.rpartition("}")[2]
        if element.text and not len(element):
            values[tag] = element.text
    return values


# --- finding speakers --------------------------------------------------------


def probe(ip: str, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """Read a speaker's own description; anything else will not answer this."""
    try:
        _status, body = net.request(
            "GET", f"http://{ip}:{PORT}/xml/device_description.xml", timeout=timeout
        )
    except net.HttpError:
        return None
    details = _leaves(body if isinstance(body, str) else "")
    udn = details.get("UDN", "")
    if "RINCON" not in udn.upper() and "sonos" not in details.get("manufacturer", "").lower():
        return None
    room = details.get("roomName", "")
    return {
        "id": udn.replace("uuid:", "").lower() or f"sonos-{ip.replace('.', '-')}",
        "source": SOURCE,
        "ip": ip,
        "name": room or details.get("friendlyName", "Sonos"),
        "model": details.get("displayName") or details.get("modelName", "Sonos"),
        "room_name": room,
        "firmware": details.get("softwareVersion", ""),
        "protected": False,
    }


def discover(deep: bool = False) -> list[dict[str, Any]]:
    addresses = _safely(_ssdp_addresses)
    found = _verify(dict.fromkeys(addresses))
    if not found and deep:
        base = discovery.subnet_of(net.local_ip())
        found = _verify([f"{base}.{host}" for host in range(1, 255)], timeout=1.0)
    return found


def _ssdp_addresses() -> list[str]:
    return sorted(
        {
            reply["ip"]
            for reply in discovery.ssdp_scan(timeout=3.0)
            if "sonos" in reply["headers"].get("server", "").lower()
            or any("ZonePlayer" in target for target in reply["targets"])
        }
    )


def _verify(addresses, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    candidates = list(addresses)
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(64, len(candidates))) as pool:
        results = pool.map(lambda address: probe(address, timeout=timeout), candidates)
    unique: dict[str, dict[str, Any]] = {}
    for speaker in results:
        if speaker:
            unique.setdefault(speaker["id"], speaker)
    return list(unique.values())


def _safely(call_it) -> list[str]:
    try:
        return call_it() or []
    except Exception:
        return []


# --- reading -----------------------------------------------------------------


def snapshot(device: dict[str, Any]) -> dict[str, Any]:
    ip = device["ip"]
    try:
        transport = call(ip, AV_TRANSPORT, "GetTransportInfo")
        position = call(ip, AV_TRANSPORT, "GetPositionInfo")
        volume = call(ip, RENDERING, "GetVolume", {"Channel": "Master"})
        mute = call(ip, RENDERING, "GetMute", {"Channel": "Master"})
    except SonosError:
        raise
    return {"transport": transport, "position": position, "volume": volume, "mute": mute}


def _track(position: dict[str, str]) -> dict[str, str]:
    """Pull title/artist/album out of the DIDL-Lite blob Sonos returns."""
    metadata = position.get("TrackMetaData", "")
    if not metadata or metadata == "NOT_IMPLEMENTED":
        return {}
    details = _leaves(metadata)
    return {
        "title": details.get("title", ""),
        "artist": details.get("creator", "") or details.get("artist", ""),
        "album": details.get("album", ""),
    }


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    transport = (raw.get("transport") or {}).get("CurrentTransportState", "STOPPED")
    volume = int((raw.get("volume") or {}).get("CurrentVolume", 0) or 0)
    muted = (raw.get("mute") or {}).get("CurrentMute", "0") == "1"
    track = _track(raw.get("position") or {})
    playing = transport in PLAYING_STATES

    described = " — ".join(part for part in (track.get("artist"), track.get("title")) if part)
    readings = []
    if described:
        readings.append(
            {"kind": "now_playing", "label": "Playing", "value": described, "display": described, "valid": True}
        )

    return {
        "devices": [
            {
                "id": model.make_id(device["id"], "player", "0", SOURCE),
                "rid": "0",
                "source": SOURCE,
                "bridge": device["id"],
                "kind": "media",
                "name": device.get("name", "Sonos"),
                "product": device.get("model", "Sonos"),
                "manufacturer": "Sonos",
                "model": device.get("model", ""),
                "software": device.get("firmware", ""),
                "archetype": "speaker",
                "room": None,
                "room_name": device.get("room_name") or None,
                "reachable": True,
                "controllable": True,
                "capabilities": ["transport", "volume"],
                "state": {
                    "on": playing,
                    "playing": playing,
                    "transport": transport,
                    "volume": volume,
                    "muted": muted,
                    "track": track,
                    "brightness": None,
                    "hex": None,
                },
                "readings": readings,
                "buttons": [],
                "battery": None,
                "lights": [],
                "scenes": [],
            }
        ],
        "groups": [],
        "scenes": [],
    }


# --- writing -----------------------------------------------------------------


def send(device: dict[str, Any], _rtype: str, _rid: str, payload: dict[str, Any]) -> Any:
    """Transport and volume.  Anything else is not ours to act on."""
    ip = device["ip"]
    done = []

    if payload.get("play") or (payload.get("on") or {}).get("on") is True:
        call(ip, AV_TRANSPORT, "Play", {"Speed": "1"})
        done.append("play")
    elif payload.get("pause") or (payload.get("on") or {}).get("on") is False:
        # A speaker that is not playing a queue cannot pause; stopping always works.
        try:
            call(ip, AV_TRANSPORT, "Pause")
        except SonosError:
            call(ip, AV_TRANSPORT, "Stop")
        done.append("pause")
    if payload.get("next"):
        call(ip, AV_TRANSPORT, "Next")
        done.append("next")
    if payload.get("previous"):
        call(ip, AV_TRANSPORT, "Previous")
        done.append("previous")
    if payload.get("volume") is not None:
        level = max(0, min(100, int(round(float(payload["volume"])))))
        call(ip, RENDERING, "SetVolume", {"Channel": "Master", "DesiredVolume": level})
        done.append("volume")
    if payload.get("mute") is not None:
        call(ip, RENDERING, "SetMute", {"Channel": "Master", "DesiredMute": "1" if payload["mute"] else "0"})
        done.append("mute")
    return {"applied": done}


def make_device(found: dict[str, Any]) -> dict[str, Any]:
    return {**found, "api": SOURCE, "added_at": time.time()}


def address_of(location: str) -> str:
    return urlsplit(location).hostname or ""
