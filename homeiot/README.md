# homeiot

A local dashboard for the IoT devices on your own network. It finds what it
can, shows everything with its live state, and controls what can honestly be
controlled — lamps, plugs, relays, thermostats, speakers, rooms, zones, scenes,
and your own cross-cutting **Collections**. Room cards can also carry the
values you care about (temperature, humidity, power) and a camera's picture.

| Integration | Reach | Needs |
| --- | --- | --- |
| Philips Hue | full control | link button, once |
| Shelly (Gen1 and Gen2+) | full control | — |
| Sonos | full control | — |
| Meross (MTS200B and friends) | full control | your account's device key |
| Google Cast / NVIDIA Shield | read-only | — |
| Home Connect (Siemens, Bosch) | listed only | — |
| Swisscom Internet-Box | read-only | the box's password |

Where an integration stops, the dashboard says so and offers no control it
cannot honour. The reasons are under **What it does**.

It runs on your machine, talks only to your LAN, and stores everything under
`homeiot/data/`. No cloud account, no broker, nothing to install.

Built for a tablet on the wall or the kitchen counter: every control is a
native HTML input, sized for a thumb.

```
python3 -m homeiot            # then open the printed http://<your-ip>:8712
python3 -m homeiot --demo     # simulated devices, no hardware needed
```

Python 3.11+, standard library only. The browser side uses Bootstrap and
morphdom, both vendored in `web/vendor/` -- the tablet must keep working when
the internet does not, so nothing is fetched from a CDN at runtime.

The one optional extra is **ffmpeg**, and only for RTSP cameras: everything
else works without it.

---

## First run

1. Start it on a machine on the same network as the bridge.
2. Open the URL it prints — from any phone or laptop at home.
3. Press **Search for bridges**, press the round link button on top of the
   bridge, then press **Pair** within 30 seconds.

The application key the bridge hands back is written to
`homeiot/data/config.json` with `0600` permissions. That file is the only
state; delete it to start over.

If discovery finds nothing (some networks block multicast), type the bridge's
address into the field below the button. `python3 -m homeiot --discover` runs
the same search from the command line, including a sweep of your /24.

## Windows, from PowerShell

Install Python 3.11 or newer (`winget install Python.Python.3.12`, or python.org —
tick *Add python.exe to PATH*), then, from the folder that **contains** `homeiot`
(the repository root, not the `homeiot` folder itself — it runs as a module):

```powershell
py -3 -m homeiot --demo     # try it with simulated devices
py -3 -m homeiot            # the real thing
```

It prints the address to open, e.g. `http://192.168.1.20:8712`. Stop it with
Ctrl+C.

**Let other devices reach it.** Windows blocks the inbound port by default. Say
*Allow* if Defender prompts on first run; otherwise, in an **Administrator**
PowerShell:

```powershell
New-NetFirewallRule -DisplayName "homeiot" -Direction Inbound `
  -Protocol TCP -LocalPort 8712 -Action Allow -Profile Private
```

Keep it to `-Profile Private` so it is never exposed on a public network.

**Run it without a console window**, and start it at logon:

```powershell
$root = "C:\path\to\sw"
$action  = New-ScheduledTaskAction -Execute "pythonw.exe" -Argument "-m homeiot" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName homeiot -Action $action -Trigger $trigger -Description "Home IoT dashboard"
```

Start or stop it on demand with `Start-ScheduledTask homeiot` /
`Stop-ScheduledTask homeiot`. To kill whatever is holding the port:

```powershell
Get-NetTCPConnection -LocalPort 8712 -State Listen |
  Select-Object -ExpandProperty OwningProcess | Stop-Process
