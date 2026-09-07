"""Philips Hue transport: pairing, reading, writing, and the event stream.

Two dialects are supported and both are presented to the rest of the app in
the CLIP v2 shape, so exactly one normaliser exists (`model.py`):

  * CLIP v2 (`/clip/v2/resource/...`, HTTPS, square bridge, API >= 1.46)
  * CLIP v1 (`/api/<key>/...`, HTTP) -- translated on the fly, which keeps the
    original round bridge and very old firmware usable.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from typing import Any, Callable, Iterable

from . import color, net

APP_NAME = "homeiot"
V2_TYPES = (
    "bridge",
    "device",
    "room",
    "zone",
    "light",
    "grouped_light",
    "scene",
    "smart_scene",
    "motion",
    "temperature",
    "light_level",
    "contact",
    "tamper",
    "button",
    "relative_rotary",
    "device_power",
    "zigbee_connectivity",
    "entertainment_configuration",
)


class LinkButtonError(Exception):
    """The bridge wants the physical link button pressed first."""


class BridgeError(Exception):
    pass


# --- pairing -----------------------------------------------------------------


def pair(ip: str, app_name: str = APP_NAME, instance: str = "dashboard") -> dict[str, Any]:
    """Create an application key.  Raises LinkButtonError until pressed."""
    payload = {"devicetype": f"{app_name}#{instance}", "generateclientkey": True}
    errors: list[str] = []
    for url in (f"https://{ip}/api", f"http://{ip}/api"):
        try:
            _status, body = net.request("POST", url, payload=payload, insecure=True, timeout=8.0)
        except net.HttpError as error:
            errors.append(str(error))
            continue
        entry = body[0] if isinstance(body, list) and body else {}
        if "success" in entry:
            success = entry["success"]
            return {"app_key": success["username"], "client_key": success.get("clientkey", "")}
        description = (entry.get("error") or {}).get("description", "")
        if (entry.get("error") or {}).get("type") == 101 or "link button" in description.lower():
            raise LinkButtonError(description or "press the link button on the bridge")
        errors.append(description or json.dumps(body)[:200])
    raise BridgeError("; ".join(errors) or f"no response from {ip}")


def make_bridge(found: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": found["id"],
        "ip": found["ip"],
        "name": found.get("name") or "Hue Bridge",
        "model": found.get("model", ""),
        "api_version": found.get("api_version", ""),
        "api": "v2" if found.get("supports_v2", True) else "v1",
        "app_key": credentials["app_key"],
        "client_key": credentials.get("client_key", ""),
        "added_at": time.time(),
    }


# --- reading -----------------------------------------------------------------


def _v2_url(bridge: dict[str, Any], path: str) -> str:
    return f"https://{bridge['ip']}/clip/v2/{path.lstrip('/')}"


def _v2_headers(bridge: dict[str, Any]) -> dict[str, str]:
    return {"hue-application-key": bridge["app_key"]}


def fetch_v2(bridge: dict[str, Any], rtype: str) -> list[dict[str, Any]]:
    body = net.get_json(
        _v2_url(bridge, f"resource/{rtype}"), headers=_v2_headers(bridge), insecure=True, timeout=8.0
    )
    if isinstance(body, dict) and body.get("errors"):
        descriptions = "; ".join(str(e.get("description", e)) for e in body["errors"])
        if descriptions:
            raise BridgeError(descriptions)
    data = (body or {}).get("data") if isinstance(body, dict) else None
    return list(data or [])


def snapshot(bridge: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """All resources, keyed by type, in CLIP v2 shape."""
    if bridge.get("api") == "v1":
        return snapshot_v1(bridge)
    try:
        resources = {rtype: fetch_v2(bridge, rtype) for rtype in V2_TYPES}
    except net.HttpError as error:
        if error.status in (401, 403):
            raise BridgeError("bridge rejected the application key -- re-pair the bridge") from error
        raise BridgeError(str(error)) from error
    return resources


# --- writing -----------------------------------------------------------------


def send(bridge: dict[str, Any], rtype: str, rid: str, payload: dict[str, Any]) -> Any:
    if bridge.get("api") == "v1":
        return send_v1(bridge, rtype, rid, payload)
    try:
        _status, body = net.request(
            "PUT",
            _v2_url(bridge, f"resource/{rtype}/{rid}"),
            headers=_v2_headers(bridge),
            payload=payload,
            insecure=True,
            timeout=8.0,
        )
    except net.HttpError as error:
        raise BridgeError(_describe(error)) from error
    if isinstance(body, dict) and body.get("errors"):
        raise BridgeError("; ".join(str(e.get("description", e)) for e in body["errors"]))
    return body


def _describe(error: net.HttpError) -> str:
    body = error.body
    if isinstance(body, dict) and body.get("errors"):
        return "; ".join(str(e.get("description", e)) for e in body["errors"])
    return str(error)


def build_payload(command: dict[str, Any], target: dict[str, Any] | None = None) -> dict[str, Any]:
    """Translate a dashboard command into a CLIP v2 body.

    `target` is the light (or grouped_light) resource, used for gamut clamping
    and for resolving a relative brightness change.
    """
    target = target or {}
    payload: dict[str, Any] = {}

    if "on" in command:
        payload["on"] = {"on": bool(command["on"])}
    elif command.get("toggle"):
        payload["on"] = {"on": not bool(((target.get("on") or {}).get("on")))}

    if "brightness" in command:
        payload["dimming"] = {"brightness": round(color.clamp(float(command["brightness"]), 0, 100), 2)}
    elif "brightness_delta" in command:
        payload["dimming_delta"] = _delta(float(command["brightness_delta"]))

    # A lamp accepts colour *or* colour temperature, never both at once.
    if command.get("hex"):
        payload["color"] = {"xy": _xy(command["hex"], target)}
    elif command.get("xy"):
        point = (float(command["xy"][0]), float(command["xy"][1]))
        clamped = color.clamp_to_gamut(point, color.gamut_from_light(target))
        payload["color"] = {"xy": {"x": round(clamped[0], 4), "y": round(clamped[1], 4)}}
    elif command.get("mirek") or command.get("kelvin"):
        mirek = command.get("mirek") or color.kelvin_to_mirek(float(command["kelvin"]))
        payload["color_temperature"] = {"mirek": int(color.clamp(float(mirek), 153, 500))}

    if command.get("effect"):
        payload["effects"] = {"effect": str(command["effect"])}
    if command.get("alert"):
        payload["alert"] = {"action": str(command["alert"])}
    if command.get("transition") is not None:
        payload["dynamics"] = {"duration": int(color.clamp(float(command["transition"]), 0, 60000))}
    if payload.get("on", {}).get("on") is False:
        # Colour/brightness sent alongside "off" is ignored by the bridge anyway.
        payload = {key: value for key, value in payload.items() if key in ("on", "dynamics")}
    return payload


def _delta(amount: float) -> dict[str, Any]:
    return {
        "action": "up" if amount >= 0 else "down",
        "brightness_delta": round(min(abs(amount), 100), 2),
    }


def _xy(hex_value: str, light: dict[str, Any]) -> dict[str, float]:
    x, y = color.hex_to_xy(hex_value, color.gamut_from_light(light))
    return {"x": round(x, 4), "y": round(y, 4)}


def recall_scene(bridge: dict[str, Any], scene_id: str, options: dict[str, Any] | None = None) -> Any:
    options = options or {}
    recall: dict[str, Any] = {"action": options.get("action", "active")}
    if options.get("brightness") is not None:
        recall["dimming"] = {"brightness": float(options["brightness"])}
    if options.get("duration") is not None:
        recall["duration"] = int(options["duration"])
    if bridge.get("api") == "v1":
        return send_v1(bridge, "scene", scene_id, {"recall": recall})
    return send(bridge, "scene", scene_id, {"recall": recall})


def identify(bridge: dict[str, Any], device_id: str) -> Any:
    if bridge.get("api") == "v1":
        return send_v1(bridge, "light", device_id, {"alert": {"action": "breathe"}})
    return send(bridge, "device", device_id, {"identify": {"action": "identify"}})


def rename(bridge: dict[str, Any], rtype: str, rid: str, name: str) -> Any:
    if bridge.get("api") == "v1":
        return send_v1(bridge, rtype, rid, {"metadata": {"name": name}})
    return send(bridge, rtype, rid, {"metadata": {"name": name[:32]}})


# --- CLIP v1 compatibility ---------------------------------------------------


def _v1_url(bridge: dict[str, Any], path: str = "") -> str:
    return f"http://{bridge['ip']}/api/{bridge['app_key']}/{path.lstrip('/')}"


def snapshot_v1(bridge: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    try:
        everything = net.get_json(_v1_url(bridge), timeout=8.0)
    except net.HttpError as error:
        raise BridgeError(str(error)) from error
    if isinstance(everything, list):  # v1 reports errors as a list
        raise BridgeError(str((everything[0].get("error") or {}).get("description", everything)))
    if not isinstance(everything, dict):
        raise BridgeError("unexpected response from bridge")
    return translate_v1(everything, bridge)


def translate_v1(data: dict[str, Any], bridge: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Turn a v1 `/api/<key>` dump into CLIP v2 shaped resources."""
    resources: dict[str, list[dict[str, Any]]] = {rtype: [] for rtype in V2_TYPES}
    config = data.get("config") or {}
    resources["bridge"].append(
        {
            "id": f"v1-bridge-{bridge['id']}",
            "bridge_id": bridge["id"],
            "owner": {"rid": f"v1-bridgedevice-{bridge['id']}", "rtype": "device"},
            "time_zone": {"time_zone": config.get("timezone", "")},
        }
    )

    for key, light in (data.get("lights") or {}).items():
        resources["light"].append(_v1_light(key, light))
        resources["device"].append(_v1_light_device(key, light))
        resources["zigbee_connectivity"].append(
            {
                "id": f"v1-zc-{key}",
                "owner": {"rid": f"v1-device-{key}", "rtype": "device"},
                "status": "connected" if (light.get("state") or {}).get("reachable") else "connectivity_issue",
            }
        )

    for key, group in (data.get("groups") or {}).items():
        resources[_v1_group_type(group)].append(_v1_group(key, group))
        resources["grouped_light"].append(_v1_grouped_light(key, group))

    for key, scene in (data.get("scenes") or {}).items():
        resources["scene"].append(_v1_scene(key, scene))

    for key, sensor in (data.get("sensors") or {}).items():
        resources_for_sensor = _v1_sensor(key, sensor)
        for rtype, resource in resources_for_sensor:
            resources.setdefault(rtype, []).append(resource)
    return resources


