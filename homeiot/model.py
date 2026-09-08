"""Normalisation: CLIP v2 resources in, one flat home model out.

Nothing here talks to the network.  Given a bag of resources it produces the
document the API and the UI both speak:

    {"devices": [...], "groups": [...], "scenes": [...]}

Ids are namespaced as ``hue:<bridge id>:<resource type>:<resource id>`` so a
single string identifies anything the dashboard can point at.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from . import color

SENSOR_TYPES = ("motion", "temperature", "light_level", "contact", "tamper", "camera_motion")
LIGHT_CAPABILITIES = {
    "dimming": "dimming",
    "color_temperature": "color_temp",
    "color": "color",
    "effects": "effects",
    "gradient": "gradient",
    "timed_effects": "timed_effects",
}
PLUG_ARCHETYPES = {"plug", "hue_lightstrip_plug"}


# --- ids ---------------------------------------------------------------------


def make_id(bridge_id: str, rtype: str, rid: str, source: str = "hue") -> str:
    """An id names one addressable thing: source, gateway, kind, and which one."""
    return f"{source}:{bridge_id}:{rtype}:{rid}"


def parse_id(target_id: str) -> dict[str, str] | None:
    parts = str(target_id).split(":", 3)
    if len(parts) != 4:
        return None
    return {"source": parts[0], "bridge": parts[1], "rtype": parts[2], "rid": parts[3]}


def _by_id(resources: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {resource["id"]: resource for resource in resources if resource.get("id")}


def _owner(resource: dict[str, Any]) -> str | None:
    return (resource.get("owner") or {}).get("rid")


# --- lights ------------------------------------------------------------------


def light_state(light: dict[str, Any]) -> dict[str, Any]:
    dimming = light.get("dimming") or {}
    temperature = light.get("color_temperature") or {}
    schema = temperature.get("mirek_schema") or {}
    tint = light.get("color") or {}
    brightness = float(dimming.get("brightness", 100.0))
    on = bool((light.get("on") or {}).get("on"))

    state: dict[str, Any] = {
        "on": on,
        "brightness": round(brightness, 1) if dimming else None,
        "min_dim_level": dimming.get("min_dim_level"),
        "mirek": temperature.get("mirek") if temperature else None,
        "mirek_valid": bool(temperature.get("mirek_valid")),
        "mirek_range": [
            int(schema.get("mirek_minimum", 153)),
            int(schema.get("mirek_maximum", 500)),
        ]
        if temperature
        else None,
        "xy": tint.get("xy") if tint else None,
        "effect": (light.get("effects") or {}).get("status"),
        "effects": (light.get("effects") or {}).get("effect_values") or [],
        "mode": light.get("mode"),
    }
    state["hex"] = _swatch(state)
    state["kelvin"] = round(color.mirek_to_kelvin(state["mirek"])) if state.get("mirek") else None
    return state


def _swatch(state: dict[str, Any]) -> str:
    brightness = state.get("brightness")
    level = 100.0 if brightness is None else float(brightness)
    point = state.get("xy")
    if point and not state.get("mirek_valid"):
        return color.xy_to_hex(float(point["x"]), float(point["y"]), level)
    if state.get("mirek"):
        warm = color.mirek_to_hex(float(state["mirek"]))
        return color.rgb_to_hex(
            [channel * (0.35 + 0.65 * color.clamp(level / 100)) for channel in color.hex_to_rgb(warm)]
        )
    if point:
        return color.xy_to_hex(float(point["x"]), float(point["y"]), level)
    return color.rgb_to_hex([color.clamp(0.35 + 0.65 * level / 100)] * 3)


def light_capabilities(light: dict[str, Any]) -> list[str]:
    found = ["on_off"] + [name for key, name in LIGHT_CAPABILITIES.items() if light.get(key)]
    if light.get("alert"):
        found.append("alert")
    return found


# --- sensors -----------------------------------------------------------------


def lux_from_level(level: int) -> float:
    """Hue reports light level as 10000*log10(lux)+1."""
    return round(10 ** ((float(level) - 1) / 10000), 1) if level else 0.0


def sensor_reading(rtype: str, resource: dict[str, Any]) -> dict[str, Any] | None:
    if rtype == "motion":
        motion = resource.get("motion") or {}
        report = motion.get("motion_report") or {}
        value = report.get("motion", motion.get("motion"))
        return {
            "kind": "motion",
            "label": "Motion",
            "value": bool(value),
            "display": "Motion" if value else "Clear",
            "valid": bool(motion.get("motion_valid", "motion_report" in motion)),
            "updated": report.get("changed"),
            "enabled": resource.get("enabled", True),
        }
    if rtype == "temperature":
        temperature = resource.get("temperature") or {}
        report = temperature.get("temperature_report") or {}
        value = report.get("temperature", temperature.get("temperature"))
        return {
            "kind": "temperature",
            "label": "Temperature",
            "value": round(float(value), 1) if value is not None else None,
            "display": f"{float(value):.1f}°C" if value is not None else "—",
            "unit": "°C",
            "valid": bool(temperature.get("temperature_valid", "temperature_report" in temperature)),
            "updated": report.get("changed"),
        }
    if rtype == "light_level":
        light = resource.get("light") or {}
        report = light.get("light_level_report") or {}
        level = report.get("light_level", light.get("light_level"))
        lux = lux_from_level(int(level or 0))
        return {
            "kind": "light_level",
            "label": "Light level",
            "value": lux,
            "display": f"{lux:,.0f} lx" if lux else "Dark",
            "unit": "lx",
            "valid": bool(light.get("light_level_valid", "light_level_report" in light)),
            "updated": report.get("changed"),
        }
    if rtype == "contact":
        contact = resource.get("contact_report") or {}
        state = contact.get("state", resource.get("state"))
        return {
            "kind": "contact",
            "label": "Contact",
            "value": state == "contact",
            "display": "Closed" if state == "contact" else "Open",
            "valid": state is not None,
            "updated": contact.get("changed"),
        }
    if rtype == "tamper":
        reports = resource.get("tamper_reports") or []
        latest = reports[0] if reports else {}
        return {
            "kind": "tamper",
            "label": "Tamper",
            "value": latest.get("state") == "tampered",
            "display": "Tampered" if latest.get("state") == "tampered" else "Secure",
            "valid": bool(reports),
            "updated": latest.get("changed"),
        }
    return None


def button_reading(resource: dict[str, Any]) -> dict[str, Any]:
    button = resource.get("button") or {}
    report = button.get("button_report") or {}
    event = report.get("event") or button.get("last_event") or ""
    return {
        "kind": "button",
        "label": f"Button {(resource.get('metadata') or {}).get('control_id', '')}".strip(),
        "value": event,
        "display": str(event).replace("_", " ").title() or "—",
        "updated": report.get("updated"),
        "control_id": (resource.get("metadata") or {}).get("control_id"),
    }


# --- devices -----------------------------------------------------------------


def _device_kind(device: dict[str, Any], services: dict[str, list[dict[str, Any]]]) -> str:
    if services.get("bridge"):
        return "bridge"
    lights = services.get("light") or []
    if lights:
        archetype = (device.get("metadata") or {}).get("archetype", "")
        product = (device.get("product_data") or {}).get("product_name", "").lower()
        if archetype in PLUG_ARCHETYPES or "plug" in product or "socket" in product:
            return "plug"
        return "light"
    if services.get("button") or services.get("relative_rotary"):
        return "switch"
    if any(services.get(rtype) for rtype in SENSOR_TYPES):
        return "sensor"
    return "other"


def build_device(
    bridge_id: str,
    device: dict[str, Any],
    services: dict[str, list[dict[str, Any]]],
    room_of: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    product = device.get("product_data") or {}
    metadata = device.get("metadata") or {}
    kind = _device_kind(device, services)

    lights = [
        {
            "id": make_id(bridge_id, "light", light["id"]),
            "rid": light["id"],
            "name": (light.get("metadata") or {}).get("name") or metadata.get("name", "Light"),
            "capabilities": light_capabilities(light),
            "state": light_state(light),
        }
        for light in services.get("light", [])
    ]

    readings = [
        reading
        for rtype in SENSOR_TYPES
        for resource in services.get(rtype, [])
        if (reading := sensor_reading(rtype, resource))
    ]
    buttons = [button_reading(resource) for resource in services.get("button", [])]
    power = (services.get("device_power") or [{}])[0].get("power_state") or {}
    connectivity = (services.get("zigbee_connectivity") or [{}])[0].get("status")
    room = room_of.get(device["id"])

    merged = _merge_light_state([light["state"] for light in lights]) if lights else {}
    capabilities = sorted({capability for light in lights for capability in light["capabilities"]})

    return {
        "id": make_id(bridge_id, "device", device["id"]),
        "rid": device["id"],
        "source": "hue",
        "bridge": bridge_id,
        "kind": kind,
        "name": metadata.get("name") or product.get("product_name") or "Device",
        "archetype": metadata.get("archetype", "unknown_archetype"),
        "product": product.get("product_name", ""),
        "manufacturer": product.get("manufacturer_name", ""),
        "model": product.get("model_id", ""),
        "software": product.get("software_version", ""),
        "room": room["id"] if room else None,
        "room_name": room["name"] if room else None,
        "reachable": None if connectivity is None else connectivity == "connected",
        "controllable": bool(lights),
        "capabilities": capabilities,
        "lights": lights,
        "state": merged,
        "readings": readings,
        "buttons": buttons,
        "battery": {
            "level": power.get("battery_level"),
            "state": power.get("battery_state"),
        }
        if power
        else None,
        "scenes": [],
    }


def _merge_light_state(states: list[dict[str, Any]]) -> dict[str, Any]:
    """One headline state for a device that owns several light services."""
    if not states:
        return {}
    if len(states) == 1:
        return dict(states[0])
    lit = [state for state in states if state["on"]]
    brightnesses = [state["brightness"] for state in lit if state.get("brightness") is not None]
    swatches = [state["hex"] for state in lit if state.get("hex")]
    first = dict(states[0])
    return {
        **first,
        "on": bool(lit),
        "brightness": round(sum(brightnesses) / len(brightnesses), 1) if brightnesses else first.get("brightness"),
        "hex": color.average_hex(swatches) or first.get("hex"),
        "mixed": len({state["on"] for state in states}) > 1,
    }


# --- groups ------------------------------------------------------------------


def build_group(
    bridge_id: str,
    group: dict[str, Any],
    rtype: str,
    grouped_lights: dict[str, dict[str, Any]],
    devices_by_rid: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metadata = group.get("metadata") or {}
    children = [child["rid"] for child in group.get("children", []) if child.get("rtype") == "device"]
    child_lights = [child["rid"] for child in group.get("children", []) if child.get("rtype") == "light"]
    service = next(
        (
            grouped_lights[item["rid"]]
            for item in group.get("services", [])
            if item.get("rtype") == "grouped_light" and item["rid"] in grouped_lights
        ),
        None,
    )
    members = [devices_by_rid[rid] for rid in children if rid in devices_by_rid]
    lamps = [device for device in members if device["kind"] in ("light", "plug")]
    on_count = sum(1 for device in lamps if device["state"].get("on"))
    brightnesses = [
        device["state"]["brightness"]
        for device in lamps
        if device["state"].get("on") and device["state"].get("brightness") is not None
    ]
    swatches = [device["state"]["hex"] for device in lamps if device["state"].get("on")]

    state = light_state(service) if service else {}
    state.update(
        {
            **_group_color_limits(lamps),
            "on": bool(on_count) if lamps else bool(state.get("on")),
            "any_on": bool(on_count),
            "all_on": bool(lamps) and on_count == len(lamps),
            "count": len(lamps),
            "on_count": on_count,
            "brightness": round(sum(brightnesses) / len(brightnesses), 1)
            if brightnesses
            else state.get("brightness"),
            "hex": color.average_hex(swatches) or state.get("hex"),
        }
    )
    return {
        "id": make_id(bridge_id, rtype, group["id"]),
        "rid": group["id"],
        "source": "hue",
        "bridge": bridge_id,
        "kind": rtype,
        "name": metadata.get("name", rtype.title()),
        "archetype": metadata.get("archetype", "other"),
        "capabilities": _group_capabilities(lamps),
        "grouped_light": make_id(bridge_id, "grouped_light", service["id"]) if service else None,
        "device_ids": [devices_by_rid[rid]["id"] for rid in children if rid in devices_by_rid],
        "light_ids": [make_id(bridge_id, "light", rid) for rid in child_lights],
        "state": state,
        "scenes": [],
    }


def _group_capabilities(members: list[dict[str, Any]]) -> list[str]:
    """What a group can do is the union of what its members can do."""
    return sorted({capability for member in members for capability in member.get("capabilities", [])})


def _group_color_limits(members: list[dict[str, Any]]) -> dict[str, Any]:
    """The widest colour-temperature range any member supports."""
    ranges = [member["state"]["mirek_range"] for member in members if member["state"].get("mirek_range")]
    if not ranges:
        return {}
    return {"mirek_range": [min(low for low, _ in ranges), max(high for _, high in ranges)]}


# --- assembly ----------------------------------------------------------------


def build_home(bridge: dict[str, Any], resources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    bridge_id = bridge["id"]
    devices = resources.get("device") or []
    grouped_lights = _by_id(resources.get("grouped_light") or [])

    # Every service resource, bucketed by the device that owns it.
    services_of: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for rtype, items in resources.items():
        if rtype in ("device", "room", "zone", "scene", "smart_scene", "entertainment_configuration"):
            continue
        for resource in items:
            owner = _owner(resource)
            if owner:
                services_of.setdefault(owner, {}).setdefault(rtype, []).append(resource)

    room_of: dict[str, dict[str, Any]] = {}
    for room in resources.get("room") or []:
        entry = {
            "id": make_id(bridge_id, "room", room["id"]),
            "name": (room.get("metadata") or {}).get("name", "Room"),
        }
        for child in room.get("children", []):
            if child.get("rtype") == "device":
                room_of[child["rid"]] = entry

    built = [
        build_device(bridge_id, device, services_of.get(device["id"], {}), room_of) for device in devices
    ]
    devices_by_rid = {device["rid"]: device for device in built}

    groups = [
        build_group(bridge_id, group, rtype, grouped_lights, devices_by_rid)
        for rtype in ("room", "zone")
        for group in resources.get(rtype) or []
    ]
    groups_by_rid = {group["rid"]: group for group in groups}

    scenes = []
    for scene in resources.get("scene") or []:
        owner = (scene.get("group") or {}).get("rid")
        group = groups_by_rid.get(owner)
        entry = {
            "id": make_id(bridge_id, "scene", scene["id"]),
            "rid": scene["id"],
            "source": "hue",
            "bridge": bridge_id,
            "name": (scene.get("metadata") or {}).get("name", "Scene"),
            "group": group["id"] if group else None,
            "group_name": group["name"] if group else None,
            "active": (scene.get("status") or {}).get("active", "inactive") != "inactive",
            "colors": _scene_colors(scene),
        }
        scenes.append(entry)
        if group:
            group["scenes"].append(entry["id"])

    return {"devices": built, "groups": groups, "scenes": scenes}


def _scene_colors(scene: dict[str, Any]) -> list[str]:
    """A few representative swatches, for the scene chips in the UI."""
    swatches = []
    for action in (scene.get("actions") or [])[:6]:
        body = action.get("action") or {}
        brightness = float((body.get("dimming") or {}).get("brightness", 100))
        point = (body.get("color") or {}).get("xy")
        if point:
            swatches.append(color.xy_to_hex(float(point["x"]), float(point["y"]), brightness))
        elif (body.get("color_temperature") or {}).get("mirek"):
            swatches.append(color.mirek_to_hex(float(body["color_temperature"]["mirek"])))
    return swatches[:5]


def merge_homes(homes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, list[Any]] = {"devices": [], "groups": [], "scenes": []}
    for home in homes:
        for key in merged:
            merged[key].extend(home.get(key, []))
    merged["devices"].sort(key=lambda device: (device["room_name"] or "~", device["name"].lower()))
    merged["groups"].sort(key=lambda group: (group["kind"] != "room", group["name"].lower()))
    merged["scenes"].sort(key=lambda scene: ((scene["group_name"] or "~").lower(), scene["name"].lower()))
    return merged


# --- collections -------------------------------------------------------------


def decorate_collections(collections: list[dict[str, Any]], home: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach live state to the user's own groupings."""
    devices = {device["id"]: device for device in home["devices"]}
    groups = {group["id"]: group for group in home["groups"]}
    decorated = []
    for collection in collections:
        members = [devices.get(member) or groups.get(member) for member in collection["members"]]
        present = [member for member in members if member]
        lamps = [member for member in present if (member.get("lights") or member.get("grouped_light"))]
        on_count = sum(1 for member in lamps if member["state"].get("on"))
        brightnesses = [
            member["state"]["brightness"]
            for member in lamps
            if member["state"].get("on") and member["state"].get("brightness") is not None
        ]
        decorated.append(
            {
                **collection,
                "member_ids": [member["id"] for member in present],
                "missing": len(collection["members"]) - len(present),
                "capabilities": _group_capabilities(lamps),
                "state": {
                    **_group_color_limits(lamps),
                    "on": bool(on_count),
                    "any_on": bool(on_count),
                    "all_on": bool(lamps) and on_count == len(lamps),
                    "count": len(lamps),
                    "on_count": on_count,
                    "brightness": round(sum(brightnesses) / len(brightnesses), 1) if brightnesses else None,
                    "hex": color.average_hex(
                        [member["state"]["hex"] for member in lamps if member["state"].get("on")]
                    ),
                },
            }
        )
    return decorated


