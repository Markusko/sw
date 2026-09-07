"""Configuration on disk: one JSON document, read and written atomically.

The file holds bridge credentials, so it is created with 0600 permissions.
Every function here takes and returns plain dicts -- nothing mutates in place.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "bridges": [],
    "collections": [],
    "ui": {"theme": "auto"},
}

_LOCK = threading.Lock()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def load(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        stored = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return {**json.loads(json.dumps(DEFAULT_CONFIG)), **stored}


def save(config: dict[str, Any], path: Path = CONFIG_PATH) -> dict[str, Any]:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".config-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(config, file, indent=2, sort_keys=True)
                file.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return config


def update(mutator, path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Read, transform with a pure function, write back."""
    return save(mutator(load(path)), path)


# --- bridges -----------------------------------------------------------------


def put_bridge(config: dict[str, Any], bridge: dict[str, Any]) -> dict[str, Any]:
    others = [item for item in config["bridges"] if item.get("id") != bridge.get("id")]
    return {**config, "bridges": [*others, bridge]}


def drop_bridge(config: dict[str, Any], bridge_id: str) -> dict[str, Any]:
    return {**config, "bridges": [b for b in config["bridges"] if b.get("id") != bridge_id]}


# --- collections -------------------------------------------------------------
# A collection is a free-form set of devices that cuts across rooms: "Evening",
# "Security", "Desk".  Members are normalised device ids.


def normalise_collection(raw: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = existing or {"id": new_id("col-"), "icon": "circle", "members": [], "order": 0}
    members = raw.get("members", base["members"])
    return {
        **base,
        "name": str(raw.get("name", base.get("name", "Untitled"))).strip()[:60] or "Untitled",
        "icon": str(raw.get("icon", base["icon"]))[:32],
        "order": int(raw.get("order", base["order"])),
        "members": [str(member) for member in dict.fromkeys(members)][:200],
    }


def put_collection(config: dict[str, Any], collection: dict[str, Any]) -> dict[str, Any]:
    others = [item for item in config["collections"] if item["id"] != collection["id"]]
    ordered = sorted([*others, collection], key=lambda item: (item.get("order", 0), item["name"].lower()))
    return {**config, "collections": ordered}


def drop_collection(config: dict[str, Any], collection_id: str) -> dict[str, Any]:
    kept = [item for item in config["collections"] if item["id"] != collection_id]
    return {**config, "collections": kept}


def forget_devices(config: dict[str, Any], device_ids: set[str]) -> dict[str, Any]:
    """Drop stale members (e.g. after a bridge is removed)."""
    collections = [
        {**item, "members": [m for m in item["members"] if m not in device_ids]}
        for item in config["collections"]
    ]
    return {**config, "collections": collections}
