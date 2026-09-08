"""The new integrations, tested against fake devices that speak the real wire.

None of this hardware is here, so each protocol is stood up as a small HTTP
server that answers the way the device does -- the same SOAP envelopes, the
same signed JSON -- and the client is driven against it over a real socket.
That catches what a mocked function call would not: the headers, the encoding,
the signature, and the parsing of a genuine response.

    python3 -m unittest homeiot.tests.test_integrations
"""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from homeiot import cast, homeconnect, integrations, meross, model, sonos, swisscom

# --- a fake Sonos ------------------------------------------------------------

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
  <deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>
  <friendlyName>192.168.1.31 - Sonos One</friendlyName>
  <manufacturer>Sonos, Inc.</manufacturer>
  <modelName>Sonos One</modelName>
  <displayName>One</displayName>
  <roomName>Kitchen</roomName>
  <softwareVersion>78.1-53190</softwareVersion>
  <UDN>uuid:RINCON_347E5C0D442401400</UDN>
</device></root>"""

TRACK_METADATA = (
    '&lt;DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
    "&lt;item&gt;&lt;dc:title&gt;Teardrop&lt;/dc:title&gt;"
    "&lt;dc:creator&gt;Massive Attack&lt;/dc:creator&gt;"
    "&lt;upnp:album&gt;Mezzanine&lt;/upnp:album&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;"
)

SOAP_REPLIES = {
    "GetTransportInfo": "<CurrentTransportState>PLAYING</CurrentTransportState>"
                        "<CurrentTransportStatus>OK</CurrentTransportStatus>",
    "GetPositionInfo": f"<Track>1</Track><TrackDuration>0:05:30</TrackDuration>"
                       f"<TrackMetaData>{TRACK_METADATA}</TrackMetaData><RelTime>0:01:12</RelTime>",
    "GetVolume": "<CurrentVolume>27</CurrentVolume>",
    "GetMute": "<CurrentMute>0</CurrentMute>",
}


def _soap(action: str, inner: str) -> bytes:
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f"{inner}</u:{action}Response></s:Body></s:Envelope>"
    ).encode()


class SonosHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/xml/device_description.xml":
            return self.send_error(404)
        body = DESCRIPTION.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        action = (self.headers.get("SOAPACTION") or "").strip('"').rpartition("#")[2]
        SonosHandler.calls.append({"action": action, "body": body, "path": self.path})

        if action == "Play" and "Speed>9<" in body:  # a way to force a fault
            return self._fault()
        reply = _soap(action, SOAP_REPLIES.get(action, ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def _fault(self):
        body = (
            '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            "<s:Body><s:Fault><detail><UPnPError><errorCode>701</errorCode>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        ).encode()
        self.send_response(500)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SonosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", sonos.PORT), SonosHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SonosHandler.calls.clear()
        self.speaker = sonos.probe("127.0.0.1")

    def test_a_speaker_describes_itself(self):
        self.assertEqual(self.speaker["id"], "rincon_347e5c0d442401400")
        self.assertEqual(self.speaker["name"], "Kitchen")  # the room, not the IP-prefixed name
        self.assertEqual(self.speaker["model"], "One")
        self.assertEqual(self.speaker["source"], "sonos")

    def test_anything_that_is_not_a_speaker_is_not_adopted(self):
        self.assertIsNone(sonos.probe("127.0.0.1:1"))

    def test_what_is_playing_and_how_loud(self):
        built = sonos.home(self.speaker, sonos.snapshot(self.speaker))["devices"][0]
        self.assertEqual(built["kind"], "media")
        self.assertEqual(built["state"]["volume"], 27)
        self.assertTrue(built["state"]["playing"])
        self.assertFalse(built["state"]["muted"])
        self.assertEqual(built["state"]["track"]["title"], "Teardrop")
        self.assertEqual(built["state"]["track"]["artist"], "Massive Attack")
        self.assertEqual(built["readings"][0]["display"], "Massive Attack — Teardrop")

    def test_volume_is_sent_as_the_speaker_expects(self):
        sonos.send(self.speaker, "player", "0", {"volume": 35})
        call = SonosHandler.calls[-1]
        self.assertEqual(call["action"], "SetVolume")
        self.assertIn("<DesiredVolume>35</DesiredVolume>", call["body"])
        self.assertIn("<Channel>Master</Channel>", call["body"])
        self.assertEqual(call["path"], sonos.RENDERING[1])

    def test_volume_is_clamped_to_what_the_protocol_allows(self):
        sonos.send(self.speaker, "player", "0", {"volume": 250})
        self.assertIn("<DesiredVolume>100</DesiredVolume>", SonosHandler.calls[-1]["body"])

    def test_transport_commands(self):
        sonos.send(self.speaker, "player", "0", {"play": True})
        self.assertEqual(SonosHandler.calls[-1]["action"], "Play")
        sonos.send(self.speaker, "player", "0", {"next": True})
        self.assertEqual(SonosHandler.calls[-1]["action"], "Next")
        # A switch turned off on a media card means pause.
        sonos.send(self.speaker, "player", "0", {"on": {"on": False}})
        self.assertEqual(SonosHandler.calls[-1]["action"], "Pause")

    def test_a_refusal_is_reported_not_swallowed(self):
        with self.assertRaises(sonos.SonosError) as caught:
            sonos.call("127.0.0.1", sonos.AV_TRANSPORT, "Play", {"Speed": "9"})
        self.assertIn("701", str(caught.exception))

    def test_ids_route_back_to_the_speaker(self):
        home = sonos.home(self.speaker, sonos.snapshot(self.speaker))
        writes = model.write_targets(home, [], home["devices"][0]["id"])
        self.assertEqual(writes, [{"bridge": self.speaker["id"], "rtype": "player", "rid": "0"}])


# --- a fake Meross -----------------------------------------------------------

DEVICE_KEY = "a1b2c3d4e5"

THERMOSTAT_DIGEST = {
    "thermostat": {
        "mode": [
            {
                "channel": 0, "onoff": 1, "mode": 3, "state": 1,
                "currentTemp": 215, "targetTemp": 220, "heatTemp": 220, "coolTemp": 180,
                "ecoTemp": 160, "manualTemp": 225, "min": 50, "max": 350, "warning": 0,
            }
        ]
    }
}


class MerossHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length).decode())
        header = request["header"]
        MerossHandler.calls.append(request)

        expected = hashlib.md5(
            f"{header['messageId']}{DEVICE_KEY}{header['timestamp']}".encode()
        ).hexdigest()
        if header.get("sign") != expected:
            return self._reply({"header": {**header, "method": "ERROR"},
                                "payload": {"error": {"code": 5001, "detail": "sign error"}}})

        if header["namespace"] == meross.SYSTEM_ALL:
            return self._reply({
                "header": {**header, "method": "GETACK"},
                "payload": {"all": {
                    "system": {
                        "hardware": {"type": "mts200b", "uuid": "2012345678901234abcd",
                                     "macAddress": "48:e1:e9:00:11:22"},
                        "firmware": {"version": "6.2.5"},
                    },
                    "digest": THERMOSTAT_DIGEST,
                }},
            })
        return self._reply({"header": {**header, "method": "SETACK"}, "payload": {}})

    def _reply(self, document):
        body = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MerossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MerossHandler)
        cls.server.daemon_threads = True
        cls.address = f"127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MerossHandler.calls.clear()
        self.device = meross.make_device(meross.probe(self.address, key=DEVICE_KEY), key=DEVICE_KEY)

    def test_the_signature_is_what_the_device_expects(self):
        # setUp already proved it: a wrong signature is answered with an error.
        self.assertEqual(self.device["id"], "2012345678901234abcd")
        self.assertEqual(self.device["model"], "mts200b")
        self.assertFalse(self.device["protected"])

    def test_a_wrong_key_is_reported_as_a_wrong_key(self):
        found = meross.probe(self.address, key="not-the-key")
        self.assertTrue(found["needs_key"])
        with self.assertRaises(meross.KeyError_):
            meross.rpc(self.address, meross.SYSTEM_ALL, {}, key="not-the-key")

    def test_the_thermostat_reads_in_degrees(self):
        built = meross.home(self.device, meross.snapshot(self.device))["devices"][0]
        self.assertEqual(built["kind"], "thermostat")
        self.assertEqual(built["state"]["current"], 21.5)  # tenths on the wire
        self.assertEqual(built["state"]["target"], 22.0)
        self.assertEqual(built["state"]["target_range"], [5.0, 35.0])
        self.assertTrue(built["state"]["heating"])
        self.assertEqual(built["state"]["mode_label"], "Schedule")
        self.assertIn("target_temperature", built["capabilities"])
        readings = {reading["kind"]: reading["display"] for reading in built["readings"]}
        self.assertEqual(readings["temperature"], "21.5°C")
        self.assertEqual(readings["heating"], "Heating")

    def test_setting_a_temperature_switches_it_to_manual(self):
        meross.send(self.device, "thermostat", "0", {"target": 22.5})
        sent = MerossHandler.calls[-1]
        self.assertEqual(sent["header"]["method"], "SET")
        self.assertEqual(sent["header"]["namespace"], meross.THERMOSTAT)
        body = sent["payload"]["mode"][0]
        self.assertEqual(body["targetTemp"], 225)  # back to tenths
        self.assertEqual(body["mode"], meross.MANUAL)  # or the schedule would undo it
        self.assertEqual(body["channel"], 0)

    def test_turning_the_thermostat_off(self):
        meross.send(self.device, "thermostat", "0", {"on": {"on": False}})
        self.assertEqual(MerossHandler.calls[-1]["payload"]["mode"][0]["onoff"], 0)

    def test_a_device_without_its_key_refuses_to_pretend(self):
        with self.assertRaises(meross.MerossError):
            meross.snapshot({**self.device, "key": ""})


# --- the announcement-only integrations --------------------------------------


class AnnouncementTests(unittest.TestCase):
    """Cast, Home Connect: everything comes from the mDNS TXT record."""

    def test_a_shield_is_read_from_what_cast_announces(self):
        device = {"id": "shield-1", "ip": "10.0.0.5", "name": "Shield", "model": "SHIELD Android TV"}
        raw = {"txt": {"fn": "Living Room Shield", "md": "SHIELD Android TV", "st": "1",
                       "rs": "Netflix", "ve": "05"}}
        built = cast.home(device, raw)["devices"][0]
        self.assertEqual(built["kind"], "media")
        self.assertEqual(built["name"], "Living Room Shield")
        self.assertTrue(built["state"]["on"])
        self.assertEqual(built["readings"][0]["display"], "Netflix")
        self.assertFalse(built["controllable"])  # honest: no remote is implemented

    def test_an_idle_cast_device_says_idle(self):
        built = cast.home({"id": "c", "ip": "1.2.3.4"}, {"txt": {"fn": "Shield", "st": "0"}})["devices"][0]
        self.assertFalse(built["state"]["on"])
        self.assertEqual(built["readings"][0]["display"], "Idle")

    def test_a_siemens_dishwasher_is_identified(self):
        entry = {"instance": "SIEMENS-DISHWASHER-ABC._homeconnect._tcp.local", "ip": "10.0.0.6",
                 "txt": {"type": "Dishwasher", "brand": "siemens", "vib": "SN65ZX49CE",
                         "serialno": "123456", "id": "abc123"}}
        found = homeconnect._describe(entry)
        self.assertEqual(found["name"], "Siemens Dishwasher")
        self.assertEqual(found["model"], "SN65ZX49CE")
        self.assertTrue(found["protected"])  # the keys live in the account

        built = homeconnect.home(found, {"txt": entry["txt"]})["devices"][0]
        self.assertEqual(built["kind"], "appliance")
        self.assertFalse(built["controllable"])
        self.assertIn("Home Connect account", built["note"])

    def test_a_hob_is_named_as_a_hob(self):
        found = homeconnect._describe(
            {"instance": "x._homeconnect._tcp.local", "ip": "10.0.0.7",
             "txt": {"type": "CookTop", "brand": "siemens", "vib": "EX675LYV1E"}}
        )
        self.assertEqual(found["name"], "Siemens Hob")


# --- a fake Internet-Box -----------------------------------------------------
# Modelled on the real one: a Vite build that joins a base onto relative paths
# rather than writing "/api/..." out, a lazily loaded chunk holding half the
# endpoints, and a /ws that drops a POST but accepts a websocket handshake.
# Reading this box means reading the way it is written, not pattern-matching.

BOX_PAGE = """<!DOCTYPE html><html><head><title>Internet-Box</title>
<meta name="viewport" content="width=device-width">
<link rel="modulepreload" href="/assets/vendor-8b21c0.js"></head>
<body><div id="app"></div>
<script type="module" crossorigin src="/assets/index-Dl-CRwBR.js"></script>
<script src="https://example.invalid/tracker.js"></script>
</body></html>"""

BOX_APP_JS = """
const B="/cgi/json-req";const V="v1/";
async function q(s){return fetch(B,{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({service:s,method:"get"})})}
const info=()=>fetch("/api/"+V+"system/deviceinfo");
const login=()=>fetch("/api/v1/session",{method:"POST"});
const live=()=>new WebSocket("ws://"+location.host+"/ws");
const later=()=>import("./detail-3a1f77.js");
var u="/assets/logo.svg";var m="application/json";
"""

BOX_CHUNK_JS = """
const hosts=()=>fetch("/api/v1/network/hosts");
const legacy=()=>fetch('/data/status.json');
"""


class BoxHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._upgrade() if self.path == "/ws" else self._send(404, "no", "text/html")
        if self.path == "/":
            return self._send(200, BOX_PAGE, "text/html")
        if self.path.endswith("index-Dl-CRwBR.js"):
            return self._send(200, BOX_APP_JS, "application/javascript")
        if self.path.endswith("detail-3a1f77.js"):
            return self._send(200, BOX_CHUNK_JS, "application/javascript")
        if self.path.endswith("vendor-8b21c0.js"):
            return self._send(200, "/* vendor bundle, nothing of ours */", "application/javascript")
        if self.path == "/api/v1/system/deviceinfo":
            return self._send(200, json.dumps({"model": "PRV65AX", "firmware": "15.20.46"}),
                              "application/json")
        if self.path == "/api/v1/session":
            return self._send(401, json.dumps({"error": "unauthorised"}), "application/json")
        self._send(404, "<html><head><title>Not Found</title></head><body>404</body></html>", "text/html")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        sent = self.rfile.read(length)
        if self.path == "/ws":
            # The dialect is the content type: anything else gets dropped, which
            # is exactly how a live route can look like a dead one.
            if self.headers.get("Content-Type") != "application/x-sah-ws-4-call+json":
                self.close_connection = True
                return
            asked = json.loads(sent or b"{}").get("method", "")
            return self._send(200, json.dumps({"status": {"method": asked, "contextID": "abc123"}}),
                              "application/json")
        self._send(404, "<html>404</html>", "text/html")

    def _upgrade(self):
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Protocol", "sah-ws")
        self.end_headers()
        self.close_connection = True

    def _send(self, status, body, content_type):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class InternetBoxProbeTests(unittest.TestCase):
    """The prober has to find the API by reading the box's own web app."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BoxHandler)
        cls.server.daemon_threads = True
        cls.address = f"127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.found = swisscom.survey(cls.address, timeout=3.0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_the_box_is_recognised_by_its_own_page(self):
        self.assertEqual(self.found["title"], "Internet-Box")
        self.assertTrue(self.found["reachable"])
        described = swisscom.probe(self.address, timeout=3.0)
        self.assertEqual(described["name"], "Internet-Box")
        self.assertEqual(described["source"], "swisscom")

    def test_it_follows_the_scripts_the_page_loads(self):
        urls = [asset["url"] for asset in self.found["assets"]]
        self.assertTrue(any("index-Dl-CRwBR.js" in url for url in urls), urls)
        self.assertTrue(any("vendor-8b21c0.js" in url for url in urls), urls)
        # Somebody else's CDN is not this box's API.
        self.assertFalse(any("example.invalid" in url for url in urls), urls)

    def test_it_follows_the_chunks_those_scripts_load(self):
        """Half the endpoints live in a lazily imported chunk the page never names."""
        urls = [asset["url"] for asset in self.found["assets"]]
        self.assertTrue(any("detail-3a1f77.js" in url for url in urls), urls)
        self.assertIn("/api/v1/network/hosts", self.found["mentioned"])

    def test_it_pulls_the_endpoints_out_of_them(self):
        mentioned = self.found["mentioned"]
        for path in ("/api/v1/session", "/cgi/json-req", "/data/status.json", "/ws"):
            self.assertIn(path, mentioned)
        # The interesting one is never written whole: the app joins it together.
        self.assertIn("system/deviceinfo", mentioned)
        self.assertIn("v1/", mentioned)
        self.assertNotIn("application/json", mentioned)  # a MIME type is not a path
        self.assertNotIn("/assets/logo.svg", mentioned)  # nor is a picture

    def test_it_puts_the_pieces_back_together(self):
        """No string in that bundle spells the endpoint out; joining does."""
        self.assertIn("/api/v1/system/deviceinfo", self.found["rebuilt"])

    def test_it_quotes_the_code_that_builds_the_requests(self):
        """A joined URL can only be understood by reading the joining."""
        snippets = " || ".join(self.found["snippets"])
        self.assertIn("fetch(", snippets)
        self.assertIn("new WebSocket", snippets)
        self.assertIn("system/deviceinfo", snippets)

    def test_it_then_asks_for_what_it_found(self):
        answered = {item["path"]: item for item in self.found["discovered"] if item["status"] == 200}
        self.assertIn("/api/v1/system/deviceinfo", answered)
        self.assertTrue(answered["/api/v1/system/deviceinfo"]["json"])
        self.assertIn("PRV65AX", answered["/api/v1/system/deviceinfo"]["sample"])

    def test_a_route_that_drops_a_post_is_offered_a_handshake(self):
        """A reset is not a dead end: /ws refuses a POST and takes an upgrade."""
        by_path = {item["path"]: item for item in self.found["candidates"]}
        self.assertFalse(by_path["/ws"]["reached"])  # the POST got nothing
        sockets = {item["path"]: item for item in self.found["sockets"]}
        self.assertEqual(sockets["/ws"]["status"], 101)
        self.assertTrue(any("websocket" in header.lower() for header in sockets["/ws"]["headers"]))

    def test_it_speaks_the_dialect_before_calling_a_route_dead(self):
        """The same POST fails or succeeds on its content type alone."""
        spoke = [item for item in self.found["dialect"] if item["path"] == "/ws" and item["json"]]
        self.assertTrue(spoke, self.found["dialect"])
        self.assertIn("contextID", spoke[0]["sample"])
        # And the plain-JSON POST in the known paths got nothing, from the
        # very same route: that difference is the whole point of the probe.
        plain = {item["path"]: item for item in self.found["candidates"]}["/ws"]
        self.assertFalse(plain["reached"])

    def test_the_control_says_what_a_missing_path_looks_like(self):
        """Without this, a 404 could mean anything."""
        controls = {(item["method"], item["reached"]): item for item in self.found["controls"]}
        self.assertEqual([item["status"] for item in self.found["controls"]], [404, 404])
        self.assertTrue(all(reached for _method, reached in controls))

    def test_the_old_shapes_are_reported_as_missing_not_as_working(self):
        by_path = {item["path"]: item for item in self.found["candidates"]}
        self.assertEqual(by_path["/api/v1/general/deviceinfo"]["status"], 404)

    def test_the_report_is_readable_and_complete(self):
        text = swisscom.report(self.found)
        self.assertIn("Internet-Box", text)
        self.assertIn("/api/v1/system/deviceinfo", text)
        self.assertIn("scripts read: 3", text)
        self.assertIn("HTTP 101", text)
        self.assertIn("v1/system/deviceinfo", text)
        self.assertIn("Paste this back", text)