def _v1_light(key: str, light: dict[str, Any]) -> dict[str, Any]:
    state = light.get("state") or {}
    control = (light.get("capabilities") or {}).get("control") or {}
    resource: dict[str, Any] = {
        "id": f"v1-light-{key}",
        "id_v1": f"/lights/{key}",
        "type": "light",
        "owner": {"rid": f"v1-device-{key}", "rtype": "device"},
        "metadata": {"name": light.get("name", f"Light {key}"), "archetype": "unknown_archetype"},
        "on": {"on": bool(state.get("on"))},
        "mode": "normal",
    }
    if "bri" in state:
        resource["dimming"] = {"brightness": round(float(state["bri"]) / 254 * 100, 2), "min_dim_level": 0.2}
    if "ct" in state and control.get("ct"):
        limits = control["ct"]
        resource["color_temperature"] = {
            "mirek": int(state["ct"]),
            "mirek_valid": state.get("colormode") == "ct",
            "mirek_schema": {
                "mirek_minimum": int(limits.get("min", 153)),
                "mirek_maximum": int(limits.get("max", 500)),
            },
        }
    if "xy" in state:
        gamut = control.get("colorgamut")
        resource["color"] = {
            "xy": {"x": float(state["xy"][0]), "y": float(state["xy"][1])},
            "gamut_type": control.get("colorgamuttype", "C"),
        }
        if gamut and len(gamut) == 3:
            resource["color"]["gamut"] = {
                name: {"x": float(point[0]), "y": float(point[1])}
                for name, point in zip(("red", "green", "blue"), gamut)
            }
    if state.get("effect") is not None:
        resource["effects"] = {
            "status": "no_effect" if state["effect"] == "none" else state["effect"],
            "effect_values": ["no_effect", "colorloop"],
        }
    return resource


