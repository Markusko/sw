"""Finding things on the LAN with nothing but the standard library.

Three independent probes, run together and merged:

  * mDNS   -- multicast DNS-SD, how modern Hue bridges and most IoT kit announce
  * SSDP   -- UPnP search, how older bridges and many media/plug devices answer
  * cloud  -- https://discovery.meethue.com, Philips' own "which bridge is on
              your network" endpoint (needs internet, used only as a fallback)

Everything is best effort: a probe that fails contributes nothing and never
raises.  Results are plain dicts so the rest of the app stays functional.
"""

from __future__ import annotations

import re
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from . import net

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900

# Service types worth asking about by name; the enumeration query finds the rest.
KNOWN_SERVICES = {
    "_hue._tcp.local": "Philips Hue bridge",
    "_hap._tcp.local": "HomeKit accessory",
    "_matter._tcp.local": "Matter device",
    "_matterc._udp.local": "Matter commissioner",
    "_esphomelib._tcp.local": "ESPHome node",
    "_shelly._tcp.local": "Shelly device",
    "_tasmota._tcp.local": "Tasmota device",
    "_googlecast._tcp.local": "Google Cast",
    "_airplay._tcp.local": "AirPlay",
    "_spotify-connect._tcp.local": "Spotify Connect",
    "_printer._tcp.local": "Printer",
    "_ipp._tcp.local": "Printer",
    "_miio._udp.local": "Xiaomi device",
    "_dyson_mqtt._tcp.local": "Dyson device",
    "_wled._tcp.local": "WLED controller",
    "_home-assistant._tcp.local": "Home Assistant",
    "_zwave._tcp.local": "Z-Wave controller",
    "_deconz._tcp.local": "deCONZ gateway",
    "_workstation._tcp.local": "Computer",
}


# --- DNS wire format ---------------------------------------------------------


def encode_name(name: str) -> bytes:
    return b"".join(
        bytes([len(label)]) + label.encode() for label in name.split(".") if label
    ) + b"\x00"


def build_query(names: Iterable[str], qtype: int = 12) -> bytes:
    questions = list(names)
    header = struct.pack(">HHHHHH", 0, 0, len(questions), 0, 0, 0)
    body = b"".join(encode_name(name) + struct.pack(">HH", qtype, 1) for name in questions)
    return header + body