class SwisscomTests(unittest.TestCase):
    def test_it_reports_what_it_could_not_do(self):
        device = {"id": "swisscom-192-168-1-1", "ip": "192.168.1.1", "name": "Internet-Box",
                  "password": "", "source": "swisscom"}
        built = swisscom.home(device, {"reachable": True, "detail": {}})["devices"][0]
        self.assertEqual(built["kind"], "router")
        self.assertFalse(built["controllable"])
        values = {reading["label"]: reading["display"] for reading in built["readings"]}
        self.assertEqual(values["Reachable"], "Yes")
        self.assertEqual(values["Local API"], "No password set")
        self.assertIn("password", built["note"])

    def test_a_rejected_password_is_not_dressed_up_as_success(self):
        device = {"id": "b", "ip": "192.168.1.1", "password": "wrong", "name": "Internet-Box"}
        built = swisscom.home(device, {"reachable": True, "detail": {"error": "no"}})["devices"][0]
        values = {reading["label"]: reading["display"] for reading in built["readings"]}
        self.assertEqual(values["Local API"], "Password not accepted")

    def test_uptime_is_shown_the_way_a_person_reads_it(self):
        self.assertEqual(swisscom._uptime(93825), "1d 2h 3m")  # a day, plus change
        self.assertEqual(swisscom._uptime(65), "1m")  # under an hour: no "0h" clutter
        self.assertEqual(swisscom._uptime(None), "")
        self.assertEqual(swisscom._uptime("not a number"), "")