def _v1_light_device(key: str, light: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"v1-device-{key}",
        "id_v1": f"/lights/{key}",
        "type": "device",
        "metadata": {"name": light.get("name", f"Light {key}"), "archetype": "unknown_archetype"},
        "product_data": {
            "model_id": light.get("modelid", ""),
            "manufacturer_name": light.get("manufacturername", ""),
            "product_name": light.get("productname") or light.get("type", "Light"),
            "software_version": light.get("swversion", ""),
            "certified": bool((light.get("capabilities") or {}).get("certified")),
        },
        "services": [
            {"rid": f"v1-light-{key}", "rtype": "light"},
            {"rid": f"v1-zc-{key}", "rtype": "zigbee_connectivity"},
        ],
    }


def _v1_group_type(group: dict[str, Any]) -> str:
    return "room" if group.get("type") == "Room" else "zone"


def _v1_group(key: str, group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"v1-group-{key}",
        "id_v1": f"/groups/{key}",
        "type": _v1_group_type(group),
        "metadata": {
            "name": group.get("name", f"Group {key}"),
            "archetype": str(group.get("class", "other")).lower().replace(" ", "_"),
        },
        "children": [{"rid": f"v1-device-{light}", "rtype": "device"} for light in group.get("lights", [])],
        "services": [{"rid": f"v1-grouped-{key}", "rtype": "grouped_light"}],
    }


