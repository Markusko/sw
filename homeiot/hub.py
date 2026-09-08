"""The running home: raw bridge data, live updates, and the write path.

One hub instance holds every bridge's resources, keeps them fresh (event
stream where the bridge offers one, polling otherwise), and pushes a rebuilt
snapshot to every connected browser.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from . import camera, demo, discovery, hue, model, shelly, store

POLL_STREAMING = 60.0  # the event stream carries changes; this is a safety net
POLL_PLAIN = 4.0  # bridges without a stream (v1) and the simulator
BROADCAST_INTERVAL = 0.15


def create(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": config,
        "raw": {},
        "status": {},
        "home": {"devices": [], "groups": [], "scenes": []},
        "network": {"hosts": [], "scanned_at": None, "scanning": False},
        "lock": threading.RLock(),
        "subscribers": set(),
        "stopping": threading.Event(),
        "threads": {},
        "revision": 0,
        "pending_broadcast": threading.Event(),
        "started_at": time.time(),
    }


def transport(bridge: dict[str, Any]):
    """Which integration speaks for this gateway or device."""
    if bridge.get("api") == "demo":
        return demo
    return shelly if bridge.get("source") == shelly.SOURCE else hue


def bridges(hub: dict[str, Any]) -> list[dict[str, Any]]:
    return list(hub["config"].get("bridges", []))


def find_bridge(hub: dict[str, Any], bridge_id: str) -> dict[str, Any] | None:
    return next((bridge for bridge in bridges(hub) if bridge["id"] == bridge_id), None)


# --- snapshot ----------------------------------------------------------------


def build_home(bridge: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Each integration normalises its own data into the one shared shape."""
    builder = getattr(transport(bridge), "home", None)
    return builder(bridge, raw) if builder else model.build_home(bridge, raw)


def rebuild(hub: dict[str, Any]) -> dict[str, Any]:
    with hub["lock"]:
        homes = [
            build_home(bridge, hub["raw"][bridge["id"]])
            for bridge in bridges(hub)
            if bridge["id"] in hub["raw"]
        ]
        hub["home"] = model.merge_homes(homes)
        hub["revision"] += 1
        return hub["home"]


def snapshot(hub: dict[str, Any]) -> dict[str, Any]:
    with hub["lock"]:
        home = hub["home"]
        config = hub["config"]
        collections = model.decorate_collections(config.get("collections", []), home)
        return {
            "readouts": resolve_readouts(config.get("readouts", []), home),
            "cameras": [camera.describe(item) for item in config.get("cameras", [])],
            "ffmpeg": camera.have_ffmpeg(),
            "revision": hub["revision"],
            "generated_at": time.time(),
            "bridges": [
                {
                    "id": bridge["id"],
                    "name": bridge.get("name", "Hue Bridge"),
                    "ip": bridge.get("ip", ""),
                    "api": bridge.get("api", "v2"),
                    "source": bridge.get("source", "hue"),
                    "model": bridge.get("model", ""),
                    "demo": bool(bridge.get("demo")),
                    **hub["status"].get(bridge["id"], {}),
                }
                for bridge in bridges(hub)
            ],
            "devices": home["devices"],
            "groups": home["groups"],
            "scenes": home["scenes"],
            "collections": collections,
            "network": hub["network"],
            "ui": config.get("ui", {}),
        }


