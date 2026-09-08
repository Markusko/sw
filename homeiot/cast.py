"""Google Cast devices, which is what an NVIDIA Shield is on the network.

Cast devices announce themselves over mDNS and keep their TXT record current:
the friendly name, the model, whether an app is running, and often the title
of what is playing.  That is read without opening a connection at all, and it
is what this integration shows.

Controlling one means either the Cast channel protocol (protobuf over TLS,
then an app-specific JSON dialect) or ADB on the Shield with debugging turned
on and a pairing dance.  Neither is implemented, so nothing here pretends to
offer a remote.
"""

from __future__ import annotations

import time
from typing import Any

from . import discovery, model

SOURCE = "cast"
SERVICE = "_googlecast._tcp.local"

# The TXT keys Cast publishes.  st is 0 when idle, 1 when an app is running.
STATUS_IDLE = "0"


class CastError(Exception):
    pass


def _entries(timeout: float = 3.0) -> list[dict[str, Any]]:
    records = discovery.mdns_query([SERVICE], timeout=timeout)
    index = discovery._index_records(records)
    found = []
    for instance in index["ptr"].get(SERVICE, []):
        service = index["srv"].get(instance)
        address = index["a"].get(service["target"]) if service else None
        if not address:
            continue
        text = index["txt"].get(instance, {})
        found.append({"instance": instance, "ip": address, "port": service["port"], "txt": text})
    return found


def _describe(entry: dict[str, Any]) -> dict[str, Any]:
    text = entry["txt"]
    return {
        "id": (text.get("id") or entry["instance"].split(".")[0]).lower(),
        "source": SOURCE,
        "ip": entry["ip"],
        "port": entry.get("port", 8009),
        "name": text.get("fn") or "Cast device",
        "model": text.get("md", ""),
        "firmware": text.get("ve", ""),
        "protected": False,
    }


def probe(ip: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """Cast has no unauthenticated HTTP endpoint, so we look it up by address."""
    for entry in _entries(timeout=timeout):
        if entry["ip"] == ip:
            return _describe(entry)
    return None


def discover(deep: bool = False) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for entry in _entries():
        described = _describe(entry)
        unique.setdefault(described["id"], described)
    return list(unique.values())


def snapshot(device: dict[str, Any]) -> dict[str, Any]:
    """Re-read the announcement; a Cast device refreshes it as things change."""
    for entry in _entries():
        if entry["ip"] == device["ip"] or (entry["txt"].get("id", "").lower() == device["id"]):
            return {"txt": entry["txt"], "seen": time.time()}
    raise CastError("the device is not announcing itself on the network")


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    text = raw.get("txt") or {}
    running = text.get("st", STATUS_IDLE) != STATUS_IDLE
    playing = text.get("rs", "").strip()  # what Cast calls the receiver status

    readings = [
        {"kind": "activity", "label": "Status", "value": playing or ("Active" if running else "Idle"),
         "display": playing or ("Active" if running else "Idle"), "valid": True}
    ]
    return {
        "devices": [
            {
                "id": model.make_id(device["id"], "device", device["id"], SOURCE),
                "rid": device["id"],
                "source": SOURCE,
                "bridge": device["id"],
                "kind": "media",
                "name": text.get("fn") or device.get("name", "Cast device"),
                "product": text.get("md") or device.get("model", "Google Cast"),
                "manufacturer": "Google Cast",
                "model": text.get("md") or device.get("model", ""),
                "software": text.get("ve") or device.get("firmware", ""),
                "archetype": "media_player",
                "room": None,
                "room_name": None,
                "reachable": True,
                "controllable": False,  # read-only: see the module docstring
                "capabilities": [],
                "state": {"on": running, "playing": running, "brightness": None, "hex": None},
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


def make_device(found: dict[str, Any]) -> dict[str, Any]:
    return {**found, "api": SOURCE, "added_at": time.time()}
