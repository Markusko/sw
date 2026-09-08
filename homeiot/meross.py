"""Meross devices on the local network, without going through their cloud.

A Meross device takes JSON over HTTP at ``POST /config``.  Every request
carries a signature -- ``md5(messageId + key + timestamp)`` -- where the key
is the one your Meross account set on the device when it was paired.  Without
that key the device answers "sign error" and nothing else, which is why the
key has to be supplied once; it cannot be discovered from the network.

The MTS200B underfloor-heating thermostat is the device this was written for:
``Appliance.Control.Thermostat.Mode`` carries the current and target
temperature (in tenths of a degree) and whether it is calling for heat.
Meross switches and plugs (``Appliance.Control.ToggleX``) come along for free
because they sit in the same digest.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import discovery, model, net

SOURCE = "meross"
TIMEOUT = 5.0

SYSTEM_ALL = "Appliance.System.All"
THERMOSTAT = "Appliance.Control.Thermostat.Mode"
TOGGLE = "Appliance.Control.ToggleX"

# The thermostat's own numbering.  Setting a temperature by hand implies manual.
MODES = {0: "Comfort", 1: "Sleep", 2: "Away", 3: "Schedule", 4: "Manual"}
MANUAL = 4


class MerossError(Exception):
    pass


class KeyError_(MerossError):
    """The device rejected the signature: wrong key, or none supplied."""


# --- the wire ----------------------------------------------------------------


def _sign(message_id: str, key: str, timestamp: int) -> str:
    return hashlib.md5(f"{message_id}{key}{timestamp}".encode()).hexdigest()


def rpc(ip: str, namespace: str, payload: dict[str, Any], key: str = "",
        method: str = "GET", timeout: float = TIMEOUT) -> dict[str, Any]:
    """One signed call to a device, returning its payload."""
    message_id = uuid.uuid4().hex
    timestamp = int(time.time())
    envelope = {
        "header": {
            "from": "/homeiot",
            "messageId": message_id,
            "method": method,
            "namespace": namespace,
            "payloadVersion": 1,
            "sign": _sign(message_id, key, timestamp),
            "timestamp": timestamp,
        },
        "payload": payload,
    }
    try:
        _status, body = net.request("POST", f"http://{ip}/config", payload=envelope, timeout=timeout)
    except net.HttpError as error:
        raise MerossError(str(error)) from error
    if not isinstance(body, dict) or "header" not in body:
        raise MerossError("that address did not answer like a Meross device")

    header = body.get("header") or {}
    if header.get("method") == "ERROR":
        detail = (body.get("payload") or {}).get("error") or {}
        if int(detail.get("code", 0)) in (5001, 5002):
            raise KeyError_("the device rejected the key — check the device key from your Meross account")
        raise MerossError(str(detail.get("detail") or detail) or "the device refused that request")
    return body.get("payload") or {}


# --- finding devices ---------------------------------------------------------


def probe(ip: str, key: str = "", timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """Identify a Meross.  Without the key it is still recognisable, just mute."""
    try:
        payload = rpc(ip, SYSTEM_ALL, {}, key=key, timeout=timeout)
    except KeyError_:
        return {
            "id": f"meross-{ip.replace('.', '-')}",
            "source": SOURCE,
            "ip": ip,
            "name": "Meross device",
            "model": "",
            "key": key,
            "protected": True,  # answered, but not with the right key
            "needs_key": True,
        }
    except MerossError:
        return None

    system = (payload.get("all") or {}).get("system") or {}
    hardware = system.get("hardware") or {}
    firmware = system.get("firmware") or {}
    return {
        "id": str(hardware.get("uuid") or f"meross-{ip.replace('.', '-')}").lower(),
        "source": SOURCE,
        "ip": ip,
        "name": hardware.get("type", "Meross"),
        "model": hardware.get("type", ""),
        "firmware": firmware.get("version", ""),
        "mac": hardware.get("macAddress", ""),
        "key": key,
        "protected": False,
        "needs_key": False,
    }


def discover(deep: bool = False, key: str = "") -> list[dict[str, Any]]:
    """Meross devices do not announce themselves usefully, so we knock."""
    base = discovery.subnet_of(net.local_ip())
    hosts = [f"{base}.{host}" for host in range(1, 255)] if deep else []
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = pool.map(lambda ip: probe(ip, key=key, timeout=1.5), hosts)
    return [device for device in results if device]


# --- reading -----------------------------------------------------------------


def snapshot(device: dict[str, Any]) -> dict[str, Any]:
    key = device.get("key", "")
    if not key:
        raise MerossError("this device needs its Meross device key before it can be read")
    return {"all": rpc(device["ip"], SYSTEM_ALL, {}, key=key)}


def _tenths(value: Any, fallback: float | None = None) -> float | None:
    try:
        return round(float(value) / 10, 1)
    except (TypeError, ValueError):
        return fallback


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    digest = ((raw.get("all") or {}).get("all") or {}).get("digest") or {}
    name = device.get("name") or "Meross"
    common = {
        "source": SOURCE,
        "bridge": device["id"],
        "product": device.get("model", "Meross"),
        "manufacturer": "Meross",
        "model": device.get("model", ""),
        "software": device.get("firmware", ""),
        "archetype": "meross",
        "room": None,
        "room_name": None,
        "reachable": True,
        "buttons": [],
        "battery": None,
        "lights": [],
        "scenes": [],
    }

    devices = []
    for entry in (digest.get("thermostat") or {}).get("mode", []):
        devices.append({**common, **_thermostat(device, entry, name)})
    for entry in digest.get("togglex", []) or []:
        channel = int(entry.get("channel", 0))
        devices.append(
            {
                **common,
                "id": model.make_id(device["id"], "togglex", str(channel), SOURCE),
                "rid": str(channel),
                "kind": "plug",
                "name": name if channel == 0 else f"{name} {channel + 1}",
                "controllable": True,
                "capabilities": ["on_off"],
                "state": {"on": bool(entry.get("onoff")), "brightness": None, "hex": None},
                "readings": [],
            }
        )

    if not devices:
        devices.append(
            {
                **common,
                "id": model.make_id(device["id"], "device", device["id"], SOURCE),
                "rid": device["id"],
                "kind": "other",
                "name": name,
                "controllable": False,
                "capabilities": [],
                "state": {},
                "readings": [],
            }
        )
    return {"devices": devices, "groups": [], "scenes": []}


def _thermostat(device: dict[str, Any], entry: dict[str, Any], name: str) -> dict[str, Any]:
    channel = int(entry.get("channel", 0))
    current = _tenths(entry.get("currentTemp"))
    target = _tenths(entry.get("targetTemp"))
    heating = bool(entry.get("state"))
    on = bool(entry.get("onoff", 1))
    mode = int(entry.get("mode", MANUAL))

    readings = []
    if current is not None:
        readings.append(
            {"kind": "temperature", "label": "Temperature", "value": current,
             "display": f"{current:.1f}°C", "unit": "°C", "valid": True}
        )
    if target is not None:
        readings.append(
            {"kind": "target_temperature", "label": "Set to", "value": target,
             "display": f"{target:.1f}°C", "unit": "°C", "valid": True}
        )
    readings.append(
        {"kind": "heating", "label": "Heating", "value": heating,
         "display": "Heating" if heating else "Idle", "valid": True}
    )

    return {
        "id": model.make_id(device["id"], "thermostat", str(channel), SOURCE),
        "rid": str(channel),
        "kind": "thermostat",
        "name": name if channel == 0 else f"{name} {channel + 1}",
        "controllable": True,
        "capabilities": ["on_off", "target_temperature"],
        "state": {
            "on": on,
            "heating": heating,
            "current": current,
            "target": target,
            "target_range": [_tenths(entry.get("min"), 5.0), _tenths(entry.get("max"), 35.0)],
            "mode": mode,
            "mode_label": MODES.get(mode, str(mode)),
            "brightness": None,
            "hex": None,
        },
        "readings": readings,
    }


# --- writing -----------------------------------------------------------------


def send(device: dict[str, Any], rtype: str, rid: str, payload: dict[str, Any]) -> Any:
    key = device.get("key", "")
    if not key:
        raise MerossError("this device needs its Meross device key before it can be controlled")
    channel = int(rid or 0)
    on = (payload.get("on") or {}).get("on")

    if rtype == "thermostat":
        body: dict[str, Any] = {"channel": channel}
        if on is not None:
            body["onoff"] = 1 if on else 0
        if payload.get("target") is not None:
            # Setting a temperature by hand means manual, or the schedule
            # would take it straight back.
            body["targetTemp"] = int(round(float(payload["target"]) * 10))
            body["manualTemp"] = body["targetTemp"]
            body["mode"] = MANUAL
        if payload.get("mode") is not None:
            body["mode"] = int(payload["mode"])
        if len(body) == 1:
            return None
        return rpc(device["ip"], THERMOSTAT, {"mode": [body]}, key=key, method="SET")

    if on is None:
        return None
    return rpc(device["ip"], TOGGLE, {"togglex": {"channel": channel, "onoff": 1 if on else 0}},
               key=key, method="SET")


def make_device(found: dict[str, Any], key: str = "") -> dict[str, Any]:
    return {**found, "key": key or found.get("key", ""), "api": SOURCE, "added_at": time.time()}