def resolve_readouts(readouts: list[dict[str, Any]], home: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach the live value to each pinned readout."""
    devices = {device["id"]: device for device in home["devices"]}
    resolved = []
    for readout in readouts:
        device = devices.get(readout["device"])
        reading = next(
            (item for item in (device or {}).get("readings", []) if item["kind"] == readout["kind"]), None
        )
        resolved.append(
            {
                **readout,
                "device_name": device["name"] if device else None,
                "display": reading["display"] if reading else "—",
                "value": reading["value"] if reading else None,
                "unit": (reading or {}).get("unit", ""),
                "missing": device is None or reading is None,
            }
        )
    return resolved


def find_camera(hub: dict[str, Any], camera_id: str) -> dict[str, Any] | None:
    return next((item for item in hub["config"].get("cameras", []) if item["id"] == camera_id), None)


# --- fan-out -----------------------------------------------------------------


def subscribe(hub: dict[str, Any]) -> queue.Queue:
    channel: queue.Queue = queue.Queue(maxsize=8)
    with hub["lock"]:
        hub["subscribers"].add(channel)
    return channel


def unsubscribe(hub: dict[str, Any], channel: queue.Queue) -> None:
    with hub["lock"]:
        hub["subscribers"].discard(channel)


def broadcast(hub: dict[str, Any]) -> None:
    """Ask the broadcaster thread to push the current snapshot."""
    hub["pending_broadcast"].set()


def _broadcast_loop(hub: dict[str, Any]) -> None:
    while not hub["stopping"].is_set():
        if not hub["pending_broadcast"].wait(timeout=0.5):
            continue
        hub["pending_broadcast"].clear()
        time.sleep(BROADCAST_INTERVAL)  # coalesce bursts from the event stream
        payload = snapshot(hub)
        with hub["lock"]:
            channels = list(hub["subscribers"])
        for channel in channels:
            try:
                channel.put_nowait(payload)
            except queue.Full:
                pass  # a slow client simply misses an intermediate frame


# --- refreshing --------------------------------------------------------------


def set_status(hub: dict[str, Any], bridge_id: str, **fields: Any) -> None:
    with hub["lock"]:
        hub["status"][bridge_id] = {**hub["status"].get(bridge_id, {}), **fields}


def refresh(hub: dict[str, Any], bridge: dict[str, Any]) -> bool:
    try:
        resources = transport(bridge).snapshot(bridge)
    except Exception as error:
        set_status(hub, bridge["id"], connected=False, error=str(error))
        broadcast(hub)
        return False
    with hub["lock"]:
        hub["raw"][bridge["id"]] = resources
    set_status(hub, bridge["id"], connected=True, error="", last_update=time.time())
    rebuild(hub)
    broadcast(hub)
    return True


def refresh_all(hub: dict[str, Any]) -> None:
    for bridge in bridges(hub):
        refresh(hub, bridge)


def _poll_loop(hub: dict[str, Any], bridge: dict[str, Any]) -> None:
    while not hub["stopping"].is_set() and find_bridge(hub, bridge["id"]):
        streaming = hub["status"].get(bridge["id"], {}).get("stream") == "connected"
        interval = POLL_STREAMING if streaming else POLL_PLAIN
        if hub["stopping"].wait(timeout=interval):
            return
        if find_bridge(hub, bridge["id"]):
            refresh(hub, bridge)


def _event_loop(hub: dict[str, Any], bridge: dict[str, Any]) -> None:
    def on_events(events: list[dict[str, Any]]) -> None:
        with hub["lock"]:
            current = hub["raw"].get(bridge["id"])
            if current is None:
                return
            updated, changed = model.apply_events(current, events)
            if not changed:
                return
            hub["raw"][bridge["id"]] = updated
        rebuild(hub)
        broadcast(hub)

    def on_status(state: str, detail: str) -> None:
        set_status(hub, bridge["id"], stream=state, stream_error=detail)
        broadcast(hub)

    hue.stream_events(
        bridge,
        on_events,
        should_stop=lambda: hub["stopping"].is_set() or not find_bridge(hub, bridge["id"]),
        on_status=on_status,
    )
    set_status(hub, bridge["id"], stream="stopped")


def _spawn(hub: dict[str, Any], name: str, target: Callable[[], None]) -> None:
    with hub["lock"]:
        existing = hub["threads"].get(name)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(target=target, name=name, daemon=True)
        hub["threads"][name] = thread
    thread.start()


def resync(hub: dict[str, Any]) -> None:
    """Start workers for any bridge that does not have them yet."""
    for bridge in bridges(hub):
        bridge_id = bridge["id"]
        if bridge_id not in hub["raw"]:
            _spawn(hub, f"initial-{bridge_id}", lambda b=bridge: refresh(hub, b))
        _spawn(hub, f"poll-{bridge_id}", lambda b=bridge: _poll_loop(hub, b))
        if bridge.get("api") == "v2":
            set_status(hub, bridge_id, stream="connecting")
            _spawn(hub, f"events-{bridge_id}", lambda b=bridge: _event_loop(hub, b))
        else:
            set_status(hub, bridge_id, stream="polling")
    with hub["lock"]:
        known = {bridge["id"] for bridge in bridges(hub)}
        for stale in set(hub["raw"]) - known:
            hub["raw"].pop(stale, None)
            hub["status"].pop(stale, None)
    rebuild(hub)
    broadcast(hub)


def start(hub: dict[str, Any]) -> dict[str, Any]:
    _spawn(hub, "broadcast", lambda: _broadcast_loop(hub))
    resync(hub)
    return hub


def stop(hub: dict[str, Any]) -> None:
    hub["stopping"].set()
    hub["pending_broadcast"].set()


# --- configuration mutations -------------------------------------------------


def mutate_config(hub: dict[str, Any], mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    with hub["lock"]:
        config = store.save(mutator(hub["config"]))
        hub["config"] = config
    broadcast(hub)
    return config


def add_bridge(hub: dict[str, Any], bridge: dict[str, Any]) -> dict[str, Any]:
    mutate_config(hub, lambda config: store.put_bridge(config, bridge))
    resync(hub)
    refresh(hub, bridge)
    return bridge


def remove_bridge(hub: dict[str, Any], bridge_id: str) -> None:
    with hub["lock"]:
        gone = {device["id"] for device in hub["home"]["devices"] if device["bridge"] == bridge_id}
    mutate_config(hub, lambda config: store.forget_devices(store.drop_bridge(config, bridge_id), gone))
    with hub["lock"]:
        hub["raw"].pop(bridge_id, None)
        hub["status"].pop(bridge_id, None)
    rebuild(hub)
    broadcast(hub)


# --- commands ----------------------------------------------------------------


def command(hub: dict[str, Any], target_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Apply a command to a light, device, room, zone or collection."""
    with hub["lock"]:
        home = hub["home"]
        collections = hub["config"].get("collections", [])
        writes = model.write_targets(home, collections, target_id)
        raw = {bridge_id: resources for bridge_id, resources in hub["raw"].items()}

    if not writes:
        raise LookupError(f"unknown target {target_id}")

    results, failures, events = [], [], []
    for write in writes:
        bridge = find_bridge(hub, write["bridge"])
        if not bridge:
            failures.append(f"bridge {write['bridge']} is not connected")
            continue
        resource = model.find_resource(raw.get(bridge["id"], {}), write["rtype"], write["rid"])
        payload = hue.build_payload(body, resource)
        if not payload:
            continue
        try:
            transport(bridge).send(bridge, write["rtype"], write["rid"], payload)
            results.append({**write, "payload": payload})
            # Only Hue keeps CLIP v2 resources, so only Hue can be patched
            # ahead of the bridge confirming.  The rest are re-read instead.
            if bridge.get("source", "hue") == "hue":
                events.append(
                    {"bridge": bridge["id"], "data": {**payload, "id": write["rid"], "type": write["rtype"]}}
                )
        except Exception as error:
            failures.append(str(error))

    _apply_optimistically(hub, events)
    _confirm(hub, {write["bridge"] for write in writes})
    return {"ok": not failures, "applied": len(results), "errors": failures}


def _confirm(hub: dict[str, Any], bridge_ids: set[str]) -> None:
    """A streaming bridge tells us what happened; anything else must be re-read."""
    for bridge_id in bridge_ids:
        bridge = find_bridge(hub, bridge_id)
        if bridge and hub["status"].get(bridge_id, {}).get("stream") != "connected":
            _schedule_refresh(hub, bridge)


def _apply_optimistically(hub: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Reflect a successful write immediately; the bridge confirms right after."""
    if not events:
        return
    with hub["lock"]:
        for event in events:
            resources = hub["raw"].get(event["bridge"])
            if resources is None:
                continue
            updated, _changed = model.apply_events(resources, [{"type": "update", "data": [event["data"]]}])
            hub["raw"][event["bridge"]] = updated
    rebuild(hub)
    broadcast(hub)


def recall_scene(hub: dict[str, Any], scene_id: str, options: dict[str, Any]) -> dict[str, Any]:
    parsed = model.parse_id(scene_id)
    bridge = find_bridge(hub, parsed["bridge"]) if parsed else None
    if not bridge:
        raise LookupError(f"unknown scene {scene_id}")
    if bridge.get("api") == "demo":
        demo.send(bridge, "scene", parsed["rid"], {"recall": {"action": "active"}})
    else:
        hue.recall_scene(bridge, parsed["rid"], options)
    _schedule_refresh(hub, bridge)
    return {"ok": True}


def identify(hub: dict[str, Any], device_id: str) -> dict[str, Any]:
    parsed = model.parse_id(device_id)
    bridge = find_bridge(hub, parsed["bridge"]) if parsed else None
    if not bridge:
        raise LookupError(f"unknown device {device_id}")
    if bridge.get("api") == "demo":
        return {"ok": True, "demo": True}
    hue.identify(bridge, parsed["rid"])
    return {"ok": True}


def rename(hub: dict[str, Any], target_id: str, name: str) -> dict[str, Any]:
    parsed = model.parse_id(target_id)
    bridge = find_bridge(hub, parsed["bridge"]) if parsed else None
    if not bridge or not name.strip():
        raise LookupError(f"cannot rename {target_id}")
    transport(bridge).send(bridge, parsed["rtype"], parsed["rid"], {"metadata": {"name": name.strip()[:32]}})
    _schedule_refresh(hub, bridge)
    return {"ok": True}


def _schedule_refresh(hub: dict[str, Any], bridge: dict[str, Any], delay: float = 0.4) -> None:
    """Re-read the bridge shortly, coalescing bursts (a dragged slider) into one."""
    with hub["lock"]:
        timers = hub.setdefault("refresh_timers", {})
        pending = timers.get(bridge["id"])
        if pending is not None:
            pending.cancel()
        timer = threading.Timer(delay, lambda: refresh(hub, bridge))
        timer.daemon = True
        timers[bridge["id"]] = timer
    timer.start()


# --- discovery ---------------------------------------------------------------


def discover(hub: dict[str, Any], source: str = "hue", deep: bool = False) -> list[dict[str, Any]]:
    known = {bridge["id"] for bridge in bridges(hub)}
    found = (
        shelly.discover(deep=deep)
        if source == shelly.SOURCE
        else [{**item, "source": "hue"} for item in discovery.discover_hue(deep=deep)]
    )
    return [{**item, "paired": item["id"] in known} for item in found]


def scan_network(hub: dict[str, Any]) -> dict[str, Any]:
    with hub["lock"]:
        if hub["network"]["scanning"]:
            return hub["network"]
        hub["network"] = {**hub["network"], "scanning": True}
    broadcast(hub)

    def run() -> None:
        try:
            hosts = discovery.scan_network()
        except Exception:
            hosts = []
        with hub["lock"]:
            hub["network"] = {"hosts": hosts, "scanned_at": time.time(), "scanning": False}
        broadcast(hub)

    threading.Thread(target=run, daemon=True).start()
    return hub["network"]
