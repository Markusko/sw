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
        self.assertIn("--probe-box", built["note"])

    def test_a_rejected_password_is_not_dressed_up_as_success(self):
        device = {"id": "b", "ip": "192.168.1.1", "password": "wrong", "name": "Internet-Box"}
        built = swisscom.home(device, {"reachable": True, "detail": {"error": "no"}})["devices"][0]
        values = {reading["label"]: reading["display"] for reading in built["readings"]}
        self.assertEqual(values["Local API"], "Password not accepted")


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
