"""A simulated bridge, so the dashboard can be built and judged without one.

It serves exactly the CLIP v2 shapes `hue.py` would return, accepts the same
payloads, and drifts its sensors over time.  Start the server with `--demo`.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

from . import color

# A simulated Shelly, so the second integration, the pinned values and the
# camera can all be seen working without any hardware.
SHELLY = {
    "id": "demoshelly1pm",
    "source": "shelly",
    "api": "demo",
    "shelly": True,
    "ip": "0.0.0.0",
    "name": "Boiler relay",
    "model": "SNSW-001P16EU",
    "generation": 2,
    "firmware": "1.4.4",
    "mac": "b0a7327d0e14",
    "protected": False,
    "demo": True,
}

CAMERA = {
    "id": "cam-demo",
    "name": "Front door camera",
    "room": "hue:demo0000bridge:room:room-hallway",
    "rtsp_url": "",
    "snapshot_url": "",
    "demo": True,
}

READOUTS = [
    {"id": "val-demo-temp", "label": "Temperature", "device": "shelly:demoshelly1pm:switch:0",
     "kind": "temperature", "room": "hue:demo0000bridge:room:room-living", "order": 0},
    {"id": "val-demo-hum", "label": "Humidity", "device": "shelly:demoshelly1pm:switch:0",
     "kind": "humidity", "room": "hue:demo0000bridge:room:room-living", "order": 1},
    {"id": "val-demo-power", "label": "Energy usage", "device": "shelly:demoshelly1pm:switch:0",
     "kind": "power", "room": "hue:demo0000bridge:room:room-living", "order": 2},
]

BRIDGE = {
    "id": "demo0000bridge",
    "ip": "0.0.0.0",
    "name": "Demo Bridge",
    "model": "BSB002",
    "api_version": "1.66.0",
    "api": "demo",
    "app_key": "demo",
    "client_key": "",
    "demo": True,
}

# name, product, model, room, kind, archetype
LIGHTS = [
    ("Sofa left", "Hue color lamp", "LCA001", "living", "color", "table_shade"),
    ("Sofa right", "Hue color lamp", "LCA001", "living", "color", "table_shade"),
    ("TV gradient strip", "Hue gradient lightstrip", "LCX004", "living", "color", "hue_lightstrip_tv"),
    ("Play bar", "Hue Play", "LCT024", "living", "color", "hue_play"),
    ("Reading lamp", "Hue ambiance lamp", "LTA001", "living", "ambiance", "floor_shade"),
    ("Ceiling spots", "Hue ambiance spot", "LTG002", "kitchen", "ambiance", "spot_bulb"),
    ("Counter strip", "Hue lightstrip plus", "LST002", "kitchen", "color", "hue_lightstrip"),
    ("Bedside left", "Hue ambiance lamp", "LTA001", "bedroom", "ambiance", "table_shade"),
    ("Bedside right", "Hue ambiance lamp", "LTA001", "bedroom", "ambiance", "table_shade"),
    ("Wardrobe light", "Hue filament bulb", "LWA004", "bedroom", "dimmable", "sultan_bulb"),
    ("Hallway bulb", "Hue white lamp", "LWB010", "hallway", "dimmable", "classic_bulb"),
    ("Coffee machine", "Hue smart plug", "LOM001", "kitchen", "plug", "plug"),
]

ROOMS = [
    ("living", "Living room", "living_room"),
    ("kitchen", "Kitchen", "kitchen"),
    ("bedroom", "Bedroom", "bedroom"),
    ("hallway", "Hallway", "hallway"),
]

ZONES = [("downstairs", "Downstairs", ["living", "kitchen", "hallway"])]

SCENES = [
    ("Relax", "living", ["#ffb26b", "#ff9147", "#ffd7a8"]),
    ("Concentrate", "living", ["#f4f7ff", "#e8eeff"]),
    ("Movie night", "living", ["#3b2bff", "#ff2bb5", "#12045c"]),
    ("Bright", "kitchen", ["#fff6e8", "#ffffff"]),
    ("Dimmed", "bedroom", ["#ff9a3c", "#c96a1e"]),
    ("Nightlight", "bedroom", ["#5a2a00"]),
    ("Arrive home", "hallway", ["#ffd9a0"]),
]

_LOCK = threading.RLock()
_STATE: dict[str, list[dict[str, Any]]] = {}
_LAST_TICK = [0.0]

_SHELLY_STATE: dict[str, Any] = {
    "switch:0": {"id": 0, "output": True, "apower": 842.5, "voltage": 231.4,
                 "current": 3.64, "aenergy": {"total": 128340.0}},
    "temperature:0": {"id": 0, "tC": 21.9},
    "humidity:0": {"id": 0, "rh": 46.0},
    "sys": {"mac": "B0A7327D0E14", "uptime": 90210},
}


# --- construction ------------------------------------------------------------


def _light_resource(index: int, spec: tuple[str, str, str, str, str, str]) -> dict[str, Any]:
    name, _product, _model, _room, kind, _archetype = spec
    resource: dict[str, Any] = {
        "id": f"light-{index}",
        "type": "light",
        "owner": {"rid": f"device-{index}", "rtype": "device"},
        "metadata": {"name": name, "archetype": _archetype},
        "on": {"on": index % 3 != 2},
        "mode": "normal",
    }
    if kind == "plug":
        return resource
    resource["dimming"] = {"brightness": round(random.uniform(35, 100), 1), "min_dim_level": 0.2}
    resource["alert"] = {"action_values": ["breathe"]}
    if kind in ("color", "ambiance"):
        resource["color_temperature"] = {
            "mirek": random.choice([233, 300, 366, 447]),
            "mirek_valid": kind == "ambiance",
            "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
        }
    if kind == "color":
        point = color.hex_to_xy(random.choice(["#ffb26b", "#7fd1ff", "#ff6b9d", "#ffe9c4", "#8bff9d"]))
        resource["color"] = {
            "xy": {"x": round(point[0], 4), "y": round(point[1], 4)},
            "gamut_type": "C",
            "gamut": {
                "red": {"x": 0.6915, "y": 0.3038},
                "green": {"x": 0.17, "y": 0.7},
                "blue": {"x": 0.1532, "y": 0.0475},
            },
        }
        resource["effects"] = {
            "status": "no_effect",
            "effect_values": ["no_effect", "candle", "fire", "sparkle", "glisten", "prism"],
        }
    return resource


def _light_device(index: int, spec: tuple[str, str, str, str, str, str]) -> dict[str, Any]:
    name, product, model, _room, _kind, archetype = spec
    return {
        "id": f"device-{index}",
        "type": "device",
        "metadata": {"name": name, "archetype": archetype},
        "product_data": {
            "product_name": product,
            "manufacturer_name": "Signify Netherlands B.V.",
            "model_id": model,
            "software_version": "1.122.2",
            "certified": True,
        },
        "services": [
            {"rid": f"light-{index}", "rtype": "light"},
            {"rid": f"zc-{index}", "rtype": "zigbee_connectivity"},
        ],
    }


def _accessories() -> dict[str, list[dict[str, Any]]]:
    """Sensors and switches: the non-light half of a real Hue system."""
    return {
        "device": [
            _accessory_device("motion-1", "Hallway motion", "Hue motion sensor", "SML001",
                              [("motion", "motion-1"), ("temperature", "temp-1"),
                               ("light_level", "lux-1"), ("device_power", "power-1")]),
            _accessory_device("dimmer-1", "Bedroom dimmer", "Hue dimmer switch", "RWL022",
                              [("button", "btn-1a"), ("button", "btn-1b"), ("button", "btn-1c"),
                               ("button", "btn-1d"), ("device_power", "power-2")]),
            _accessory_device("dial-1", "Kitchen tap dial", "Hue tap dial switch", "RDM002",
                              [("button", "btn-2a"), ("button", "btn-2b"), ("button", "btn-2c"),
                               ("button", "btn-2d"), ("relative_rotary", "rot-1"), ("device_power", "power-3")]),
            _accessory_device("contact-1", "Front door", "Hue secure contact sensor", "SOC001",
                              [("contact", "contact-1"), ("tamper", "tamper-1"), ("device_power", "power-4")]),
        ],
        "motion": [
            {"id": "motion-1", "type": "motion", "owner": {"rid": "motion-1", "rtype": "device"},
             "enabled": True, "motion": {"motion": False, "motion_valid": True,
                                         "motion_report": {"changed": _stamp(-420), "motion": False}}}
        ],
        "temperature": [
            {"id": "temp-1", "type": "temperature", "owner": {"rid": "motion-1", "rtype": "device"},
             "enabled": True, "temperature": {"temperature": 21.4, "temperature_valid": True,
                                              "temperature_report": {"changed": _stamp(-120), "temperature": 21.4}}}
        ],
        "light_level": [
            {"id": "lux-1", "type": "light_level", "owner": {"rid": "motion-1", "rtype": "device"},
             "enabled": True, "light": {"light_level": 21000, "light_level_valid": True,
                                        "light_level_report": {"changed": _stamp(-120), "light_level": 21000}}}
        ],
        "contact": [
            {"id": "contact-1", "type": "contact", "owner": {"rid": "contact-1", "rtype": "device"},
             "enabled": True, "contact_report": {"changed": _stamp(-3600), "state": "contact"}}
        ],
        "tamper": [
            {"id": "tamper-1", "type": "tamper", "owner": {"rid": "contact-1", "rtype": "device"},
             "tamper_reports": [{"changed": _stamp(-86400), "source": "battery_door", "state": "not_tampered"}]}
        ],
        "button": [
            {"id": f"btn-{suffix}", "type": "button", "owner": {"rid": owner, "rtype": "device"},
             "metadata": {"control_id": control},
             "button": {"last_event": "short_release",
                        "button_report": {"updated": _stamp(-900 * control), "event": "short_release"}}}
            for owner, prefix in (("dimmer-1", "1"), ("dial-1", "2"))
            for control, suffix in enumerate((f"{prefix}a", f"{prefix}b", f"{prefix}c", f"{prefix}d"), start=1)
        ],
        "relative_rotary": [
            {"id": "rot-1", "type": "relative_rotary", "owner": {"rid": "dial-1", "rtype": "device"},
             "relative_rotary": {"last_event": {"action": "start", "rotation": {"direction": "clock_wise",
                                                                               "steps": 12, "duration": 400}}}}
        ],
        "device_power": [
            {"id": f"power-{index}", "type": "device_power", "owner": {"rid": owner, "rtype": "device"},
             "power_state": {"battery_level": level, "battery_state": "normal" if level > 15 else "critical"}}
            for index, (owner, level) in enumerate(
                [("motion-1", 87), ("dimmer-1", 62), ("dial-1", 94), ("contact-1", 12)], start=1
            )
        ],
    }


def _accessory_device(rid: str, name: str, product: str, model: str,
                      services: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": rid,
        "type": "device",
        "metadata": {"name": name, "archetype": "unknown_archetype"},
        "product_data": {
            "product_name": product,
            "manufacturer_name": "Signify Netherlands B.V.",
            "model_id": model,
            "software_version": "2.67.9",
            "certified": True,
        },
        "services": [{"rid": service_id, "rtype": rtype} for rtype, service_id in services]
        + [{"rid": f"zc-{rid}", "rtype": "zigbee_connectivity"}],
    }


def _stamp(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def build_state() -> dict[str, list[dict[str, Any]]]:
    lights = [_light_resource(index, spec) for index, spec in enumerate(LIGHTS)]
    devices = [_light_device(index, spec) for index, spec in enumerate(LIGHTS)]
    accessories = _accessories()

    rooms = [
        {
            "id": f"room-{key}",
            "type": "room",
            "metadata": {"name": name, "archetype": archetype},
            "children": [
                {"rid": f"device-{index}", "rtype": "device"}
                for index, spec in enumerate(LIGHTS)
                if spec[3] == key
            ]
            + [
                {"rid": rid, "rtype": "device"}
                for rid, room in (("motion-1", "hallway"), ("dimmer-1", "bedroom"),
                                  ("dial-1", "kitchen"), ("contact-1", "hallway"))
                if room == key
            ],
            "services": [{"rid": f"grouped-{key}", "rtype": "grouped_light"}],
        }
        for key, name, archetype in ROOMS
    ]
    zones = [
        {
            "id": f"zone-{key}",
            "type": "zone",
            "metadata": {"name": name, "archetype": "other"},
            "children": [
                {"rid": f"device-{index}", "rtype": "device"}
                for index, spec in enumerate(LIGHTS)
                if spec[3] in members
            ],
            "services": [{"rid": f"grouped-{key}", "rtype": "grouped_light"}],
        }
        for key, name, members in ZONES
    ]
    grouped = [
        {
            "id": f"grouped-{key}",
            "type": "grouped_light",
            "owner": {"rid": f"room-{key}", "rtype": "room"},
            "on": {"on": True},
            "dimming": {"brightness": 80.0},
            "alert": {"action_values": ["breathe"]},
        }
        for key, _name, _archetype in ROOMS
    ] + [
        {
            "id": f"grouped-{key}",
            "type": "grouped_light",
            "owner": {"rid": f"zone-{key}", "rtype": "zone"},
            "on": {"on": True},
            "dimming": {"brightness": 70.0},
        }
        for key, _name, _members in ZONES
    ]
    scenes = [
        {
            "id": f"scene-{index}",
            "type": "scene",
            "metadata": {"name": name},
            "group": {"rid": f"room-{room}", "rtype": "room"},
            "status": {"active": "inactive"},
            "actions": [
                {"target": {"rid": f"light-{i}", "rtype": "light"},
                 "action": {"on": {"on": True}, "dimming": {"brightness": 70.0},
                            "color": {"xy": _xy(swatch)}}}
                for i, swatch in enumerate(swatches)
            ],
        }
        for index, (name, room, swatches) in enumerate(SCENES)
    ]

    state: dict[str, list[dict[str, Any]]] = {
        "bridge": [{"id": "bridge-1", "type": "bridge", "bridge_id": BRIDGE["id"],
                    "owner": {"rid": "device-bridge", "rtype": "device"}}],
        "device": devices + accessories["device"] + [
            {"id": "device-bridge", "type": "device",
             "metadata": {"name": "Demo Bridge", "archetype": "bridge_v2"},
             "product_data": {"product_name": "Philips hue", "manufacturer_name": "Signify Netherlands B.V.",
                              "model_id": "BSB002", "software_version": "1.66.0"},
             "services": [{"rid": "bridge-1", "rtype": "bridge"}]}
        ],
        "light": lights,
        "room": rooms,
        "zone": zones,
        "grouped_light": grouped,
        "scene": scenes,
        "zigbee_connectivity": [
            {"id": f"zc-{index}", "type": "zigbee_connectivity",
             "owner": {"rid": f"device-{index}", "rtype": "device"},
             "status": "connected" if index != len(LIGHTS) - 1 else "connectivity_issue"}
            for index in range(len(LIGHTS))
        ] + [
            {"id": f"zc-{rid}", "type": "zigbee_connectivity",
             "owner": {"rid": rid, "rtype": "device"}, "status": "connected"}
            for rid in ("motion-1", "dimmer-1", "dial-1", "contact-1")
        ],
    }
    for rtype, items in accessories.items():
        if rtype != "device":
            state[rtype] = items
    return state


def _xy(swatch: str) -> dict[str, float]:
    x, y = color.hex_to_xy(swatch)
    return {"x": round(x, 4), "y": round(y, 4)}


# --- transport-shaped API ----------------------------------------------------


def snapshot(bridge: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        if bridge.get("shelly"):
            _tick_shelly()
            return {"status": json.loads(json.dumps(_SHELLY_STATE))}
        if not _STATE:
            _STATE.update(build_state())
        _tick()
        return {rtype: [dict(item) for item in items] for rtype, items in _STATE.items()}


def home(bridge: dict[str, Any], raw: Any) -> dict[str, Any]:
    from . import model, shelly

    return shelly.home(bridge, raw) if bridge.get("shelly") else model.build_home(bridge, raw)


def _tick_shelly() -> None:
    """Drift the readings so pinned values visibly move."""
    switch = _SHELLY_STATE["switch:0"]
    if switch["output"]:
        switch["apower"] = round(color.clamp(switch["apower"] + random.uniform(-40, 40), 0, 2400), 1)
        switch["aenergy"]["total"] = round(switch["aenergy"]["total"] + switch["apower"] / 3600, 1)
    else:
        switch["apower"] = 0.0
    switch["voltage"] = round(color.clamp(switch["voltage"] + random.uniform(-0.6, 0.6), 220, 240), 1)
    temperature = _SHELLY_STATE["temperature:0"]
    temperature["tC"] = round(color.clamp(temperature["tC"] + random.uniform(-0.15, 0.15), 17, 26), 1)
    humidity = _SHELLY_STATE["humidity:0"]
    humidity["rh"] = round(color.clamp(humidity["rh"] + random.uniform(-0.5, 0.5), 30, 70), 1)


def send(_bridge: dict[str, Any], rtype: str, rid: str, payload: dict[str, Any]) -> Any:
    from . import model  # local import keeps the module dependency-free at import time

    with _LOCK:
        if _bridge.get("shelly"):
            switch = _SHELLY_STATE.get(f"{rtype}:{rid}")
            if switch is None:
                raise KeyError(f"unknown channel {rtype}:{rid}")
            if "on" in payload:
                switch["output"] = bool(payload["on"]["on"])
                switch["apower"] = 842.5 if switch["output"] else 0.0
            return {"was_on": not switch["output"]}
        if not _STATE:
            _STATE.update(build_state())
        if rtype == "grouped_light":
            group = next(
                (
                    item
                    for kind in ("room", "zone")
                    for item in _STATE[kind]
                    if any(service["rid"] == rid for service in item["services"])
                ),
                None,
            )
            owned = _lights_of(group) if group else []
            for light in owned:
                _apply(light, payload)
            grouped = next(item for item in _STATE["grouped_light"] if item["id"] == rid)
            _apply(grouped, payload)
            return {"data": [{"rid": rid, "rtype": rtype}]}
        if rtype == "scene":
            scene = next(item for item in _STATE["scene"] if item["id"] == rid)
            for other in _STATE["scene"]:
                other["status"] = {"active": "static" if other["id"] == rid else "inactive"}
            for action in scene.get("actions", []):
                light = next((i for i in _STATE["light"] if i["id"] == action["target"]["rid"]), None)
                if light:
                    _apply(light, model.deep_merge({"dynamics": {"duration": 400}}, action["action"]))
            return {"data": [{"rid": rid, "rtype": rtype}]}
        target = next((item for item in _STATE.get(rtype, []) if item["id"] == rid), None)
        if target is None:
            raise KeyError(f"unknown {rtype} {rid}")
        _apply(target, payload)
        return {"data": [{"rid": rid, "rtype": rtype}]}


def _lights_of(group: dict[str, Any]) -> list[dict[str, Any]]:
    device_ids = {child["rid"] for child in group.get("children", [])}
    owned_ids = {
        service["rid"]
        for device in _STATE["device"]
        if device["id"] in device_ids
        for service in device["services"]
        if service["rtype"] == "light"
    }
    return [light for light in _STATE["light"] if light["id"] in owned_ids]


def _apply(resource: dict[str, Any], payload: dict[str, Any]) -> None:
    if "on" in payload:
        resource["on"] = {"on": bool(payload["on"]["on"])}
    if "dimming" in payload and "dimming" in resource:
        resource["dimming"] = {**resource["dimming"], "brightness": float(payload["dimming"]["brightness"])}
    if "dimming_delta" in payload and "dimming" in resource:
        delta = payload["dimming_delta"]
        step = float(delta["brightness_delta"]) * (1 if delta["action"] == "up" else -1)
        level = color.clamp(float(resource["dimming"]["brightness"]) + step, 1, 100)
        resource["dimming"] = {**resource["dimming"], "brightness": round(level, 1)}
    if "color" in payload and "color" in resource:
        resource["color"] = {**resource["color"], "xy": dict(payload["color"]["xy"])}
        if "color_temperature" in resource:
            resource["color_temperature"] = {**resource["color_temperature"], "mirek_valid": False}
    if "color_temperature" in payload and "color_temperature" in resource:
        resource["color_temperature"] = {
            **resource["color_temperature"],
            "mirek": int(payload["color_temperature"]["mirek"]),
            "mirek_valid": True,
        }
    if "effects" in payload and "effects" in resource:
        resource["effects"] = {**resource["effects"], "status": payload["effects"]["effect"]}
    if "metadata" in payload:
        resource["metadata"] = {**resource.get("metadata", {}), **payload["metadata"]}


def _tick() -> None:
    """Drift the sensors so the live view visibly breathes."""
    now = time.time()
    if now - _LAST_TICK[0] < 4:
        return
    _LAST_TICK[0] = now
    temperature = _STATE["temperature"][0]
    value = round(color.clamp(temperature["temperature"]["temperature"] + random.uniform(-0.2, 0.2), 17, 25), 1)
    temperature["temperature"] = {"temperature": value, "temperature_valid": True,
                                  "temperature_report": {"changed": _stamp(0), "temperature": value}}
    level = _STATE["light_level"][0]["light"]["light_level"]
    level = int(color.clamp(level + random.randint(-800, 800), 0, 40000))
    _STATE["light_level"][0]["light"] = {"light_level": level, "light_level_valid": True,
                                         "light_level_report": {"changed": _stamp(0), "light_level": level}}
    if random.random() < 0.12:
        motion = not _STATE["motion"][0]["motion"]["motion"]
        _STATE["motion"][0]["motion"] = {"motion": motion, "motion_valid": True,
                                         "motion_report": {"changed": _stamp(0), "motion": motion}}