def _v1_grouped_light(key: str, group: dict[str, Any]) -> dict[str, Any]:
    state = group.get("state") or {}
    action = group.get("action") or {}
    resource: dict[str, Any] = {
        "id": f"v1-grouped-{key}",
        "id_v1": f"/groups/{key}",
        "type": "grouped_light",
        "owner": {"rid": f"v1-group-{key}", "rtype": "room"},
        "on": {"on": bool(state.get("any_on"))},
    }
    if "bri" in action:
        resource["dimming"] = {"brightness": round(float(action["bri"]) / 254 * 100, 2)}
    return resource


def _v1_scene(key: str, scene: dict[str, Any]) -> dict[str, Any]:
    group = scene.get("group")
    return {
        "id": f"v1-scene-{key}",
        "id_v1": f"/scenes/{key}",
        "type": "scene",
        "metadata": {"name": scene.get("name", "Scene")},
        "group": {"rid": f"v1-group-{group}", "rtype": "room"} if group else None,
        "status": {"active": "inactive"},
    }


V1_SENSOR_KINDS = {
    "ZLLPresence": ("motion", "presence"),
    "ZLLTemperature": ("temperature", "temperature"),
    "ZLLLightLevel": ("light_level", "lightlevel"),
    "ZLLSwitch": ("button", "buttonevent"),
    "ZGPSwitch": ("button", "buttonevent"),
    "ZLLRelativeRotary": ("relative_rotary", "rotaryevent"),
}


