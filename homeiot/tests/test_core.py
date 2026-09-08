"""Unit tests for the pure parts: colour, normalisation, payloads, storage.

    python3 -m unittest discover -s homeiot/tests -t .
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from homeiot import camera, color, demo, discovery, hue, hub as hub_module, model, server, shelly, store


class ColorTests(unittest.TestCase):
    def test_hex_round_trip_stays_close(self):
        for swatch in ("#ff0000", "#33aa77", "#ffd1a1"):
            x, y = color.hex_to_xy(swatch)
            self.assertTrue(0 < x < 1 and 0 < y < 1)
            self.assertTrue(color.in_gamut((x, y), color.GAMUT_C))

    def test_out_of_gamut_points_are_pulled_onto_the_triangle(self):
        point = color.clamp_to_gamut((0.9, 0.05), color.GAMUT_C)
        self.assertTrue(color.in_gamut(point, color.GAMUT_C))

    def test_warm_temperatures_are_warmer_than_cool_ones(self):
        warm = color.hex_to_rgb(color.mirek_to_hex(500))
        cool = color.hex_to_rgb(color.mirek_to_hex(153))
        self.assertGreater(warm[0] - warm[2], cool[0] - cool[2])

    def test_mirek_kelvin_round_trip(self):
        self.assertAlmostEqual(color.kelvin_to_mirek(color.mirek_to_kelvin(370)), 370, places=6)

    def test_brightness_darkens_the_swatch(self):
        bright = color.hex_to_rgb(color.xy_to_hex(0.45, 0.41, 100))
        dim = color.hex_to_rgb(color.xy_to_hex(0.45, 0.41, 5))
        self.assertGreater(sum(bright), sum(dim))


class PayloadTests(unittest.TestCase):
    def test_brightness_and_on(self):
        payload = hue.build_payload({"on": True, "brightness": 42.5})
        self.assertEqual(payload["on"], {"on": True})
        self.assertEqual(payload["dimming"], {"brightness": 42.5})

    def test_off_drops_colour_and_brightness(self):
        payload = hue.build_payload({"on": False, "brightness": 80, "hex": "#ff0000"})
        self.assertEqual(payload, {"on": {"on": False}})

    def test_toggle_reads_current_state(self):
        self.assertEqual(hue.build_payload({"toggle": True}, {"on": {"on": True}})["on"], {"on": False})

    def test_colour_wins_over_temperature(self):
        payload = hue.build_payload({"hex": "#00ff00", "mirek": 300})
        self.assertIn("color", payload)
        self.assertNotIn("color_temperature", payload)

    def test_mirek_is_clamped(self):
        self.assertEqual(hue.build_payload({"mirek": 999})["color_temperature"]["mirek"], 500)

    def test_brightness_delta_direction(self):
        self.assertEqual(hue.build_payload({"brightness_delta": -12})["dimming_delta"]["action"], "down")

    def test_v1_translation(self):
        state = hue.to_v1_state(hue.build_payload({"on": True, "brightness": 100, "mirek": 300}))
        self.assertEqual(state, {"on": True, "bri": 254, "ct": 300})


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.bridge = dict(demo.BRIDGE)
        self.resources = demo.build_state()
        self.home = model.build_home(self.bridge, self.resources)

    def test_every_device_is_normalised(self):
        self.assertEqual(len(self.home["devices"]), len(self.resources["device"]))
        for device in self.home["devices"]:
            self.assertTrue(device["id"].startswith("hue:"))
            self.assertIn(device["kind"], ("light", "plug", "sensor", "switch", "bridge", "other"))

    def test_the_full_range_of_device_kinds_is_present(self):
        kinds = {device["kind"] for device in self.home["devices"]}
        self.assertTrue({"light", "plug", "sensor", "switch"} <= kinds)

    def test_lights_expose_their_capabilities(self):
        colour_lamp = next(d for d in self.home["devices"] if d["name"] == "Sofa left")
        self.assertEqual(
            set(colour_lamp["capabilities"]), {"on_off", "dimming", "color_temp", "color", "effects", "alert"}
        )
        plug = next(d for d in self.home["devices"] if d["kind"] == "plug")
        self.assertEqual(plug["capabilities"], ["on_off"])

    def test_sensor_readings_are_human_readable(self):
        sensor = next(d for d in self.home["devices"] if d["kind"] == "sensor" and d["readings"])
        kinds = {reading["kind"] for reading in sensor["readings"]}
        self.assertTrue({"motion", "temperature", "light_level"} <= kinds)
        self.assertTrue(sensor["battery"]["level"] > 0)

    def test_rooms_summarise_their_lights(self):
        living = next(g for g in self.home["groups"] if g["name"] == "Living room")
        self.assertEqual(living["kind"], "room")
        self.assertGreater(living["state"]["count"], 0)
        self.assertTrue(living["scenes"])

    def test_devices_know_their_room(self):
        lamp = next(d for d in self.home["devices"] if d["name"] == "Sofa left")
        self.assertEqual(lamp["room_name"], "Living room")

    def test_lux_conversion(self):
        self.assertAlmostEqual(model.lux_from_level(1), 1.0, places=3)
        self.assertGreater(model.lux_from_level(21000), 100)

    def test_write_targets_resolve(self):
        lamp = next(d for d in self.home["devices"] if d["name"] == "Sofa left")
        self.assertEqual(model.write_targets(self.home, [], lamp["id"]),
                         [{"bridge": self.bridge["id"], "rtype": "light", "rid": "light-0"}])

        living = next(g for g in self.home["groups"] if g["name"] == "Living room")
        writes = model.write_targets(self.home, [], living["id"])
        self.assertEqual(writes, [{"bridge": self.bridge["id"], "rtype": "grouped_light", "rid": "grouped-living"}])

    def test_collections_expand_to_their_members(self):
        lamp = next(d for d in self.home["devices"] if d["name"] == "Sofa left")
        living = next(g for g in self.home["groups"] if g["name"] == "Living room")
        collection = {"id": "col-1", "name": "Evening", "members": [lamp["id"], living["id"]], "icon": "", "order": 0}
        writes = model.write_targets(self.home, [collection], "col-1")
        self.assertEqual(len(writes), 2)
        decorated = model.decorate_collections([collection], self.home)[0]
        self.assertEqual(decorated["missing"], 0)
        self.assertEqual(decorated["state"]["count"], 2)

    def test_events_patch_resources(self):
        events = [{"type": "update", "data": [{"id": "light-0", "type": "light", "on": {"on": False}}]}]
        updated, changed = model.apply_events(self.resources, events)
        self.assertTrue(changed)
        light = next(item for item in updated["light"] if item["id"] == "light-0")
        self.assertFalse(light["on"]["on"])
        self.assertIn("dimming", light)  # the merge is deep, not a replacement

    def test_deleting_a_resource(self):
        events = [{"type": "delete", "data": [{"id": "light-0", "type": "light"}]}]
        updated, _ = model.apply_events(self.resources, events)
        self.assertNotIn("light-0", [item["id"] for item in updated["light"]])


class V1TranslationTests(unittest.TestCase):
    RAW = {
        "config": {"name": "Bridge", "timezone": "Europe/Prague"},
        "lights": {
            "1": {
                "state": {"on": True, "bri": 127, "ct": 300, "xy": [0.4, 0.4], "colormode": "ct",
                          "effect": "none", "reachable": True},
                "type": "Extended color light", "name": "Old lamp", "modelid": "LCT001",
                "manufacturername": "Philips", "productname": "Hue color lamp",
                "capabilities": {"control": {"ct": {"min": 153, "max": 500}, "colorgamuttype": "B"}},
            }
        },
        "groups": {"1": {"name": "Salon", "type": "Room", "class": "Living room", "lights": ["1"],
                         "state": {"any_on": True, "all_on": True}, "action": {"bri": 127}}},
        "scenes": {"abc": {"name": "Evening", "group": "1", "type": "GroupScene"}},
        "sensors": {
            "2": {"type": "ZLLPresence", "name": "Hall motion", "modelid": "SML001",
                  "uniqueid": "00:17:88:01:02:03:04:05-02-0406",
                  "state": {"presence": True, "lastupdated": "2026-01-01T10:00:00"},
                  "config": {"on": True, "battery": 90, "reachable": True}},
            "9": {"type": "Daylight", "name": "Daylight", "state": {"daylight": True}, "config": {}},
        },
    }

    def test_v1_becomes_a_normal_home(self):
        bridge = {"id": "v1bridge", "ip": "10.0.0.5", "app_key": "key", "api": "v1"}
        resources = hue.translate_v1(self.RAW, bridge)
        home = model.build_home(bridge, resources)

        lamp = next(d for d in home["devices"] if d["name"] == "Old lamp")
        self.assertEqual(lamp["kind"], "light")
        self.assertEqual(lamp["state"]["brightness"], 50.0)
        self.assertEqual(lamp["room_name"], "Salon")
        self.assertIn("color", lamp["capabilities"])

        sensor = next(d for d in home["devices"] if d["kind"] == "sensor")
        self.assertEqual(sensor["readings"][0]["display"], "Motion")
        self.assertEqual(sensor["battery"]["level"], 90)

        self.assertEqual([scene["name"] for scene in home["scenes"]], ["Evening"])
        self.assertEqual(len(home["groups"]), 1)

    def test_virtual_sensors_are_skipped(self):
        bridge = {"id": "v1bridge", "ip": "10.0.0.5", "app_key": "key", "api": "v1"}
        resources = hue.translate_v1(self.RAW, bridge)
        self.assertEqual(len(resources["motion"]), 1)


class DemoTransportTests(unittest.TestCase):
    def test_writing_to_a_light_changes_the_snapshot(self):
        demo._STATE.clear()
        bridge = dict(demo.BRIDGE)
        demo.send(bridge, "light", "light-0", {"on": {"on": False}})
        light = next(item for item in demo.snapshot(bridge)["light"] if item["id"] == "light-0")
        self.assertFalse(light["on"]["on"])

    def test_writing_to_a_room_reaches_every_lamp(self):
        demo._STATE.clear()
        bridge = dict(demo.BRIDGE)
        demo.send(bridge, "grouped_light", "grouped-living", {"on": {"on": True}, "dimming": {"brightness": 30.0}})
        state = demo.snapshot(bridge)
        home = model.build_home(bridge, state)
        living = next(g for g in home["groups"] if g["name"] == "Living room")
        self.assertEqual(living["state"]["on_count"], living["state"]["count"])
        for device in home["devices"]:
            if device["room_name"] == "Living room" and device["kind"] == "light":
                self.assertEqual(device["state"]["brightness"], 30.0)

    def test_recalling_a_scene_marks_it_active(self):
        demo._STATE.clear()
        bridge = dict(demo.BRIDGE)
        demo.send(bridge, "scene", "scene-0", {"recall": {"action": "active"}})
        home = model.build_home(bridge, demo.snapshot(bridge))
        self.assertTrue(next(s for s in home["scenes"] if s["rid"] == "scene-0")["active"])


class StoreTests(unittest.TestCase):
    def test_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = store.load(path)
            config = store.put_bridge(config, {"id": "abc", "ip": "10.0.0.2"})
            store.save(config, path)
            if os.name == "posix":  # Windows has no mode bits to check
                self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
            self.assertEqual(store.load(path)["bridges"][0]["id"], "abc")

    def test_collections_are_sanitised(self):
        collection = store.normalise_collection({"name": " Evening  ", "members": ["a", "a", "b"]})
        self.assertEqual(collection["name"], "Evening")
        self.assertEqual(collection["members"], ["a", "b"])
        self.assertTrue(collection["id"].startswith("col-"))

    def test_removing_a_bridge_prunes_members(self):
        config = store.load(Path("/nonexistent"))
        config = store.put_collection(config, store.normalise_collection({"name": "X", "members": ["hue:a:device:1"]}))
        pruned = store.forget_devices(config, {"hue:a:device:1"})
        self.assertEqual(pruned["collections"][0]["members"], [])


class DiscoveryTests(unittest.TestCase):
    def test_dns_name_encoding_round_trip(self):
        packet = discovery.build_query(["_hue._tcp.local"])
        name, _offset = discovery._read_name(packet, 12)
        self.assertEqual(name, "_hue._tcp.local")

    def test_response_parsing(self):
        packet = (
            b"\x00\x00\x84\x00\x00\x00\x00\x02\x00\x00\x00\x00"
            + discovery.encode_name("_hue._tcp.local")
            + b"\x00\x0c\x00\x01\x00\x00\x00\x78"
            + len(discovery.encode_name("Bridge._hue._tcp.local")).to_bytes(2, "big")
            + discovery.encode_name("Bridge._hue._tcp.local")
            + discovery.encode_name("bridge.local")
            + b"\x00\x01\x00\x01\x00\x00\x00\x78\x00\x04\xc0\xa8\x01\x2a"
        )
        records = discovery.parse_dns(packet)
        self.assertIn({"name": "bridge.local", "type": 1, "value": "192.168.1.42"}, records)

    def test_bridge_version_detection(self):
        self.assertFalse(discovery._supports_clip_v2({"modelid": "BSB001", "apiversion": "1.16.0"}))
        self.assertFalse(discovery._supports_clip_v2({"modelid": "BSB002", "apiversion": "1.40.0"}))
        self.assertTrue(discovery._supports_clip_v2({"modelid": "BSB002", "apiversion": "1.66.0"}))

    def test_txt_records(self):
        self.assertEqual(discovery._parse_txt(b"\x07key=val"), {"key": "val"})


class ShellyTests(unittest.TestCase):
    GEN2 = {
        "status": {
            "switch:0": {"id": 0, "output": True, "apower": 842.5, "voltage": 231.4,
                         "aenergy": {"total": 128340.0}},
            "temperature:0": {"tC": 21.9},
            "humidity:0": {"rh": 46.0},
            "devicepower:0": {"battery": {"V": 2.9, "percent": 74}},
        }
    }
    GEN1 = {
        "status": {
            "relays": [{"ison": True}, {"ison": False}],
            "meters": [{"power": 12.5, "total": 60000}, {"power": 0.0, "total": 0}],
            "tmp": {"value": 71.6, "units": "F", "is_valid": True},
            "hum": {"value": 55.0, "is_valid": True},
            "bat": {"value": 88},
            "wifi_sta": {"connected": True},
        },
        "settings": {"name": "Old relay"},
    }

    def test_gen2_becomes_one_controllable_device(self):
        device = {"id": "shellyplus1pm-a1", "name": "Boiler", "model": "SNSW-001P16EU", "generation": 2}
        built = shelly.home(device, self.GEN2)["devices"]
        self.assertEqual(len(built), 1)
        entry = built[0]
        self.assertEqual(entry["id"], "shelly:shellyplus1pm-a1:switch:0")
        self.assertEqual(entry["kind"], "relay")
        self.assertEqual(entry["capabilities"], ["on_off"])
        self.assertTrue(entry["state"]["on"])
        self.assertEqual(entry["battery"]["level"], 74)
        readings = {reading["kind"]: reading["display"] for reading in entry["readings"]}
        self.assertEqual(readings["power"], "842.5 W")
        self.assertEqual(readings["energy"], "128.34 kWh")
        self.assertEqual(readings["temperature"], "21.9 °C")
        self.assertEqual(readings["humidity"], "46 %")

    def test_gen1_multi_channel_and_fahrenheit(self):
        device = {"id": "shsw-25-b2", "name": "", "model": "SHSW-25", "generation": 1}
        built = shelly.home(device, self.GEN1)["devices"]
        self.assertEqual([entry["name"] for entry in built], ["Old relay 1", "Old relay 2"])
        self.assertTrue(built[0]["state"]["on"])
        self.assertFalse(built[1]["state"]["on"])
        # Sensors belong to the device, so they are listed once, on channel one.
        kinds = {reading["kind"] for reading in built[0]["readings"]}
        self.assertTrue({"power", "energy", "temperature", "humidity"} <= kinds)
        self.assertEqual({reading["kind"] for reading in built[1]["readings"]}, {"power", "energy"})
        temperature = next(r for r in built[0]["readings"] if r["kind"] == "temperature")
        self.assertEqual(temperature["display"], "22 °C")  # 71.6F
        self.assertEqual(built[0]["battery"]["level"], 88)

    def test_a_device_with_only_sensors_is_not_controllable(self):
        device = {"id": "shellyht-c3", "name": "Cellar", "model": "SHHT-1", "generation": 1}
        built = shelly.home(device, {"status": {"tmp": {"value": 8.5}, "hum": {"value": 80.0}}})["devices"]
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["kind"], "sensor")
        self.assertFalse(built[0]["controllable"])

    def test_ids_route_writes_back_to_the_channel(self):
        device = {"id": "shellyplus1pm-a1", "name": "Boiler", "model": "X", "generation": 2}
        home = shelly.home(device, self.GEN2)
        writes = model.write_targets(home, [], home["devices"][0]["id"])
        self.assertEqual(writes, [{"bridge": "shellyplus1pm-a1", "rtype": "switch", "rid": "0"}])

    def test_probe_reads_both_generations(self):
        self.assertEqual(model.parse_id("shelly:abc:switch:0"),
                         {"source": "shelly", "bridge": "abc", "rtype": "switch", "rid": "0"})


class ReadoutTests(unittest.TestCase):
    HOME = {
        "devices": [
            {"id": "shelly:x:switch:0", "name": "Boiler", "readings": [
                {"kind": "temperature", "display": "21.9 °C", "value": 21.9, "unit": "°C"},
                {"kind": "power", "display": "842.5 W", "value": 842.5, "unit": "W"},
            ]}
        ],
        "groups": [],
    }

    def test_a_readout_picks_up_its_live_value(self):
        readout = store.normalise_readout(
            {"label": "Energy usage", "device": "shelly:x:switch:0", "kind": "power", "room": "hue:b:room:1"}
        )
        resolved = hub_module.resolve_readouts([readout], self.HOME)[0]
        self.assertEqual(resolved["display"], "842.5 W")
        self.assertEqual(resolved["room"], "hue:b:room:1")
        self.assertFalse(resolved["missing"])

    def test_a_readout_for_a_vanished_device_says_so(self):
        readout = store.normalise_readout({"label": "Gone", "device": "shelly:zz:switch:0", "kind": "power"})
        resolved = hub_module.resolve_readouts([readout], self.HOME)[0]
        self.assertTrue(resolved["missing"])
        self.assertEqual(resolved["display"], "—")

    def test_the_label_defaults_to_the_value_name(self):
        self.assertEqual(
            store.normalise_readout({"device": "d", "kind": "light_level"})["label"], "Light Level"
        )

    def test_a_readout_needs_something_to_read(self):
        with self.assertRaises(ValueError):
            store.normalise_readout({"label": "Nothing"})

    def test_removing_a_device_removes_its_readouts(self):
        config = store.load(Path("/nonexistent"))
        config = store.put_readout(config, store.normalise_readout({"device": "shelly:x:switch:0", "kind": "power"}))
        pruned = store.forget_devices(config, {"shelly:x:switch:0"})
        self.assertEqual(pruned["readouts"], [])


class CameraTests(unittest.TestCase):
    def test_credentials_are_masked_not_leaked(self):
        masked = camera.mask_url("rtsp://admin:hunter2@10.0.0.9:554/h264")
        self.assertEqual(masked, "rtsp://admin:***@10.0.0.9:554/h264")
        self.assertNotIn("hunter2", masked)
        described = camera.describe({"id": "c1", "name": "Door", "rtsp_url": "rtsp://u:p@h/s"})
        self.assertNotIn("p@h", described["url"])

    def test_redaction_covers_error_text(self):
        self.assertNotIn("hunter2", camera.redact("failed: rtsp://admin:hunter2@10.0.0.9/h264 timed out"))

    def test_the_mode_says_what_can_actually_be_shown(self):
        self.assertEqual(camera.mode({"demo": True}), "demo")
        self.assertEqual(camera.mode({"snapshot_url": "http://h/s.jpg"}), "snapshot")
        self.assertEqual(camera.mode({}), "unconfigured")
        # Without ffmpeg an RTSP camera reports why, rather than showing nothing.
        expected = "stream" if camera.have_ffmpeg() else "needs_ffmpeg"
        self.assertEqual(camera.mode({"rtsp_url": "rtsp://h/s"}), expected)

    def test_only_real_stream_addresses_are_accepted(self):
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://h/s"):
            with self.assertRaises(ValueError):
                camera.normalise({"id": "c1", "name": "X", "rtsp_url": bad})
        ok = camera.normalise({"id": "c1", "name": "X", "rtsp_url": "rtsp://h/s", "room": "hue:b:room:1"})
        self.assertEqual(ok["room"], "hue:b:room:1")

    def test_a_camera_needs_an_id(self):
        with self.assertRaises(ValueError):
            camera.normalise({"name": "X"})

    def test_the_demo_frame_is_a_real_png(self):
        frame = camera.demo_frame(32, 18)
        self.assertEqual(frame[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn(b"IEND", frame)


class ServerTests(unittest.TestCase):
    """A client that vanishes is routine, not a crash to report."""

    ABORT = b"\x01\x00\x00\x00\x00\x00\x00\x00"  # SO_LINGER: reset, do not close politely

    def setUp(self):
        hub = hub_module.create(store.load(Path("/nonexistent")))
        self.httpd = server.DashboardServer(("127.0.0.1", 0), server.make_handler(hub, None))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _abort(self, request: bytes | None, read: bool) -> None:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        if request:
            client.sendall(request)
        if read:
            client.recv(64)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, self.ABORT)
        client.close()

    def test_dropped_connections_stay_quiet(self):
        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            for _ in range(3):
                self._abort(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n", read=True)
                self._abort(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n", read=True)
                self._abort(b"GET /api/st", read=False)  # half a request, then gone
                self._abort(None, read=False)  # connected and said nothing
            time.sleep(0.4)
        self.assertNotIn("Traceback", noise.getvalue())
        self.assertEqual(noise.getvalue(), "")

    def test_the_server_still_answers_afterwards(self):
        self._abort(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n", read=True)
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        client.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertIn(b"200 OK", client.recv(256))
        client.close()


if __name__ == "__main__":
    unittest.main()