# --- a fake Internet-Box speaking the dialect a real one answered ------------
# Modelled on what a live Internet-Box 4 actually said when asked: POST
# /sysbus/<Service>:<method> over the SAH content type, answered with no
# "result" envelope; DeviceInfo and NMC public; everything else refused
# (error 13, "Permission denied") until BOTH the contextID from a POST /ws
# login AND the session cookie that same reply sets are sent back -- a real
# login attempt that supplied only the first looked identical to a wrong
# password until tested live, which is why the fake here enforces both. The
# device list itself is not /sysbus/Devices:get (the box never answers that
# to anyone) but a /ws RPC call, exactly as the real webui sends it.

SYSBUS_PASSWORD = "letmein123"
SYSBUS_CONTEXT = "ctx-abc123"
# Two of them, as the real box sets: its own core.min.js says the login "is
# stored in two cookies", and a client that keeps one is refused like a
# client with no password at all.
SYSBUS_COOKIES = ("deviceid/sessid=cookie-xyz", "sah/context=ctx-cookie")
SYSBUS_COOKIE = "; ".join(SYSBUS_COOKIES)


# Ten seconds apart, as the box logs them: 12.5 MB down and 1.25 MB up in
# that time, which is 10 Mbps and 1 Mbps.
SYSBUS_SAMPLES = [
    {"Timestamp": 1_700_000_000, "RxBytes": 1_000_000_000, "TxBytes": 500_000_000},
    {"Timestamp": 1_700_000_010, "RxBytes": 1_012_500_000, "TxBytes": 501_250_000},
]


