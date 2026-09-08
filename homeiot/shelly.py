"""Shelly devices: discovery, reading and control, over plain HTTP.

Two generations answer differently and both are supported:

  * Gen1 (Shelly 1, 2.5, Plug S, H&T …) -- ``/status``, ``/relay/0?turn=on``
  * Gen2+ (Plus, Pro, Gen3/4) -- ``/rpc/Shelly.GetStatus``, ``/rpc/Switch.Set``

``GET /shelly`` is unauthenticated on every generation and says which one you
are talking to, so that is the probe.  Unlike Hue there is no gateway: each
device is its own endpoint, so each is registered as its own source.

Devices protected by a password are reported as such rather than half-working:
Gen1 wants Basic and Gen2 wants Digest, and neither is implemented yet.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import discovery, model, net

SOURCE = "shelly"
TIMEOUT = 4.0


class ShellyError(Exception):
    pass


# --- identifying a device ----------------------------------------------------


def probe(ip: str, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """Ask an address whether it is a Shelly, and which kind."""
    try:
        info = net.get_json(f"http://{ip}/shelly", timeout=timeout)
    except net.HttpError:
        return None
    if not isinstance(info, dict) or not info.get("mac"):
        return None

    generation = int(info.get("gen", 1))
    identifier = str(info.get("id") or f"shelly-{info['mac']}").lower()
    return {
        "id": identifier,
        "source": SOURCE,
        "ip": ip,
        "name": info.get("name") or info.get("app") or info.get("type") or "Shelly",
        "model": info.get("model") or info.get("type", ""),
        "generation": generation,
        "mac": info.get("mac", ""),
        "firmware": info.get("ver") or info.get("fw", ""),
        "protected": bool(info.get("auth_en") or info.get("auth")),
    }


def discover(deep: bool = False) -> list[dict[str, Any]]:
    """mDNS first, then a sweep of our own /24 if that turned nothing up."""
    addresses = _safely(lambda: _mdns_addresses())
    found = _verify(dict.fromkeys(addresses))
    if not found and deep:
        base = discovery.subnet_of(net.local_ip())
        found = _verify([f"{base}.{host}" for host in range(1, 255)], timeout=1.0)
    return found


def _mdns_addresses() -> list[str]:
    records = discovery.mdns_query(["_shelly._tcp.local", "_http._tcp.local"], timeout=3.0)
    index = discovery._index_records(records)
    targets = {
        index["srv"][instance]["target"]
        for service, instances in index["ptr"].items()
        for instance in instances
        if instance in index["srv"] and ("shelly" in service.lower() or "shelly" in instance.lower())
    }
    return sorted({index["a"][target] for target in targets if target in index["a"]})


def _verify(addresses, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    candidates = list(addresses)
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(64, len(candidates))) as pool:
        results = pool.map(lambda address: probe(address, timeout=timeout), candidates)
    unique: dict[str, dict[str, Any]] = {}
    for device in results:
        if device:
            unique.setdefault(device["id"], device)
    return list(unique.values())


def _safely(call) -> list[str]:
    try:
        return call() or []
    except Exception:
        return []


# --- reading -----------------------------------------------------------------


def snapshot(device: dict[str, Any]) -> dict[str, Any]:
    """Everything the device will tell us, in its own shape."""
    if device.get("protected"):
        raise ShellyError("this device has a password set, which is not supported yet")
    base = f"http://{device['ip']}"
    try:
        if int(device.get("generation", 1)) >= 2:
            return {"status": net.get_json(f"{base}/rpc/Shelly.GetStatus", timeout=TIMEOUT)}
        return {
            "status": net.get_json(f"{base}/status", timeout=TIMEOUT),
            "settings": _optional(f"{base}/settings"),
        }
    except net.HttpError as error:
        raise ShellyError(str(error)) from error


def _optional(url: str) -> dict[str, Any]:
    try:
        found = net.get_json(url, timeout=TIMEOUT)
    except net.HttpError:
        return {}
    return found if isinstance(found, dict) else {}


# --- writing -----------------------------------------------------------------


def send(device: dict[str, Any], rtype: str, rid: str, payload: dict[str, Any]) -> Any:
    """Apply a command to one channel of one device."""
    base = f"http://{device['ip']}"
    on = payload.get("on", {}).get("on")
    brightness = (payload.get("dimming") or {}).get("brightness")
    if on is None and brightness is None:
        return None

    if int(device.get("generation", 1)) >= 2:
        method = "Light.Set" if rtype == "light" else "Switch.Set"
        query = [f"id={rid}"]
        if on is not None:
            query.append(f"on={'true' if on else 'false'}")
        if brightness is not None and rtype == "light":
            query.append(f"brightness={int(round(float(brightness)))}")
        url = f"{base}/rpc/{method}?{'&'.join(query)}"
    else:
        channel = "light" if rtype == "light" else "relay"
        query = []
        if on is not None:
            query.append(f"turn={'on' if on else 'off'}")
        if brightness is not None and channel == "light":
            query.append(f"brightness={int(round(float(brightness)))}")
        url = f"{base}/{channel}/{rid}?{'&'.join(query)}"

    try:
        return net.get_json(url, timeout=TIMEOUT)
    except net.HttpError as error:
        raise ShellyError(str(error)) from error


# --- normalising -------------------------------------------------------------


def _reading(kind: str, label: str, value: Any, unit: str, digits: int = 1) -> dict[str, Any]:
    number = round(float(value), digits)
    shown = f"{number:g} {unit}".strip() if unit else f"{number:g}"
    return {"kind": kind, "label": label, "value": number, "display": shown, "unit": unit, "valid": True}


def _channels_gen2(status: dict[str, Any]) -> list[dict[str, Any]]:
    channels = []
    for key, component in sorted(status.items()):
        kind, _, index = key.partition(":")
        if kind not in ("switch", "light") or not index.isdigit():
            continue
        readings = []
        if component.get("apower") is not None:
            readings.append(_reading("power", "Power", component["apower"], "W"))
        if component.get("voltage") is not None:
            readings.append(_reading("voltage", "Voltage", component["voltage"], "V"))
        if (component.get("aenergy") or {}).get("total") is not None:
            readings.append(_reading("energy", "Energy", component["aenergy"]["total"] / 1000, "kWh", 3))
        channels.append(
            {
                "rtype": kind,
                "rid": index,
                "on": bool(component.get("output")),
                "brightness": component.get("brightness"),
                "readings": readings,
            }
        )
    return channels


def _sensors_gen2(status: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    readings, battery = [], None
    for key, component in sorted(status.items()):
        kind = key.partition(":")[0]
        if kind == "temperature" and component.get("tC") is not None:
            readings.append(_reading("temperature", "Temperature", component["tC"], "°C"))
        elif kind == "humidity" and component.get("rh") is not None:
            readings.append(_reading("humidity", "Humidity", component["rh"], "%"))
        elif kind == "illuminance" and component.get("lux") is not None:
            readings.append(_reading("light_level", "Light level", component["lux"], "lx", 0))
        elif kind == "devicepower":
            percent = (component.get("battery") or {}).get("percent")
            if percent is not None:
                battery = {"level": int(percent), "state": "normal" if percent > 15 else "critical"}
    return readings, battery


def _channels_gen1(status: dict[str, Any]) -> list[dict[str, Any]]:
    meters = status.get("meters") or []
    channels = []
    for index, relay in enumerate(status.get("relays") or []):
        readings = []
        if index < len(meters):
            meter = meters[index]
            if meter.get("power") is not None:
                readings.append(_reading("power", "Power", meter["power"], "W"))
            if meter.get("total") is not None:
                # Gen1 counts watt-minutes.
                readings.append(_reading("energy", "Energy", float(meter["total"]) / 60000, "kWh", 3))
        channels.append({"rtype": "switch", "rid": str(index), "on": bool(relay.get("ison")),
                         "brightness": None, "readings": readings})
    for index, light in enumerate(status.get("lights") or []):
        channels.append({"rtype": "light", "rid": str(index), "on": bool(light.get("ison")),
                         "brightness": light.get("brightness"), "readings": []})
    return channels


def _sensors_gen1(status: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    readings, battery = [], None
    temperature = status.get("tmp") or {}
    if temperature.get("value") is not None:
        celsius = float(temperature["value"])
        if str(temperature.get("units", "C")).upper().startswith("F"):
            celsius = (celsius - 32) / 1.8
        readings.append(_reading("temperature", "Temperature", celsius, "°C"))
    humidity = status.get("hum") or {}
    if humidity.get("value") is not None:
        readings.append(_reading("humidity", "Humidity", humidity["value"], "%"))
    power = status.get("bat") or {}
    if power.get("value") is not None:
        battery = {"level": int(power["value"]), "state": "normal" if power["value"] > 15 else "critical"}
    return readings, battery


def home(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Turn one device's status into the dashboard's flat model."""
    status = raw.get("status") or {}
    settings = raw.get("settings") or {}
    generation = int(device.get("generation", 1))

    if generation >= 2:
        channels = _channels_gen2(status)
        readings, battery = _sensors_gen2(status)
        reachable = True
    else:
        channels = _channels_gen1(status)
        readings, battery = _sensors_gen1(status)
        reachable = bool((status.get("wifi_sta") or {}).get("connected", True))

    name = device.get("name") or settings.get("name") or "Shelly"
    product = device.get("model") or "Shelly"
    common = {
        "source": SOURCE,
        "bridge": device["id"],
        "name": name,
        "product": product,
        "manufacturer": "Allterco Shelly",
        "model": device.get("model", ""),
        "software": device.get("firmware", ""),
        "archetype": "shelly",
        "room": None,
        "room_name": None,
        "reachable": reachable,
        "buttons": [],
        "scenes": [],
        "lights": [],
    }

    devices = []
    for index, channel in enumerate(channels):
        dimmable = channel["brightness"] is not None
        devices.append(
            {
                **common,
                "id": model.make_id(device["id"], channel["rtype"], channel["rid"], SOURCE),
                "rid": channel["rid"],
                "kind": "light" if channel["rtype"] == "light" else _switch_kind(product),
                "name": name if len(channels) == 1 else f"{name} {int(channel['rid']) + 1}",
                "controllable": True,
                "capabilities": ["on_off"] + (["dimming"] if dimmable else []),
                "state": {
                    "on": channel["on"],
                    "brightness": float(channel["brightness"]) if dimmable else None,
                    "hex": None,
                },
                # Sensors belong to the device, so they hang off its first channel.
                "readings": channel["readings"] + (readings if index == 0 else []),
                "battery": battery if index == 0 else None,
            }
        )

    if not devices:
        devices.append(
            {
                **common,
                "id": model.make_id(device["id"], "device", device["id"], SOURCE),
                "rid": device["id"],
                "kind": "sensor",
                "controllable": False,
                "capabilities": [],
                "state": {},
                "readings": readings,
                "battery": battery,
            }
        )
    return {"devices": devices, "groups": [], "scenes": []}


def _switch_kind(product: str) -> str:
    """A plug is a plug; anything else switching a load is a relay."""
    return "plug" if "plug" in str(product).lower() else "relay"


def make_device(found: dict[str, Any]) -> dict[str, Any]:
    return {**found, "api": "shelly", "added_at": time.time()}
