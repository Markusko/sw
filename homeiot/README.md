# homeiot

A local dashboard for the IoT devices on your own network. It finds your
Philips Hue bridge, shows every device it can reach with its live state, and
lets you control the ones that can be controlled — lamps, plugs, rooms, zones,
scenes, and your own cross-cutting **Collections**.

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

**Plugs** — on/off.

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

**Settings** — pair and remove bridges, and scan the LAN for everything else
answering mDNS or SSDP: HomeKit accessories, Matter devices, Shelly and Tasmota
nodes, ESPHome, Cast targets, printers. They are listed, not controlled; Hue is
the integration that exists today.

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
| Detail panel | Bootstrap offcanvas |

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
├── demo.py          a simulated bridge with a full range of devices
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
python3 -m unittest discover -s homeiot/tests -t .     # 37 unit tests, no network

python3 -m homeiot --demo &                            # end-to-end, needs Playwright
node homeiot/tests/e2e.mjs
```

The end-to-end script drives a real browser with a **touchscreen and a tablet
viewport**, taps rather than clicks, and asserts that each interaction actually
changed the state on the server: switches, dimmers, colour, colour temperature,
effects, scenes, collections and theme. It also measures every touch target,
checks that live updates patch the page instead of rebuilding it, and checks
that nothing scrolls sideways at phone, tablet-portrait and tablet-landscape
widths.

## Adding another integration

`hue.py` and `demo.py` are interchangeable transports: both expose
`snapshot(bridge)` returning CLIP v2 shaped resources and `send(bridge, rtype,
rid, payload)`. `hub.transport()` picks between them. A new integration means a
third module of that shape plus a normaliser branch in `model.py` — the UI,
grouping, collections and event plumbing come for free.