def _v1_sensor(key: str, sensor: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    kind = V1_SENSOR_KINDS.get(sensor.get("type", ""))
    if not kind:
        return []  # CLIP/daylight virtual sensors have no physical device
    rtype, field = kind
    state = sensor.get("state") or {}
    config = sensor.get("config") or {}
    unique = str(sensor.get("uniqueid", key))
    device_id = f"v1-device-s{unique.rsplit('-', 2)[0].replace(':', '')}"
    out: list[tuple[str, dict[str, Any]]] = []

    device = {
        "id": device_id,
        "id_v1": f"/sensors/{key}",
        "type": "device",
        "metadata": {"name": sensor.get("name", f"Sensor {key}"), "archetype": "unknown_archetype"},
        "product_data": {
            "model_id": sensor.get("modelid", ""),
            "manufacturer_name": sensor.get("manufacturername", ""),
            "product_name": sensor.get("productname") or sensor.get("type", "Sensor"),
            "software_version": sensor.get("swversion", ""),
        },
        "services": [{"rid": f"v1-{rtype}-{key}", "rtype": rtype}],
    }
    out.append(("device", device))

    resource: dict[str, Any] = {
        "id": f"v1-{rtype}-{key}",
        "id_v1": f"/sensors/{key}",
        "type": rtype,
        "owner": {"rid": device_id, "rtype": "device"},
        "enabled": bool(config.get("on", True)),
    }
    updated = state.get("lastupdated")
    if rtype == "motion":
        resource["motion"] = {"motion": bool(state.get(field)), "motion_valid": state.get(field) is not None}
    elif rtype == "temperature":
        celsius = float(state.get(field, 0)) / 100
        resource["temperature"] = {"temperature": round(celsius, 2), "temperature_valid": field in state}
    elif rtype == "light_level":
        resource["light"] = {"light_level": int(state.get(field, 0)), "light_level_valid": field in state}
    elif rtype in ("button", "relative_rotary"):
        resource["button"] = {"button_report": {"event": str(state.get(field, "")), "updated": updated}}
    out.append((rtype, resource))

    if config.get("battery") is not None:
        out.append(
            (
                "device_power",
                {
                    "id": f"v1-power-{key}",
                    "type": "device_power",
                    "owner": {"rid": device_id, "rtype": "device"},
                    "power_state": {
                        "battery_level": int(config["battery"]),
                        "battery_state": "normal" if int(config["battery"]) > 15 else "critical",
                    },
                },
            )
        )
    return out


V1_COMMAND_PATHS = {"light": "lights/{id}/state", "grouped_light": "groups/{id}/action"}


def send_v1(bridge: dict[str, Any], rtype: str, rid: str, payload: dict[str, Any]) -> Any:
    """Write a v2-shaped payload back through the v1 API."""
    key = rid.rsplit("-", 1)[-1]
    if rtype == "scene":
        url = _v1_url(bridge, "groups/0/action")
        body: dict[str, Any] = {"scene": key}
    elif rtype in V1_COMMAND_PATHS:
        url = _v1_url(bridge, V1_COMMAND_PATHS[rtype].format(id=key))
        body = to_v1_state(payload)
    elif rtype in ("room", "zone", "device", "light_metadata"):
        name = (payload.get("metadata") or {}).get("name")
        if not name:
            raise BridgeError(f"cannot write {rtype} on a v1 bridge")
        collection = "groups" if rtype in ("room", "zone") else "lights"
        url = _v1_url(bridge, f"{collection}/{key}")
        body = {"name": name}
    else:
        raise BridgeError(f"cannot write {rtype} on a v1 bridge")
    try:
        _status, response = net.request("PUT", url, payload=body, timeout=8.0)
    except net.HttpError as error:
        raise BridgeError(str(error)) from error
    return response


def to_v1_state(payload: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if "on" in payload:
        state["on"] = bool(payload["on"]["on"])
    if "dimming" in payload:
        state["bri"] = int(round(float(payload["dimming"]["brightness"]) / 100 * 254))
    if "dimming_delta" in payload:
        delta = payload["dimming_delta"]
        step = int(round(float(delta["brightness_delta"]) / 100 * 254))
        state["bri_inc"] = step if delta["action"] == "up" else -step
    if "color" in payload:
        point = payload["color"]["xy"]
        state["xy"] = [point["x"], point["y"]]
    if "color_temperature" in payload:
        state["ct"] = int(payload["color_temperature"]["mirek"])
    if "effects" in payload:
        effect = payload["effects"]["effect"]
        state["effect"] = "none" if effect in ("no_effect", "none") else effect
    if "alert" in payload:
        state["alert"] = "select" if payload["alert"]["action"] == "breathe" else "none"
    if "dynamics" in payload:
        state["transitiontime"] = max(0, int(payload["dynamics"]["duration"] / 100))
    return state


# --- event stream ------------------------------------------------------------


def stream_events(
    bridge: dict[str, Any],
    on_events: Callable[[list[dict[str, Any]]], None],
    should_stop: Callable[[], bool],
    on_status: Callable[[str, str], None] = lambda state, detail: None,
) -> None:
    """Follow the bridge's server-sent event stream until told to stop.

    Reconnects with a bounded backoff; a v1 bridge has no stream, so the caller
    falls back to polling.
    """
    backoff = 2.0
    while not should_stop():
        connection = None
        try:
            connection = http.client.HTTPSConnection(
                bridge["ip"], 443, timeout=95, context=net.INSECURE_CONTEXT
            )
            connection.request(
                "GET",
                "/eventstream/clip/v2",
                headers={"hue-application-key": bridge["app_key"], "Accept": "text/event-stream"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise BridgeError(f"event stream returned HTTP {response.status}")
            on_status("connected", "")
            backoff = 2.0
            for events in _read_sse(response, should_stop):
                on_events(events)
        except (OSError, socket.timeout, http.client.HTTPException, BridgeError) as error:
            on_status("reconnecting", str(error))
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if should_stop():
            break
        time.sleep(backoff)
        backoff = min(backoff * 1.8, 30.0)


def _read_sse(response, should_stop: Callable[[], bool]) -> Iterable[list[dict[str, Any]]]:
    payload: list[str] = []
    while not should_stop():
        line = response.readline()
        if not line:
            return
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text.startswith("data:"):
            payload.append(text[5:].strip())
        elif text == "":
            if payload:
                try:
                    events = json.loads("".join(payload))
                except json.JSONDecodeError:
                    events = []
                payload = []
                if isinstance(events, list) and events:
                    yield events
