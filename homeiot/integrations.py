"""The registry of integrations, and what each of them can honestly do.

An integration is a module exposing:

    SOURCE            the namespace its ids live in
    probe(ip)         -> a device description, or None if that is not one
    discover(deep)    -> a list of device descriptions
    snapshot(device)  -> whatever that device will tell us, in its own shape
    home(device, raw) -> {"devices": [...], "groups": [...], "scenes": [...]}
    send(device, rtype, rid, payload)   optional; read-only ones omit it

`ACCESS` records what each one needs from the person setting it up, and how
far it actually goes, so the dashboard can say so rather than pretending.
"""

from __future__ import annotations

from typing import Any

from . import cast, homeconnect, hue, meross, shelly, sonos, swisscom

MODULES = (hue, shelly, sonos, meross, cast, homeconnect, swisscom)

BY_SOURCE = {module.SOURCE: module for module in MODULES}

# What the setup screen shows for each: whether it can be searched for, what
# the person has to supply, and how far the integration goes.
ACCESS: dict[str, dict[str, Any]] = {
    "hue": {
        "label": "Philips Hue",
        "control": "full",
        "needs": "",
        "note": "Lights, plugs, sensors, switches, rooms and scenes.",
    },
    "shelly": {
        "label": "Shelly",
        "control": "full",
        "needs": "",
        "note": "Relays, dimmers and plugs, with their metering.",
    },
    "sonos": {
        "label": "Sonos",
        "control": "full",
        "needs": "",
        "note": "Play, pause, skip and volume, over the speaker's own local API.",
    },
    "meross": {
        "label": "Meross",
        "control": "full",
        "needs": "device key",
        "note": (
            "Thermostats and switches, controlled on your own network. Meross signs every local "
            "request with the key your account set when the device was paired, so the key has to "
            "be supplied once — from the Meross app or from your account at iot.meross.com."
        ),
    },
    "cast": {
        "label": "Google Cast / NVIDIA Shield",
        "control": "read",
        "needs": "",
        "note": (
            "Shows whether the device is awake and what it is playing, which is what Cast "
            "announces on the network. Remote control needs the Cast channel protocol or ADB, "
            "neither of which is implemented."
        ),
    },
    "homeconnect": {
        "label": "Home Connect (Siemens, Bosch)",
        "control": "identify",
        "needs": "",
        "note": (
            "Appliances announce themselves and are listed with their model and type. Reading "
            "their programme state needs the per-appliance keys held in your Home Connect "
            "account, which this cannot fetch for you."
        ),
    },
    "swisscom": {
        "label": "Swisscom Internet-Box",
        "control": "read",
        "needs": "password",
        "note": (
            "Reads what the box's own local API will answer: internet status, uptime and the "
            "devices on your network. The box's password (printed underneath it) is needed."
        ),
    },
}


def module_for(device: dict[str, Any]):
    """The integration that speaks for a stored device."""
    return BY_SOURCE.get(device.get("source", "hue"), hue)


def sources() -> list[dict[str, Any]]:
    return [{"source": source, **detail} for source, detail in ACCESS.items()]


def can_write(source: str) -> bool:
    return hasattr(BY_SOURCE.get(source), "send")