```

**If discovery finds nothing**, it is usually a Windows machine with several
adapters — a VPN, Hyper-V or WSL virtual switch — sending the multicast query
out of the wrong one. Two ways round it: `py -3 -m homeiot --discover`, which
also sweeps your /24 directly, or the *enter an address* box on the setup
screen. Pairing works the same either way.

Everything else in this README applies unchanged; only `run.sh` is
POSIX-only, and on Windows you simply call the module instead.

## What it does

**Lights** — on/off, brightness, colour (presets, or the device's own colour
picker; either way clamped to the lamp's actual gamut), colour temperature in
kelvin, effects (candle, fire, sparkle, prism …), identify, rename. Devices
with several lamps inside them get per-lamp switches too.

**Plugs and relays** — on/off, with whatever the device meters.

**Shelly** — Gen1 (Shelly 1, 2.5, Plug S, H&T …) and Gen2+ (Plus, Pro, Gen3/4)
over plain HTTP, found by mDNS or by a sweep of your subnet, or added by
address. Relays, dimmers and plugs switch and dim; power, voltage, energy,
temperature, humidity and battery are read. A device with a password set is
reported as such rather than half-working — neither Gen1 Basic nor Gen2 Digest
auth is implemented yet.

**Values on a room card** — pin any value a device reports to any room, with a
label you choose: *Temperature 21.9 °C*, *Energy usage 842 W*. The device does
not have to be in that room, which is the point: a Shelly in the utility
cupboard can report the living-room temperature. Add and remove them in the
room's own window.

**Cameras** — give a camera an RTSP address and it appears on its room's card
as a still and in the room's window as a live picture. No browser can play
RTSP, so ffmpeg on this machine converts it to a picture stream any browser can
show, in an ordinary `<img>` — no player, no plugin. Without ffmpeg, give the
camera its snapshot URL instead (most cameras have one) and it updates once a
second; if neither is possible the dashboard says so rather than showing a
broken picture. Credentials in the address are stored in the config file and
masked everywhere they would otherwise be shown.

**Sensors** — motion, temperature, illuminance in lux, contact and tamper for
the secure range, battery level, and how long ago each reading changed.

**Switches** — dimmer switches, tap dials and wall modules with their last
button event and battery.

**Rooms and zones** — the groups you already made in the Hue app, each with one
switch, one brightness slider, and its scenes as buttons. A room writes to the
bridge's `grouped_light` service, so the whole room changes in one round trip.

**Scenes** — recall any scene from the card of the room it belongs to.

**Collections** — your own groupings, which is the part Hue does not do: any
mix of devices, rooms and zones under one name, controlled together. "Evening",
"Desk", "Away lights", "Everything upstairs".

> **On the name.** You asked for something better than "systems". The
> alternatives worth considering were *Spaces* (too close to Rooms), *Circuits*
> (sounds electrical, implies wiring), *Ensembles* (pretty, but vague) and
> *Sets* (too mathematical). **Collections** won: it is neutral about what is
> inside, it does not collide with Hue's own vocabulary (room, zone, scene,
> group), and it reads naturally in a sentence — "add this lamp to a
> collection". Renaming it later is a one-word change in `app.js` and the
> `collections` key in `config.json`.

**Sonos** — every speaker's own local API on port 1400: play, pause, skip,
volume, mute, and what is playing now. No account, no cloud, no key; found by
SSDP. This is the most open of the lot and the integration is complete.

**Meross** — the MTS200B underfloor-heating thermostat: room temperature, the
target, whether it is calling for heat, and setting the target (which switches
it out of its schedule, or the schedule would put it straight back). Meross
switches and plugs come along with it. Controlled **on your own network**, not
through their cloud — but every local request is signed with the key your
account set when the device was paired, so that key has to be supplied once,
from the Meross app or from your account at iot.meross.com. Meross devices do
not announce themselves usefully, so searching for them sweeps the subnet.

**Google Cast, including the NVIDIA Shield** — read-only, and deliberately so.
A Cast device keeps its mDNS announcement current: the name, the model, whether
an app is running and usually what it is playing. That is shown. A remote would
need either the Cast channel protocol (protobuf over TLS) or ADB on the Shield
with debugging enabled and a pairing dance; neither is implemented, so no
buttons are offered that would do nothing.

**Home Connect (Siemens, Bosch)** — the dishwasher and the hob are identified
from what they announce: appliance type, brand, model number and serial.
Programme state and remote start need the per-appliance keys that exist only
inside your Home Connect account, or their cloud API behind OAuth and a
developer registration. Neither can be taken from the network, so the appliance
is listed with a note saying exactly that rather than showing empty dials.

**Swisscom Internet-Box** — Swisscom does not publish this box's local API and
it has changed between generations, so this one is written to find out rather
than to assume. It recognises the box and, with the password, tries the login
shapes these boxes are known to accept. Run:

```
python3 -m homeiot --probe-box 192.168.1.1
```

and it prints exactly what your box answers on each known endpoint. That output
is what proper support should be written against; guessing past it would only
produce a dashboard that lies. The MQTT service the box advertises is
Swisscom's own, for their mesh repeaters, and needs credentials this cannot
obtain.

**Settings** — find, add and remove everything above, manage cameras, and scan
the LAN for whatever else answers mDNS or SSDP: HomeKit accessories, Matter
devices, Tasmota nodes, ESPHome, printers. Each integration is listed with what
it can reach and what it needs from you.

## Live updates

Bridges on API 1.46+ have a server-sent event stream, and the dashboard follows
it: press a physical dimmer switch and the card moves at once. The browser gets
its own event stream from this server, so several phones stay in sync with each
other. Older bridges are polled every four seconds instead. The header pill
says which mode you are in.

## The interface

Every control is the browser's own, styled by Bootstrap:

| Control | What it actually is |
| --- | --- |
| On/off | `<input type="checkbox" role="switch">` |
| Dimmer, warmth | `<input type="range">` |
| Colour | preset buttons and `<input type="color">` |
| Effect | `<select>` |
| Detail panel | Bootstrap modal, centred above the page |

That is the whole design rule, and it is what makes it reliable on a tablet:
touch handling, momentum, focus rings, keyboard support, the OS colour picker
and every accessibility affordance come from the platform rather than from
code of mine that has to be right on every device. Switches, dimmers and tabs
are at least 44px on their short side, and the end-to-end suite measures them
on every run rather than trusting the CSS.

Live updates are applied to the page with morphdom, so nothing is torn down
under a finger mid-gesture.

## Layout

```
homeiot/
├── __main__.py      python3 -m homeiot
├── server.py        HTTP: JSON API, static files, SSE, CLI
├── hub.py           the running home: refresh loops, event stream, writes
├── model.py         CLIP v2 resources -> the flat model the UI speaks
├── hue.py           bridge transport: pair, read, write, stream, v1 fallback
├── discovery.py     mDNS, SSDP, cloud discovery, subnet sweep
├── color.py         xy <-> sRGB, mired <-> kelvin, gamut clamping
├── store.py         config.json, atomically, 0600
├── integrations.py  the registry: which module speaks for which device
├── shelly.py        Shelly Gen1 and Gen2+: discovery, reading, control
├── sonos.py         Sonos: SSDP, SOAP transport and volume
├── meross.py        Meross local API: signed requests, MTS200B thermostat
├── cast.py          Google Cast / NVIDIA Shield: read-only, from mDNS
├── homeconnect.py   Siemens and Bosch appliances: identification only
├── swisscom.py      Internet-Box: recognition, and a prober for the rest
├── camera.py        RTSP via ffmpeg, snapshot polling, URL masking
├── demo.py          a simulated bridge, Shelly and camera
├── web/             index.html, app.css, app.js — no build step
│   └── vendor/      bootstrap.min.css, bootstrap.bundle.min.js, morphdom
├── data/            config.json lives here (gitignored)
└── tests/           unit tests, plus a Playwright end-to-end script
```

Two API dialects go in, one model comes out: CLIP v2 for modern bridges, and a
translation layer that reshapes the old v1 API into the same resources, so the
round bridge and 2015 firmware still work without a second code path anywhere
above `hue.py`.

## API

Everything the UI does is available over HTTP. Ids look like
`hue:<bridge>:<type>:<uuid>`, and `PUT /api/targets/<id>/state` accepts the same
body whether the id is a lamp, a device, a room, a zone or a collection:

```bash
curl -X PUT http://localhost:8712/api/targets/hue:001788.../state \
     -H 'Content-Type: application/json' \
     -d '{"on": true, "brightness": 40, "hex": "#ffb26b", "transition": 800}'
