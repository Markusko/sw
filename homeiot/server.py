"""HTTP layer: a small JSON API, static files, and a server-sent event feed.

Standard library only -- `python3 -m homeiot` is the whole install procedure.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import camera, demo, hue, hub as hub_module, integrations, model, net, shelly, store

WEB_ROOT = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

Handler = Callable[[dict[str, Any], re.Match, dict[str, Any], dict[str, Any]], Any]

# A browser closing a keep-alive connection or an event stream raises these,
# and on Windows it happens constantly (WinError 10053).  It is not an error:
# nothing failed, the other end simply went away.
DISCONNECTS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], DISCONNECTS):
            super().handle_error(request, client_address)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --- handlers ----------------------------------------------------------------


def get_state(hub: dict[str, Any], _match, _body, _query) -> Any:
    return hub_module.snapshot(hub)


def post_refresh(hub: dict[str, Any], _match, _body, _query) -> Any:
    hub_module.refresh_all(hub)
    return {"ok": True, "revision": hub["revision"]}


def get_sources(_hub: dict[str, Any], _match, _body, _query) -> Any:
    return {"sources": integrations.sources()}


def post_discover(hub: dict[str, Any], _match, body, _query) -> Any:
    source = str(body.get("source", "hue"))
    found = hub_module.discover(
        hub, source=source, deep=bool(body.get("deep")), key=str(body.get("key", ""))
    )
    return {"bridges": found}


def post_adopt(hub: dict[str, Any], _match, body, _query) -> Any:
    """Take on a device from any integration that does not need pairing."""
    source = str(body.get("source", "")).strip()
    ip = str(body.get("ip", "")).strip()
    secret = str(body.get("key") or body.get("password") or "").strip()
    module = integrations.BY_SOURCE.get(source)
    if module is None or source == "hue":
        raise ApiError("that is not something this can adopt directly")
    if not ip:
        raise ApiError("an address is required")

    try:
        found = module.probe(ip, key=secret) if source == "meross" else module.probe(ip)
    except Exception as error:
        raise ApiError(str(error), 502) from error
    if not found:
        raise ApiError(f"no {integrations.ACCESS[source]['label']} device answered at {ip}", 404)

    if source == "meross":
        if found.get("needs_key"):
            raise ApiError("that device rejected the key — check it in your Meross account", 401)
        device = module.make_device(found, key=secret)
    elif source == "swisscom":
        device = module.make_device(found, password=secret)
    elif found.get("protected") and source == "shelly":
        raise ApiError(f"{found['name']} has a password set, which is not supported yet", 501)
    else:
        device = module.make_device(found)

    hub_module.add_bridge(hub, device)
    return {"ok": True, "device": {"id": device["id"], "name": device["name"], "ip": device["ip"],
                                   "source": source}}


def post_shelly(hub: dict[str, Any], _match, body, _query) -> Any:
    """Shelly devices need no pairing -- an address is the whole handshake."""
    ip = str(body.get("ip", "")).strip()
    if not ip:
        raise ApiError("an address is required")
    found = shelly.probe(ip)
    if not found:
        raise ApiError(f"no Shelly device answered at {ip}", 404)
    if found["protected"]:
        raise ApiError(f"{found['name']} has a password set, which is not supported yet", 501)
    device = shelly.make_device(found)
    hub_module.add_bridge(hub, device)
    return {"ok": True, "device": {key: device[key] for key in ("id", "name", "ip", "model", "generation")}}


def post_pair(hub: dict[str, Any], _match, body, _query) -> Any:
    from . import discovery

    ip = str(body.get("ip", "")).strip()
    if not ip:
        raise ApiError("an address is required")
    found = discovery.probe_bridge(ip)
    if not found:
        raise ApiError(f"no Hue bridge answered at {ip}", 404)
    try:
        credentials = hue.pair(ip)
    except hue.LinkButtonError as error:
        raise ApiError(str(error) or "press the link button, then try again", 428) from error
    except hue.BridgeError as error:
        raise ApiError(str(error), 502) from error
    bridge = hue.make_bridge(found, credentials)
    hub_module.add_bridge(hub, bridge)
    return {"ok": True, "bridge": {key: bridge[key] for key in ("id", "name", "ip", "api", "model")}}


def post_demo(hub: dict[str, Any], _match, _body, _query) -> Any:
    hub_module.add_bridge(hub, dict(demo.BRIDGE))
    return {"ok": True}


def delete_bridge(hub: dict[str, Any], match, _body, _query) -> Any:
    hub_module.remove_bridge(hub, match.group("id"))
    return {"ok": True}


def put_target_state(hub: dict[str, Any], match, body, _query) -> Any:
    try:
        return hub_module.command(hub, match.group("id"), body)
    except LookupError as error:
        raise ApiError(str(error), 404) from error


def post_scene(hub: dict[str, Any], match, body, _query) -> Any:
    try:
        return hub_module.recall_scene(hub, match.group("id"), body)
    except LookupError as error:
        raise ApiError(str(error), 404) from error
    except hue.BridgeError as error:
        raise ApiError(str(error), 502) from error


def post_identify(hub: dict[str, Any], match, _body, _query) -> Any:
    try:
        return hub_module.identify(hub, match.group("id"))
    except LookupError as error:
        raise ApiError(str(error), 404) from error
    except hue.BridgeError as error:
        raise ApiError(str(error), 502) from error


def put_name(hub: dict[str, Any], match, body, _query) -> Any:
    try:
        return hub_module.rename(hub, match.group("id"), str(body.get("name", "")))
    except LookupError as error:
        raise ApiError(str(error), 404) from error
    except hue.BridgeError as error:
        raise ApiError(str(error), 502) from error


def post_collection(hub: dict[str, Any], _match, body, _query) -> Any:
    collection = store.normalise_collection(body)
    hub_module.mutate_config(hub, lambda config: store.put_collection(config, collection))
    return {"ok": True, "collection": collection}


def put_collection(hub: dict[str, Any], match, body, _query) -> Any:
    collection_id = match.group("id")
    existing = next((c for c in hub["config"]["collections"] if c["id"] == collection_id), None)
    if not existing:
        raise ApiError("no such collection", 404)
    updated = store.normalise_collection(body, existing)
    hub_module.mutate_config(hub, lambda config: store.put_collection(config, updated))
    return {"ok": True, "collection": updated}


def delete_collection(hub: dict[str, Any], match, _body, _query) -> Any:
    hub_module.mutate_config(hub, lambda config: store.drop_collection(config, match.group("id")))
    return {"ok": True}


def put_ui(hub: dict[str, Any], _match, body, _query) -> Any:
    allowed = {key: body[key] for key in ("theme",) if key in body}
    hub_module.mutate_config(hub, lambda config: {**config, "ui": {**config.get("ui", {}), **allowed}})
    return {"ok": True, "ui": hub["config"]["ui"]}


def post_scan(hub: dict[str, Any], _match, _body, _query) -> Any:
    return hub_module.scan_network(hub)


def post_readout(hub: dict[str, Any], _match, body, _query) -> Any:
    try:
        readout = store.normalise_readout(body)
    except ValueError as error:
        raise ApiError(str(error)) from error
    hub_module.mutate_config(hub, lambda config: store.put_readout(config, readout))
    return {"ok": True, "readout": readout}


def put_readout(hub: dict[str, Any], match, body, _query) -> Any:
    existing = next((r for r in hub["config"].get("readouts", []) if r["id"] == match.group("id")), None)
    if not existing:
        raise ApiError("no such value", 404)
    try:
        readout = store.normalise_readout(body, existing)
    except ValueError as error:
        raise ApiError(str(error)) from error
    hub_module.mutate_config(hub, lambda config: store.put_readout(config, readout))
    return {"ok": True, "readout": readout}


def delete_readout(hub: dict[str, Any], match, _body, _query) -> Any:
    hub_module.mutate_config(hub, lambda config: store.drop_readout(config, match.group("id")))
    return {"ok": True}


def post_camera(hub: dict[str, Any], _match, body, _query) -> Any:
    try:
        item = camera.normalise({**body, "id": store.new_id("cam-")})
    except ValueError as error:
        raise ApiError(str(error)) from error
    hub_module.mutate_config(hub, lambda config: store.put_camera(config, item))
    return {"ok": True, "camera": camera.describe(item)}


def put_camera(hub: dict[str, Any], match, body, _query) -> Any:
    existing = hub_module.find_camera(hub, match.group("id"))
    if not existing:
        raise ApiError("no such camera", 404)
    try:
        item = camera.normalise(body, existing)
    except ValueError as error:
        raise ApiError(str(error)) from error
    hub_module.mutate_config(hub, lambda config: store.put_camera(config, item))
    return {"ok": True, "camera": camera.describe(item)}


def delete_camera(hub: dict[str, Any], match, _body, _query) -> Any:
    hub_module.mutate_config(hub, lambda config: store.drop_camera(config, match.group("id")))
    return {"ok": True}


ROUTES: list[tuple[str, re.Pattern, Handler]] = [
    ("GET", re.compile(r"^/api/state$"), get_state),
    ("POST", re.compile(r"^/api/refresh$"), post_refresh),
    ("POST", re.compile(r"^/api/discover$"), post_discover),
    ("POST", re.compile(r"^/api/pair$"), post_pair),
    ("POST", re.compile(r"^/api/demo$"), post_demo),
    ("DELETE", re.compile(r"^/api/bridges/(?P<id>[^/]+)$"), delete_bridge),
    ("PUT", re.compile(r"^/api/targets/(?P<id>[^/]+)/state$"), put_target_state),
    ("PUT", re.compile(r"^/api/targets/(?P<id>[^/]+)/name$"), put_name),
    ("POST", re.compile(r"^/api/scenes/(?P<id>[^/]+)/recall$"), post_scene),
    ("POST", re.compile(r"^/api/devices/(?P<id>[^/]+)/identify$"), post_identify),
    ("POST", re.compile(r"^/api/collections$"), post_collection),
    ("PUT", re.compile(r"^/api/collections/(?P<id>[^/]+)$"), put_collection),
    ("DELETE", re.compile(r"^/api/collections/(?P<id>[^/]+)$"), delete_collection),
    ("PUT", re.compile(r"^/api/ui$"), put_ui),
    ("POST", re.compile(r"^/api/scan$"), post_scan),
    ("POST", re.compile(r"^/api/shelly$"), post_shelly),
    ("GET", re.compile(r"^/api/sources$"), get_sources),
    ("POST", re.compile(r"^/api/adopt$"), post_adopt),
    ("POST", re.compile(r"^/api/readouts$"), post_readout),
    ("PUT", re.compile(r"^/api/readouts/(?P<id>[^/]+)$"), put_readout),
    ("DELETE", re.compile(r"^/api/readouts/(?P<id>[^/]+)$"), delete_readout),
    ("POST", re.compile(r"^/api/cameras$"), post_camera),
    ("PUT", re.compile(r"^/api/cameras/(?P<id>[^/]+)$"), put_camera),
    ("DELETE", re.compile(r"^/api/cameras/(?P<id>[^/]+)$"), delete_camera),
]

CAMERA_FRAME = re.compile(r"^/api/cameras/(?P<id>[^/]+)/frame$")
CAMERA_STREAM = re.compile(r"^/api/cameras/(?P<id>[^/]+)/stream$")


# --- request handling --------------------------------------------------------


def make_handler(hub: dict[str, Any], token: str | None):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "homeiot"
        protocol_version = "HTTP/1.1"

        # -- plumbing
        def handle_one_request(self) -> None:
            # Raised while waiting for the next request on a kept-alive socket,
            # which is outside any handler's reach.
            try:
                super().handle_one_request()
            except DISCONNECTS:
                self.close_connection = True

        def log_message(self, fmt: str, *args: Any) -> None:
            if hub.get("verbose"):
                sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, default=str).encode()
            self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ApiError("malformed JSON body") from error
            return parsed if isinstance(parsed, dict) else {"value": parsed}

        def _authorised(self, query: dict[str, Any]) -> bool:
            if not token:
                return True
            supplied = (
                self.headers.get("X-Auth-Token")
                or (query.get("token") or [None])[0]
                or _cookie(self.headers.get("Cookie", ""), "homeiot_token")
            )
            return supplied == token

        def _same_origin(self) -> bool:
            """Block cross-site writes; a browser always sends Origin on those."""
            origin = self.headers.get("Origin")
            if not origin or self.command in ("GET", "HEAD"):
                return True
            return urlparse(origin).netloc == self.headers.get("Host")

        # -- verbs
        def do_GET(self) -> None:
            self._dispatch()

        def do_HEAD(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def _dispatch(self) -> None:
            parsed = urlparse(self.path)
            # ids carry colons, which the browser percent-encodes on the way in
            path = unquote(parsed.path).rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if not self._authorised(query):
                    return self._json(401, {"error": "a token is required"})
                if not self._same_origin():
                    return self._json(403, {"error": "cross-origin writes are refused"})
                if path == "/api/events":
                    return self._stream()
                if self.command in ("GET", "HEAD"):
                    still = CAMERA_FRAME.match(path)
                    if still:
                        return self._camera_frame(still.group("id"))
                    live = CAMERA_STREAM.match(path)
                    if live:
                        return self._camera_stream(live.group("id"))
                candidates = [(method, pattern.match(path), handler) for method, pattern, handler in ROUTES]
                candidates = [(method, match, handler) for method, match, handler in candidates if match]
                for method, match, handler in candidates:
                    if method == self.command:
                        body = self._read_body() if self.command in ("POST", "PUT") else {}
                        return self._json(200, handler(hub, match, body, query))
                if candidates:
                    allowed = ", ".join(sorted({method for method, _match, _handler in candidates}))
                    return self._json(405, {"error": f"{self.command} not allowed here; try {allowed}"})
                if path.startswith("/api/"):
                    return self._json(404, {"error": "no such endpoint"})
                return self._static(path)
            except ApiError as error:
                self._json(error.status, {"error": str(error)})
            except DISCONNECTS:
                pass
            except Exception as error:  # never take the server down for one request
                self._json(500, {"error": f"{type(error).__name__}: {error}"})

        def _static(self, path: str) -> None:
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (WEB_ROOT / relative).resolve()
            if not str(target).startswith(str(WEB_ROOT)) or not target.is_file():
                target = WEB_ROOT / "index.html"
            body = target.read_bytes()
            content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            cache = "no-cache" if target.suffix in (".html", ".js", ".css") else "max-age=86400"
            self._send(200, body, content_type, {"Cache-Control": cache})

        def _stream(self) -> None:
            channel = hub_module.subscribe(hub)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self._emit("state", hub_module.snapshot(hub))
                while not hub["stopping"].is_set():
                    try:
                        payload = channel.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self._emit("state", payload)
            except (OSError, *DISCONNECTS):
                pass
            finally:
                hub_module.unsubscribe(hub, channel)

        # -- cameras
        def _camera_frame(self, camera_id: str) -> None:
            item = hub_module.find_camera(hub, camera_id)
            if not item:
                return self._json(404, {"error": "no such camera"})
            try:
                content_type, payload = camera.frame(item)
            except camera.CameraError as error:
                return self._json(503, {"error": camera.redact(str(error))})
            self._send(200, payload, content_type, {"Cache-Control": "no-store"})

        def _camera_stream(self, camera_id: str) -> None:
            """A live view as multipart JPEG, which every browser can show in an <img>."""
            item = hub_module.find_camera(hub, camera_id)
            if not item:
                return self._json(404, {"error": "no such camera"})
            if camera.mode(item) in ("needs_ffmpeg", "unconfigured"):
                return self._json(503, {"error": camera.redact(_camera_reason(item))})

            boundary = "homeiotframe"
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for content_type, payload in camera.frames(item, lambda: hub["stopping"].is_set()):
                    head = f"--{boundary}\r\nContent-Type: {content_type}\r\n"
                    head += f"Content-Length: {len(payload)}\r\n\r\n"
                    self.wfile.write(head.encode())
                    self.wfile.write(payload)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (*DISCONNECTS, camera.CameraError, OSError):
                pass  # the viewer closed the tab, or the camera went away

        def _emit(self, event: str, payload: Any) -> None:
            data = json.dumps(payload, default=str)
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
            self.wfile.flush()

    return DashboardHandler


def _camera_reason(item: dict[str, Any]) -> str:
    if camera.mode(item) == "needs_ffmpeg":
        return "ffmpeg is not installed, so this RTSP stream cannot be shown"
    return "this camera has neither an RTSP nor a snapshot address"


def _cookie(header: str, name: str) -> str | None:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


# --- entry point -------------------------------------------------------------


def build_hub(demo_mode: bool = False) -> dict[str, Any]:
    config = store.load()
    hub = hub_module.create(config)
    if demo_mode and not any(bridge.get("demo") for bridge in config.get("bridges", [])):
        config = store.put_bridge(config, dict(demo.BRIDGE))
        config = store.put_bridge(config, dict(demo.SHELLY))
        for fake in demo.FAKES:
            config = store.put_bridge(config, dict(fake))
        config = store.put_camera(config, dict(demo.CAMERA))
        for readout in demo.READOUTS:
            config = store.put_readout(config, dict(readout))
        hub["config"] = config
    return hub


def serve(host: str = "0.0.0.0", port: int = 8712, demo_mode: bool = False,
          token: str | None = None, verbose: bool = False) -> None:
    hub = build_hub(demo_mode)
    hub["verbose"] = verbose
    hub_module.start(hub)

    server = DashboardServer((host, port), make_handler(hub, token))
    address = net.local_ip() if host in ("0.0.0.0", "") else host
    print(f"homeiot -> http://{address}:{port}  (local: http://127.0.0.1:{port})")
    if demo_mode:
        print("demo bridge active -- simulated devices, no hardware needed")
    if token:
        print(f"access token required: append ?token={token} on first visit")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        hub_module.stop(hub)
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeiot", description="Home IoT dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    parser.add_argument("--port", type=int, default=8712)
    parser.add_argument("--demo", action="store_true", help="add a simulated bridge")
    parser.add_argument("--token", default=None, help="require this token in ?token= or X-Auth-Token")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--discover", action="store_true", help="print discovered bridges and exit")
    parser.add_argument("--scan", action="store_true", help="print every IoT host found on the LAN and exit")
    parser.add_argument("--probe-box", metavar="ADDRESS", nargs="?", const="192.168.1.1",
                        help="ask a Swisscom Internet-Box what its local API answers, and exit")
    arguments = parser.parse_args(argv)

    if arguments.discover:
        from . import discovery

        for bridge in discovery.discover_hue(deep=True):
            print(f"{bridge['ip']:<16} {bridge['name']}  ({bridge['model']}, API {bridge['api_version']})")
        return 0
    if arguments.scan:
        from . import discovery

        for host in discovery.scan_network():
            labels = ", ".join(host.get("labels", [])) or "unknown"
            print(f"{host['ip']:<16} {host.get('hostname', ''):<28} {labels}")
        return 0

    if arguments.probe_box:
        from . import swisscom

        address = arguments.probe_box
        if address is True or address == "auto":
            address = swisscom.gateway_candidates()[0]
        print(f"reading {address} — this follows the box's own web app, so give it a moment\n")
        print(swisscom.report(swisscom.survey(address)))
        return 0

    serve(arguments.host, arguments.port, arguments.demo, arguments.token, arguments.verbose)
    return 0