# --- write targets -----------------------------------------------------------


def write_targets(home: dict[str, Any], collections: list[dict[str, Any]], target_id: str) -> list[dict[str, str]]:
    """Expand a dashboard id into the concrete resources to PUT.

    Rooms and zones write once to their grouped_light (one bridge round trip
    for the whole room); devices and collections fan out to lights.
    """
    collection = next((item for item in collections if item["id"] == target_id), None)
    if collection is not None:
        return [
            write
            for member in collection["members"]
            for write in write_targets(home, [], member)
        ]

    parsed = parse_id(target_id)
    if not parsed:
        return []
    if parsed["source"] != "hue":
        # Other integrations address the thing itself; there is no service layer.
        return [{"bridge": parsed["bridge"], "rtype": parsed["rtype"], "rid": parsed["rid"]}]
    if parsed["rtype"] in ("room", "zone"):
        group = next((item for item in home["groups"] if item["id"] == target_id), None)
        if group and group["grouped_light"]:
            grouped = parse_id(group["grouped_light"])
            return [{"bridge": grouped["bridge"], "rtype": "grouped_light", "rid": grouped["rid"]}]
        return [write for member in (group or {}).get("device_ids", []) for write in write_targets(home, [], member)]
    if parsed["rtype"] == "device":
        device = next((item for item in home["devices"] if item["id"] == target_id), None)
        return [
            {"bridge": parsed["bridge"], "rtype": "light", "rid": light["rid"]}
            for light in (device or {}).get("lights", [])
        ]
    if parsed["rtype"] in ("light", "grouped_light"):
        return [{"bridge": parsed["bridge"], "rtype": parsed["rtype"], "rid": parsed["rid"]}]
    return []


def find_resource(resources: dict[str, list[dict[str, Any]]], rtype: str, rid: str) -> dict[str, Any] | None:
    return next((item for item in resources.get(rtype, []) if item.get("id") == rid), None)


# --- event application -------------------------------------------------------


def apply_events(
    resources: dict[str, list[dict[str, Any]]], events: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Fold a batch of CLIP v2 events into the raw resource map."""
    updated = {rtype: list(items) for rtype, items in resources.items()}
    changed = False
    for event in events:
        kind = event.get("type", "update")
        for data in event.get("data", []):
            rtype, rid = data.get("type"), data.get("id")
            if not rtype or not rid:
                continue
            bucket = updated.setdefault(rtype, [])
            index = next((i for i, item in enumerate(bucket) if item.get("id") == rid), None)
            if kind == "delete":
                if index is not None:
                    bucket.pop(index)
                    changed = True
            elif index is None:
                bucket.append(dict(data))
                changed = True
            else:
                bucket[index] = deep_merge(bucket[index], data)
                changed = True
    return updated, changed


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        merged[key] = deep_merge(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
    return merged