```

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/state` | full snapshot |
| GET | `/api/events` | SSE stream of snapshots |
| POST | `/api/discover` | search for bridges (`{"deep": true}` sweeps the subnet) |
| POST | `/api/pair` | `{"ip": "..."}` — 428 until the link button is pressed |
| DELETE | `/api/bridges/<id>` | forget a bridge |
| PUT | `/api/targets/<id>/state` | `on`, `toggle`, `brightness`, `brightness_delta`, `hex`, `xy`, `mirek`, `kelvin`, `effect`, `alert`, `transition` |
| PUT | `/api/targets/<id>/name` | rename |
| POST | `/api/scenes/<id>/recall` | recall a scene |
| POST | `/api/devices/<id>/identify` | make it blink |
| GET/POST/PUT/DELETE | `/api/collections[/<id>]` | manage collections |
| GET | `/api/sources` | the integrations, their reach and what they need |
| POST | `/api/adopt` | `{"source", "ip", "key"/"password"}` — take on a device |
| POST | `/api/shelly` | `{"ip": "..."}` — adopt a Shelly, no pairing needed |
| GET/POST/PUT/DELETE | `/api/readouts[/<id>]` | values pinned to a room |
| GET/POST/PUT/DELETE | `/api/cameras[/<id>]` | cameras |
| GET | `/api/cameras/<id>/frame` | one still image |
| GET | `/api/cameras/<id>/stream` | live multipart JPEG for an `<img>` |
| POST | `/api/scan` | scan the LAN for other IoT hosts |
| POST | `/api/refresh` | re-read every bridge now |