def _read_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    labels: list[str] = []
    while offset < len(data) and depth < 20:
        length = data[offset]
        if length == 0:
            return ".".join(labels), offset + 1
        if length & 0xC0 == 0xC0:  # compression pointer
            pointer = struct.unpack(">H", data[offset : offset + 2])[0] & 0x3FFF
            suffix, _ = _read_name(data, pointer, depth + 1)
            labels.append(suffix)
            return ".".join(labels), offset + 2
        labels.append(data[offset + 1 : offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    return ".".join(labels), offset


def parse_dns(data: bytes) -> list[dict[str, Any]]:
    """Return every record in a response as {name, type, value}."""
    try:
        _, _, questions, answers, authority, additional = struct.unpack(">HHHHHH", data[:12])
    except struct.error:
        return []
    offset = 12
    for _ in range(questions):
        _, offset = _read_name(data, offset)
        offset += 4
    records: list[dict[str, Any]] = []
    for _ in range(answers + authority + additional):
        if offset + 10 > len(data):
            break
        name, offset = _read_name(data, offset)
        rtype, _rclass, _ttl, length = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        payload = data[offset : offset + length]
        value: Any = None
        if rtype == 1 and length == 4:  # A
            value = socket.inet_ntoa(payload)
        elif rtype in (12, 5):  # PTR / CNAME
            value, _ = _read_name(data, offset)
        elif rtype == 33 and length > 6:  # SRV
            _priority, _weight, port = struct.unpack(">HHH", payload[:6])
            target, _ = _read_name(data, offset + 6)
            value = {"port": port, "target": target}
        elif rtype == 16:  # TXT
            value = _parse_txt(payload)
        offset += length
        if value is not None:
            records.append({"name": name, "type": rtype, "value": value})
    return records


def _parse_txt(payload: bytes) -> dict[str, str]:
    entries: dict[str, str] = {}
    index = 0
    while index < len(payload):
        length = payload[index]
        chunk = payload[index + 1 : index + 1 + length].decode("utf-8", "replace")
        key, _, value = chunk.partition("=")
        if key:
            entries[key] = value
        index += 1 + length
    return entries


# --- mDNS --------------------------------------------------------------------


def mdns_query(names: Iterable[str], timeout: float = 2.5) -> list[dict[str, Any]]:
    """Multicast a batch of PTR questions and collect every record we hear."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.settimeout(0.4)
    records: list[dict[str, Any]] = []
    try:
        sock.bind(("", 0))
        sock.sendto(build_query(names), (MDNS_GROUP, MDNS_PORT))
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while not deadline.is_set():
                try:
                    data, _sender = sock.recvfrom(9000)
                except socket.timeout:
                    continue
                except OSError:
                    break
                records.extend(parse_dns(data))
        finally:
            timer.cancel()
    except OSError:
        return records
    finally:
        sock.close()
    return records


def _index_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    addresses = {r["name"]: r["value"] for r in records if r["type"] == 1}
    services = {r["name"]: r["value"] for r in records if r["type"] == 33}
    texts = {r["name"]: r["value"] for r in records if r["type"] == 16}
    pointers: dict[str, list[str]] = {}
    for record in records:
        if record["type"] == 12:
            pointers.setdefault(record["name"], []).append(record["value"])
    return {"a": addresses, "srv": services, "txt": texts, "ptr": pointers}


def mdns_scan(timeout: float = 3.0) -> list[dict[str, Any]]:
    """Enumerate DNS-SD services and resolve them to hosts."""
    enumeration = mdns_query(["_services._dns-sd._udp.local", *KNOWN_SERVICES], timeout=timeout)
    index = _index_records(enumeration)
    discovered = {
        name
        for names in index["ptr"].get("_services._dns-sd._udp.local", [])
        for name in [names]
        if name.endswith(".local")
    }
    extra = sorted(discovered - set(KNOWN_SERVICES))[:25]
    records = enumeration + (mdns_query(extra, timeout=timeout) if extra else [])
    index = _index_records(records)

    hosts: dict[str, dict[str, Any]] = {}
    for service_type, instances in index["ptr"].items():
        if service_type.startswith("_services."):
            continue
        for instance in instances:
            service = index["srv"].get(instance)
            target = service["target"] if service else None
            address = index["a"].get(target or "", None)
            if not address:
                continue
            entry = hosts.setdefault(
                address,
                {"ip": address, "hostname": (target or "").removesuffix(".local"), "services": [], "labels": []},
            )
            label = KNOWN_SERVICES.get(service_type, service_type.rsplit("._", 1)[0].lstrip("_"))
            pretty = instance.removesuffix("." + service_type)
            if label not in entry["labels"]:
                entry["labels"].append(label)
            entry["services"].append(
                {"type": service_type, "name": pretty, "port": service["port"] if service else None,
                 "txt": index["txt"].get(instance, {})}
            )
    return sorted(hosts.values(), key=lambda host: tuple(int(part) for part in host["ip"].split(".")))


def find_hue_mdns(timeout: float = 2.5) -> list[str]:
    records = mdns_query(["_hue._tcp.local"], timeout=timeout)
    index = _index_records(records)
    targets = {
        index["srv"][instance]["target"]
        for instance in index["ptr"].get("_hue._tcp.local", [])
        if instance in index["srv"]
    }
    return sorted({index["a"][target] for target in targets if target in index["a"]})


# --- SSDP --------------------------------------------------------------------

SSDP_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: ssdp:all\r\n\r\n"
).encode()


def ssdp_scan(timeout: float = 3.0) -> list[dict[str, Any]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.4)
    replies: dict[str, dict[str, Any]] = {}
    try:
        sock.sendto(SSDP_SEARCH, (SSDP_GROUP, SSDP_PORT))
    except OSError:
        sock.close()
        return []
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        while not deadline.is_set():
            try:
                data, sender = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            headers = _parse_headers(data.decode("utf-8", "replace"))
            entry = replies.setdefault(sender[0], {"ip": sender[0], "headers": {}, "targets": []})
            entry["headers"].update(headers)
            target = headers.get("st") or headers.get("nt")
            if target and target not in entry["targets"]:
                entry["targets"].append(target)
    finally:
        timer.cancel()
        sock.close()
    return list(replies.values())


def _parse_headers(text: str) -> dict[str, str]:
    headers = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return headers


def find_hue_ssdp(timeout: float = 3.0) -> list[str]:
    return sorted(
        {
            reply["ip"]
            for reply in ssdp_scan(timeout)
            if "ipbridge" in reply["headers"].get("server", "").lower()
            or "hue-bridgeid" in reply["headers"]
        }
    )


# --- Hue-specific probes -----------------------------------------------------


def find_hue_cloud() -> list[str]:
    try:
        found = net.get_json("https://discovery.meethue.com/", timeout=5.0)
    except net.HttpError:
        return []
    if not isinstance(found, list):
        return []
    return [item["internalipaddress"] for item in found if isinstance(item, dict) and item.get("internalipaddress")]


def probe_bridge(ip: str, timeout: float = 2.5) -> dict[str, Any] | None:
    """Ask an address whether it is a Hue bridge; unauthenticated endpoint."""
    for url in (f"https://{ip}/api/0/config", f"http://{ip}/api/config"):
        try:
            config = net.get_json(url, timeout=timeout, insecure=True)
        except net.HttpError:
            continue
        if isinstance(config, dict) and config.get("bridgeid"):
            return {
                "id": str(config["bridgeid"]).lower(),
                "ip": ip,
                "name": config.get("name") or "Hue Bridge",
                "model": config.get("modelid", ""),
                "api_version": config.get("apiversion", ""),
                "software": config.get("swversion", ""),
                "mac": config.get("mac", ""),
                "supports_v2": _supports_clip_v2(config),
            }
    return None


def _supports_clip_v2(config: dict[str, Any]) -> bool:
    """CLIP v2 arrived with API 1.46 on the square (BSB002) bridge."""
    if str(config.get("modelid", "")).upper() == "BSB001":
        return False
    parts = re.findall(r"\d+", str(config.get("apiversion", "")))[:3]
    version = tuple(int(part) for part in parts) + (0, 0, 0)
    return version[:2] >= (1, 46)


def subnet_of(ip: str) -> str:
    return ip.rsplit(".", 1)[0]


def scan_subnet_for_bridges(ip: str | None = None, timeout: float = 1.2) -> list[dict[str, Any]]:
    """Last resort: knock on every host in our /24.  Slow-ish but thorough."""
    base = subnet_of(ip or net.local_ip())
    candidates = [f"{base}.{host}" for host in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as pool:
        found = pool.map(lambda address: probe_bridge(address, timeout=timeout), candidates)
    return [bridge for bridge in found if bridge]


def discover_hue(deep: bool = False) -> list[dict[str, Any]]:
    """Run the fast probes in parallel, then verify each candidate address."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(find_hue_mdns), pool.submit(find_hue_ssdp), pool.submit(find_hue_cloud)]
        addresses = [address for job in jobs for address in _safely(job.result)]
    bridges = _verify(dict.fromkeys(addresses))
    if not bridges and deep:
        bridges = scan_subnet_for_bridges()
    return bridges


def _verify(addresses: Iterable[str]) -> list[dict[str, Any]]:
    candidates = list(addresses)
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(candidates))) as pool:
        results = pool.map(probe_bridge, candidates)
    unique: dict[str, dict[str, Any]] = {}
    for bridge in results:
        if bridge:
            unique.setdefault(bridge["id"], bridge)
    return list(unique.values())


def _safely(call) -> list[str]:
    try:
        return call() or []
    except Exception:  # a failing probe must never sink discovery
        return []


def scan_network(timeout: float = 3.0) -> list[dict[str, Any]]:
    """Everything we can see on the LAN, Hue or not, for the Network view."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        mdns_job = pool.submit(mdns_scan, timeout)
        ssdp_job = pool.submit(ssdp_scan, timeout)
        hosts = {host["ip"]: host for host in _safely_any(mdns_job.result, [])}
        for reply in _safely_any(ssdp_job.result, []):
            headers = reply["headers"]
            entry = hosts.setdefault(
                reply["ip"], {"ip": reply["ip"], "hostname": "", "services": [], "labels": []}
            )
            server = headers.get("server", "")
            label = "Hue bridge" if "ipbridge" in server.lower() else "UPnP device"
            if label not in entry["labels"]:
                entry["labels"].append(label)
            entry.setdefault("upnp", {"server": server, "targets": reply["targets"][:6]})
    return sorted(hosts.values(), key=lambda host: tuple(int(part) for part in host["ip"].split(".")))


def _safely_any(call, fallback):
    try:
        return call() or fallback
    except Exception:
        return fallback