class SysbusHandler(BaseHTTPRequestHandler):
    logins = 0             # how many times a client logged in
    logs = 0               # how many times the traffic log was fetched
    context = SYSBUS_CONTEXT  # what the box currently accepts; rotate to expire
    samples = SYSBUS_SAMPLES

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(length) or b"{}")
        sah = self.headers.get("Content-Type") == "application/x-sah-ws-4-call+json"
        current = SysbusHandler.context
        authed = (sah and self.headers.get("X-Context") == current
                  and self.headers.get("Authorization") == f"X-Sah {current}"
                  and self.headers.get("Cookie") == SYSBUS_COOKIE)

        if self.path == "/ws" and sah and self.headers.get("Authorization") == "X-Sah-Login":
            parameters = sent.get("parameters", {})
            if (sent.get("service") == "sah.Device.Information" and sent.get("method") == "createContext"
                    and parameters.get("password") == SYSBUS_PASSWORD):
                SysbusHandler.logins += 1
                return self._json(200, {"status": 0, "data": {"contextID": current}},
                                  cookies=SYSBUS_COOKIES)
            return self._json(401, {"status": 1, "data": {}})

        if self.path == "/ws" and sah and sent.get("service") == "Devices" and sent.get("method") == "get":
            if authed:
                return self._json(200, {"status": [{"Name": "dev-1"}, {"Name": "dev-2"}]})
            return self._json(401, {"status": None,
                                    "errors": [{"error": 13, "description": "Permission denied",
                                               "info": "Devices"}]})

        if self.path == "/sysbus/DeviceInfo:get" and sah:
            return self._json(200, {"status": {
                "Manufacturer": "Arcadyan", "ModelName": "IB4-00", "SerialNumber": "SN-TEST-1",
                "SoftwareVersion": "15.20.46", "UpTime": 93825,
            }})

        if self.path == "/sysbus/NMC:get" and sah:
            return self._json(200, {"status": {"ActiveWANInterface": "XGS-PON", "ProvisioningState": "done"}})

        if self.path == "/sysbus/Devices/Device/HGW:getEventLog" and sah:
            if not authed:
                return self._json(401, {"errors": [{"error": 13, "description": "Permission denied"}]})
            SysbusHandler.logs += 1
            return self._json(200, {"status": SysbusHandler.samples})

        if self.path == "/sysbus/NeMo/Intf/veip0:getMIBs" and sah:
            if not authed:
                return self._json(401, {"errors": [{"error": 13, "description": "Permission denied"}]})
            return self._json(200, {"status": {"gpon": {"veip0": {
                # Tenths, as this family reports them.
                "SignalRxPower": -182, "SignalTxPower": 25, "Temperature": 441,
                "MaxBitRateSupported": 10_000_000,  # kbit/s
            }}}})

        # /sysbus/Devices:get is deliberately NOT handled: the real box
        # answers "Permission denied" to it regardless of session, and this
        # fake's 404 makes clear a caller using that shape gets nothing.
        self._json(404, {"error": "not found"})

    def _json(self, status, payload, cookies=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/x-sah-ws-4-call+json")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies:
            self.send_header("Set-Cookie", f"{cookie}; path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)


class SwisscomSysbusTests(unittest.TestCase):
    """The confirmed read path: /sysbus over the SAH dialect, gated at /ws."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SysbusHandler)
        cls.server.daemon_threads = True
        cls.address = f"127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # The box only ever answers this dialect on https; the fake one here
        # is plain HTTP, so this is the one seam swapped for the test.
        cls.original_origin = swisscom._origin
        swisscom._origin = lambda ip: f"http://{ip}"

    @classmethod
    def tearDownClass(cls):
        swisscom._origin = cls.original_origin
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # Sessions outlive a single read by design, so each test starts from
        # a box nobody has logged into yet.
        swisscom._SESSIONS.clear()
        swisscom._SLOW.clear()
        SysbusHandler.logins = 0
        SysbusHandler.logs = 0
        SysbusHandler.context = SYSBUS_CONTEXT
        SysbusHandler.samples = SYSBUS_SAMPLES

    def test_both_cookies_go_back_not_just_the_last_one(self):
        """The fake box refuses a client that kept only one of them."""
        session = swisscom._login(self.address, SYSBUS_PASSWORD)
        self.assertEqual(session["cookie"], SYSBUS_COOKIE)
        self.assertIn("; ", session["cookie"])

    def test_the_session_is_kept_rather_than_remade_every_poll(self):
        """The dashboard polls every four seconds; that is not a login rate."""
        for _ in range(3):
            swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertEqual(SysbusHandler.logins, 1)

    def test_a_session_the_box_forgot_is_replaced_once(self):
        """A refused session and a wrong password look the same; only one is."""
        first = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertEqual(first["detail"]["device_count"], 2)
        SysbusHandler.context = "ctx-rotated"  # as a reboot or a timeout would
        second = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertEqual(second["detail"]["device_count"], 2)
        self.assertEqual(SysbusHandler.logins, 2)

    def test_a_changed_password_does_not_ride_the_old_session(self):
        swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        raw = swisscom.snapshot({"ip": self.address, "password": "nope"})
        self.assertEqual(raw["detail"], {"error": "the box did not accept that password"})

    def test_public_status_needs_no_password(self):
        raw = swisscom.snapshot({"ip": self.address, "password": ""})
        self.assertEqual(raw["manufacturer"], "Arcadyan")
        self.assertEqual(raw["model"], "IB4-00")
        self.assertEqual(raw["firmware"], "15.20.46")
        self.assertEqual(raw["wan_interface"], "XGS-PON")
        self.assertEqual(raw["detail"], {})

    def test_a_correct_password_signs_in_and_counts_devices(self):
        raw = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertTrue(raw["detail"]["authenticated"])
        self.assertEqual(raw["detail"]["device_count"], 2)

    def test_a_wrong_password_is_reported_not_faked(self):
        raw = swisscom.snapshot({"ip": self.address, "password": "nope"})
        self.assertEqual(raw["detail"], {"error": "the box did not accept that password"})

    def test_home_renders_what_was_actually_read(self):
        raw = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        device = {"id": "swisscom-x", "ip": self.address, "name": "Internet-Box",
                  "password": SYSBUS_PASSWORD, "source": "swisscom"}
        built = swisscom.home(device, raw)["devices"][0]
        values = {reading["label"]: reading["display"] for reading in built["readings"]}
        self.assertEqual(values["Local API"], "Signed in")
        self.assertEqual(values["Model"], "IB4-00")
        self.assertEqual(values["Firmware"], "15.20.46")
        self.assertEqual(values["Uptime"], "1d 2h 3m")
        self.assertEqual(values["Devices on the network"], "2")
        self.assertIn("XGS-PON", values["WAN"])
        self.assertEqual(built["note"], "")  # nothing left to explain once signed in

    def test_wan_speed_is_the_difference_between_two_counters(self):
        raw = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertAlmostEqual(raw["throughput"]["down"], 10.0, places=3)
        self.assertAlmostEqual(raw["throughput"]["up"], 1.0, places=3)

    def test_the_line_is_read_in_the_units_a_person_uses(self):
        raw = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        line = raw["line"]
        self.assertAlmostEqual(line["rx_dbm"], -18.2)   # -182 tenths of a dBm
        self.assertAlmostEqual(line["tx_dbm"], 2.5)     # +25 tenths, not +25 dBm
        self.assertAlmostEqual(line["celsius"], 44.1)
        self.assertEqual(line["rate_mbps"], 10_000)     # 10 Gbit/s, given in kbit/s

    def test_the_wan_readings_reach_the_card(self):
        device = {"id": "swisscom-x", "ip": self.address, "name": "Internet-Box",
                  "password": SYSBUS_PASSWORD, "source": "swisscom"}
        raw = swisscom.snapshot(device)
        built = swisscom.home(device, raw)["devices"][0]
        values = {reading["label"]: reading["display"] for reading in built["readings"]}
        self.assertEqual(values["Download"], "10.0 Mbps")
        self.assertEqual(values["Upload"], "1.0 Mbps")
        self.assertEqual(values["Optical RX"], "-18.2 dBm")
        self.assertEqual(values["Line rate"], "10.0 Gbps")

    def test_each_value_is_its_own_kind_so_a_room_can_pin_one(self):
        """resolve_readouts takes the first reading of a kind: sharing one
        would make a pinned Upload show the Download."""
        device = {"id": "swisscom-x", "ip": self.address, "name": "Internet-Box",
                  "password": SYSBUS_PASSWORD, "source": "swisscom"}
        built = swisscom.home(device, swisscom.snapshot(device))["devices"][0]
        kinds = [reading["kind"] for reading in built["readings"]]
        self.assertEqual(len(kinds), len(set(kinds)), kinds)

    def test_the_traffic_log_is_not_fetched_on_every_poll(self):
        for _ in range(3):
            swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertEqual(SysbusHandler.logs, 1)

    def test_a_counter_that_restarted_reads_as_unknown_not_as_a_speed(self):
        SysbusHandler.samples = [
            {"Timestamp": 1_700_000_000, "RxBytes": 9_000_000_000, "TxBytes": 8_000_000_000},
            {"Timestamp": 1_700_000_010, "RxBytes": 12_500_000, "TxBytes": 1_250_000},
        ]
        device = {"id": "swisscom-x", "ip": self.address, "name": "Internet-Box",
                  "password": SYSBUS_PASSWORD, "source": "swisscom"}
        raw = swisscom.snapshot(device)
        self.assertFalse(raw["throughput"]["valid"])
        self.assertIn("restarted", raw["throughput"]["why"])
        shown = {item["label"]: item for item in swisscom.home(device, raw)["devices"][0]["readings"]}
        self.assertEqual(shown["Download"]["display"], "—")
        self.assertFalse(shown["Download"]["valid"])

    def test_a_log_it_cannot_read_is_admitted_rather_than_averaged(self):
        SysbusHandler.samples = [{"Something": "else"}, {"Another": "shape"}]
        raw = swisscom.snapshot({"ip": self.address, "password": SYSBUS_PASSWORD})
        self.assertFalse(raw["throughput"]["valid"])
        self.assertIn("two samples", raw["throughput"]["why"])

    def test_without_a_password_there_is_no_wan_reading_at_all(self):
        raw = swisscom.snapshot({"ip": self.address, "password": ""})
        self.assertEqual(raw["throughput"], {})
        self.assertEqual(SysbusHandler.logs, 0)

    def test_an_unreachable_box_raises_rather_than_faking_a_reading(self):
        with self.assertRaises(swisscom.SwisscomError):
            swisscom.snapshot({"ip": "127.0.0.1:1", "password": ""})


class RegistryTests(unittest.TestCase):
    def test_every_integration_offers_the_same_shape(self):
        for source, module in integrations.BY_SOURCE.items():
            for name in ("SOURCE", "probe", "discover", "snapshot"):
                self.assertTrue(hasattr(module, name), f"{source} is missing {name}")
            self.assertEqual(module.SOURCE, source)

    def test_what_can_be_written_matches_what_is_advertised(self):
        for source, detail in integrations.ACCESS.items():
            writable = integrations.can_write(source)
            self.assertEqual(
                writable, detail["control"] == "full",
                f"{source} claims {detail['control']} but {'has' if writable else 'has no'} send()",
            )


if __name__ == "__main__":
    unittest.main()