## Options

```
--host 0.0.0.0     interface to bind (default: everything)
--port 8712
--demo             add the simulated bridge
--token SECRET     require ?token=SECRET (stored in a cookie) on every request
--discover         print discovered bridges and exit
--scan             print every IoT host found on the LAN and exit
--verbose          log requests
```

## Security

This is built for a home LAN and behaves accordingly: no login by default, and
anyone who can reach the port can switch your lights. Two things are in place —
cross-origin writes are refused (so a web page you visit cannot drive your
lights through your browser), and `--token` adds a shared secret if you want
one. Do not port-forward it to the internet; use a VPN or your router's
Tailscale/WireGuard if you want it from outside.

The bridge serves HTTPS with a self-signed certificate whose subject is its own
bridge id, so certificate verification is impossible without pinning that
certificate. Connections to the bridge's local address therefore skip
verification — this is what every Hue client does, including Philips'. Nothing
else in the app skips verification.

## Tests

```bash
python3 -m unittest discover -s homeiot/tests -t .     # 75 unit tests, no network

python3 -m homeiot --demo &                            # end-to-end, needs Playwright
node homeiot/tests/e2e.mjs
```

None of the hardware for Sonos, Meross, Cast, Home Connect or the Internet-Box
is here, so each of those protocols is stood up as a small HTTP server that
answers the way the device does — the same SOAP envelopes, the same signed
JSON — and the client is driven against it over a real socket. That catches
what a mocked call would not: the headers, the encoding, the signature, and the
parsing of a genuine reply.

The end-to-end script drives a real browser with a **touchscreen and a tablet
viewport**, taps rather than clicks, and asserts that each interaction actually
changed the state on the server: switches, dimmers, colour, colour temperature,
effects, scenes, collections, Shelly switching, values pinned to a room, the
camera still and live view, and theme. It also measures every touch target,
checks that live updates patch the page instead of rebuilding it, and checks
that nothing scrolls sideways at phone, tablet-portrait and tablet-landscape
widths.

## Adding another integration

Every integration is a module registered in `integrations.py`, exposing:

```python
SOURCE                              # the namespace its ids live in
probe(ip)                           # a device description, or None
discover(deep)                      # what it can find on the network
snapshot(device)                    # whatever the device will tell us
home(device, raw)                   # normalised into the shared shape
send(device, rtype, rid, payload)   # optional: omit it and it is read-only
```

Leaving out `send` is how an integration says it is read-only, and the
dashboard takes it at its word: no switch is drawn, and a write is refused with
a reason. A test asserts that what each module advertises in `ACCESS` matches
what it actually implements, so the table above cannot drift from the truth.

Ids are `source:gateway:type:id`, so grouping, collections, pinned values, the
UI and the event plumbing come for free. Adding Sonos took no change to any of
them.
