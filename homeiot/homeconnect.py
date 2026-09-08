"""Home Connect appliances -- Siemens and Bosch dishwashers, hobs, ovens.

These announce themselves over mDNS as ``_homeconnect._tcp`` and their TXT
record identifies the appliance: its type, brand, model number (the VIB) and
serial.  That much is free, and it is what this integration lists.

Reading a programme or starting one is a different matter.  The appliance
speaks an encrypted socket protocol whose per-appliance key (PSK, and for
older units an AES-IV) exists only inside your Home Connect account; the
official route is their cloud API behind OAuth and a developer registration.
Neither can be conjured from the network, so this integration stops at
identifying the appliance and says so rather than showing empty dials.
"""

from __future__ import annotations

import time
from typing import Any

from . import discovery, model

SOURCE = "homeconnect"
SERVICE = "_homeconnect._tcp.local"

# The TXT keys these publish.
TYPES = {
    "dishwasher": "Dishwasher",
    "hob": "Hob",
    "oven": "Oven",
    "washer": "Washing machine",
    "dryer": "Tumble dryer",
    "fridgefreezer": "Fridge freezer",
    "coffeemaker": "Coffee machine",
    "cooktop": "Hob",
    "hood": "Extractor hood",
}


class HomeConnectError(Exception):
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
        found.append({"instance": instance, "ip": address, "txt": index["txt"].get(instance, {})})
    return found


def _describe(entry: dict[str, Any]) -> dict[str, Any]:
    text = entry["txt"]
    kind = str(text.get("type", "")).lower()
    brand = text.get("brand", "").title() or "Home Connect"
    label = TYPES.get(kind, kind.title() or "Appliance")
    return {
        "id": str(text.get("id") or entry["instance"].split(".")[0]).lower(),
        "source": SOURCE,
        "ip": entry["ip"],
        "name": f"{brand} {label}".strip(),
        "model": text.get("vib", ""),
        "appliance": label,
        "brand": brand,
        "serial": text.get("serialno", ""),
        "firmware": text.get("swversion", ""),
        "protected": True,  # everything past identification needs account keys
    }


def probe(ip: str, timeout: float = 3.0) -> dict[str, Any] | None:
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
    """Confirm it is still on the network.  There is nothing else to read."""
    for entry in _entries():
        if entry["ip"] == device["ip"] or str(entry["txt"].get("id", "")).lower() == device["id"]:
            return {"txt": entry["txt"], "seen": time.time()}
    raise HomeConnectError("the appliance is not announcing itself on the network")


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    text = raw.get("txt") or {}
    return {
        "devices": [
            {
                "id": model.make_id(device["id"], "appliance", device["id"], SOURCE),
                "rid": device["id"],
                "source": SOURCE,
                "bridge": device["id"],
                "kind": "appliance",
                "name": device.get("name", "Appliance"),
                "product": device.get("appliance", "Appliance"),
                "manufacturer": device.get("brand", "Home Connect"),
                "model": device.get("model") or text.get("vib", ""),
                "software": device.get("firmware", ""),
                "archetype": "appliance",
                "room": None,
                "room_name": None,
                "reachable": True,
                "controllable": False,
                "capabilities": [],
                "state": {"on": None, "brightness": None, "hex": None},
                "readings": [
                    {"kind": "presence", "label": "On the network", "value": True,
                     "display": "Yes", "valid": True},
                    {"kind": "serial", "label": "Serial", "value": device.get("serial", ""),
                     "display": device.get("serial", "—"), "valid": bool(device.get("serial"))},
                ],
                "buttons": [],
                "battery": None,
                "lights": [],
                "scenes": [],
                "note": (
                    "Programme state and remote start need the keys held in your Home Connect "
                    "account; this shows what the appliance announces on the network."
                ),
            }
        ],
        "groups": [],
        "scenes": [],
    }


def make_device(found: dict[str, Any]) -> dict[str, Any]:
    return {**found, "api": SOURCE, "added_at": time.time()}
