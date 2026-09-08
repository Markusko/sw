/* homeiot dashboard.
 *
 * Every control is a native HTML input styled by Bootstrap: a switch is a
 * checkbox, a dimmer is a range, a colour is a colour input.  Touch, keyboard
 * and accessibility are then the browser's job rather than ours, which is the
 * whole point -- the tablet gets the platform's own handling.
 *
 * State arrives as a snapshot over server-sent events.  Views are plain
 * template strings; morphdom applies them to the live page so nothing is torn
 * down under a finger.
 */

const App = {
  snapshot: null,
  view: localStorage.getItem('view') || 'rooms',
  theme: localStorage.getItem('theme') || 'auto',
  panel: null, // {kind: 'detail' | 'edit' | 'camera', id}
  discovered: null, // bridges offered during onboarding
  found: null, // whatever the last search in Settings turned up
  searching: false,
};

const VIEWS = [
  ['rooms', 'Rooms'],
  ['devices', 'Devices'],
  ['collections', 'Collections'],
  ['settings', 'Settings'],
];

const PRESETS = ['#ffffff', '#ffe9c4', '#ffb26b', '#ff8c42', '#ff5c5c', '#ff6bb5',
                 '#c86bff', '#6b7cff', '#4fc3ff', '#5ce1c4', '#8bff9d'];

// --- helpers -----------------------------------------------------------------

const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const byId = (list, id) => (list || []).find((item) => item.id === id);
const domId = (id) => `x${String(id).replace(/[^a-zA-Z0-9]+/g, '-')}`;

function since(stamp) {
  if (!stamp) return '';
  const seconds = (Date.now() - new Date(stamp).getTime()) / 1000;
  if (!isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

function throttle(fn, wait) {
  let last = 0;
  let timer = null;
  let queued = null;
  return (...args) => {
    queued = args;
    const remaining = wait - (Date.now() - last);
    const run = () => {
      timer = null;
      last = Date.now();
      const call = queued;
      queued = null;
      if (call) fn(...call);
    };
    if (remaining <= 0) run();
    else if (!timer) timer = setTimeout(run, remaining);
  };
}

// --- server ------------------------------------------------------------------

const token = new URLSearchParams(location.search).get('token');
if (token) document.cookie = `homeiot_token=${token}; path=/; max-age=31536000; SameSite=Strict`;

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

const fail = (error) => toast(error.message || String(error), true);
const command = (id, body) => api('PUT', `/api/targets/${encodeURIComponent(id)}/state`, body).catch(fail);
const commandSoon = throttle(command, 150);

function toast(message, bad) {
  $('#toasts').innerHTML = `
    <div class="toast show align-items-center border-0 text-bg-${bad ? 'danger' : 'dark'}" role="alert">
      <div class="d-flex">
        <div class="toast-body">${esc(message)}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>
    </div>`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($('#toasts').innerHTML = ''), bad ? 6000 : 2500);
}

// --- control fragments -------------------------------------------------------

const switchInput = (id, on, disabled) => `
  <div class="form-switch">
    <input class="form-check-input" type="checkbox" role="switch" data-act="toggle"
      data-id="${esc(id)}" ${on ? 'checked' : ''} ${disabled ? 'disabled' : ''}
      aria-label="On" />
  </div>`;

const rangeInput = ({ id, action, value, min, max, label, readout, disabled }) => `
  <label class="w-100 mb-0">
    <span class="d-flex justify-content-between small text-secondary">
      <span>${esc(label)}</span><span data-role="value">${esc(readout)}</span>
    </span>
    <input type="range" class="form-range" min="${min}" max="${max}" value="${value}"
      data-act="${action}" data-id="${esc(id)}" ${disabled ? 'disabled' : ''} aria-label="${esc(label)}" />
  </label>`;

const brightness = (target, state) => {
  if (state.brightness === null || state.brightness === undefined) return '';
  const value = Math.round(state.brightness);
  return rangeInput({
    id: target, action: 'brightness', value, min: 1, max: 100,
    label: 'Brightness', readout: `${value}%`, disabled: !state.on,
  });
};

const thermostatControls = (device) => {
  const state = device.state || {};
  const [low, high] = state.target_range || [5, 35];
  const target = state.target ?? low;
  return `
    <div class="d-flex align-items-baseline gap-2">
      <span class="fs-4 fw-semibold">${state.current === null || state.current === undefined
        ? '—'
        : `${state.current.toFixed(1)}°C`}</span>
      <span class="small text-secondary">now${state.heating ? ' · heating' : ''}</span>
    </div>
    ${rangeInput({
      id: device.id, action: 'target', value: target, min: low, max: high,
      label: 'Set to', readout: `${Number(target).toFixed(1)}°C`, disabled: !state.on,
    })}`;
};

const mediaControls = (device) => {
  const state = device.state || {};
  const track = state.track || {};
  const described = [track.artist, track.title].filter(Boolean).join(' — ');
  return `
    ${described ? `<div class="small text-truncate">${esc(described)}</div>` : ''}
    <div class="btn-group" role="group" aria-label="Playback">
      <button type="button" class="btn btn-outline-secondary" data-act="media" data-command="previous"
        data-id="${esc(device.id)}" aria-label="Previous">◀◀</button>
      <button type="button" class="btn btn-outline-secondary" data-act="media"
        data-command="${state.playing ? 'pause' : 'play'}" data-id="${esc(device.id)}"
        aria-label="${state.playing ? 'Pause' : 'Play'}">${state.playing ? '❙❙' : '▶'}</button>
      <button type="button" class="btn btn-outline-secondary" data-act="media" data-command="next"
        data-id="${esc(device.id)}" aria-label="Next">▶▶</button>
    </div>
    ${rangeInput({
      id: device.id, action: 'volume', value: state.volume ?? 0, min: 0, max: 100,
      label: 'Volume', readout: `${state.volume ?? 0}%`,
    })}`;
};

const openButton = (id, name) => `
  <button class="btn btn-link p-0 text-start text-body fw-semibold text-decoration-none text-truncate w-100"
    data-act="open" data-id="${esc(id)}">${esc(name)} <span class="text-secondary">›</span></button>`;

const sceneButtons = (scenes, ids) => {
  const chosen = (ids || []).map((id) => byId(scenes, id)).filter(Boolean);
  if (!chosen.length) return '';
  return `<div class="d-flex flex-wrap gap-2">${chosen
    .map(
      (scene) => `<button type="button" class="btn btn-sm btn-outline-secondary${scene.active ? ' active' : ''}"
        data-act="scene" data-id="${esc(scene.id)}">${esc(scene.name)}</button>`
    )
    .join('')}</div>`;
};

const tile = (key, inner) => `<div class="col"><div class="card" id="${domId(key)}">${inner}</div></div>`;
const grid = (cards) =>
  `<div class="row row-cols-1 row-cols-md-2 row-cols-xl-3 row-cols-xxl-4 g-3">${cards.join('')}</div>`;
const section = (title, inner, action = '') => `
  <section class="mb-4">
    <div class="d-flex align-items-center gap-3 mb-2">
      <h2 class="h6 text-secondary text-uppercase mb-0">${esc(title)}</h2>
      <hr class="flex-grow-1 my-0" />
      ${action}
    </div>
    ${inner}
  </section>`;

// --- cards -------------------------------------------------------------------

function deviceCard(device) {
  const state = device.state || {};
  const subtitle = device.reachable === false ? 'Unreachable' : device.room_name || device.product || '';
  // A value a control already shows is not repeated underneath it.
  const covered = device.capabilities.includes('target_temperature')
    ? ['temperature', 'target_temperature']
    : device.capabilities.includes('transport')
    ? ['now_playing']
    : [];
  const readings = (device.readings || [])
    .filter((reading) => !covered.includes(reading.kind))
    .map(
      (reading) => `<div class="d-flex justify-content-between small">
        <span class="text-secondary">${esc(reading.label)}</span><span>${esc(reading.display)}</span></div>`
    )
    .join('');
  const battery =
    device.battery && device.battery.level !== null && device.battery.level !== undefined
      ? `<span class="badge rounded-pill text-bg-${device.battery.level <= 15 ? 'danger' : 'secondary'}">${
          device.battery.level
        }%</span>`
      : '';

  return tile(
    device.id,
    `<div class="card-body d-flex flex-column gap-2">
      <div class="d-flex align-items-center gap-2">
        ${state.on && state.hex ? `<span class="dot" style="background:${esc(state.hex)}"></span>` : ''}
        <div class="min-w-0 flex-grow-1">
          ${openButton(device.id, device.name)}
          <div class="small text-secondary text-truncate">${esc(subtitle)}</div>
        </div>
        ${battery}
        ${device.controllable && device.capabilities.includes('transport')
          ? ''
          : device.controllable
          ? switchInput(device.id, state.on, device.reachable === false)
          : ''}
      </div>
      ${device.capabilities.includes('dimming') ? brightness(device.id, state) : ''}
      ${device.capabilities.includes('target_temperature') ? thermostatControls(device) : ''}
      ${device.capabilities.includes('transport') ? mediaControls(device) : ''}
      ${readings}
    </div>`
  );
}

const readoutStrip = (readouts) =>
  !readouts.length
    ? ''
    : `<div class="d-flex flex-wrap column-gap-3 row-gap-1 small readout">${readouts
        .map(
          (readout) => `<span><span class="text-secondary">${esc(readout.label)}</span>
            <span class="value fw-semibold">${esc(readout.display)}</span></span>`
        )
        .join('')}</div>`;

// The still refreshes on a ten-second grid: the src only changes when the
// bucket does, so the picture is never reloaded mid-render.
const cameraStill = (camera, classes = 'camera-thumb rounded') =>
  `<img class="${classes}" alt="${esc(camera.name)}" loading="lazy"
    src="/api/cameras/${encodeURIComponent(camera.id)}/frame?t=${Math.floor(Date.now() / 10000)}" />`;

function groupCard(group, scenes, readouts = [], cameras = []) {
  const state = group.state || {};
  return tile(
    group.id,
    `<div class="card-body d-flex flex-column gap-2">
      <div class="d-flex align-items-center gap-2">
        ${state.any_on && state.hex ? `<span class="dot" style="background:${esc(state.hex)}"></span>` : ''}
        <div class="min-w-0 flex-grow-1">
          ${openButton(group.id, group.name)}
          <div class="small text-secondary">${state.count ? `${state.on_count} of ${state.count} on` : 'No lights'}</div>
        </div>
        ${switchInput(group.id, state.any_on, !state.count)}
      </div>
      ${state.count ? brightness(group.id, { ...state, on: state.any_on }) : ''}
      ${readoutStrip(readouts)}
      ${cameras.length ? cameraStill(cameras[0]) : ''}
      ${sceneButtons(scenes, group.scenes)}
    </div>`
  );
}

function collectionCard(collection) {
  const state = collection.state || {};
  return tile(
    collection.id,
    `<div class="card-body d-flex flex-column gap-2">
      <div class="d-flex align-items-center gap-2">
        ${state.any_on && state.hex ? `<span class="dot" style="background:${esc(state.hex)}"></span>` : ''}
        <div class="min-w-0 flex-grow-1">
          ${openButton(collection.id, collection.name)}
          <div class="small text-secondary">${
            state.count ? `${state.on_count} of ${state.count} on` : `${collection.member_ids.length} members`
          }</div>
        </div>
        ${switchInput(collection.id, state.any_on, !state.count)}
      </div>
      ${state.count ? brightness(collection.id, { ...state, on: state.any_on }) : ''}
    </div>`
  );
}

// --- views -------------------------------------------------------------------

const empty = (title, body, action = '') => `
  <div class="text-center text-secondary border rounded-3 py-5 px-3">
    <p class="h5 text-body">${esc(title)}</p>
    <p class="mx-auto" style="max-width: 46ch">${esc(body)}</p>
    ${action}
  </div>`;

const inRoom = (list, roomId) => (list || []).filter((item) => item.room === roomId);

function viewRooms(snapshot) {
  const rooms = snapshot.groups.filter((group) => group.kind === 'room');
  const zones = snapshot.groups.filter((group) => group.kind === 'zone');
  const loose = snapshot.devices.filter((device) => !device.room && device.kind !== 'bridge');
  const card = (group) =>
    groupCard(group, snapshot.scenes, inRoom(snapshot.readouts, group.id), inRoom(snapshot.cameras, group.id));
  return [
    rooms.length ? section('Rooms', grid(rooms.map(card))) : '',
    zones.length ? section('Zones', grid(zones.map(card))) : '',
    loose.length ? section('Not in a room', grid(loose.map(deviceCard))) : '',
  ].join('') || empty('Nothing to show', 'No rooms are set up on this bridge.');
}

function viewDevices(snapshot) {
  const kinds = [
    ['Lights', 'light'],
    ['Plugs', 'plug'],
    ['Relays', 'relay'],
    ['Heating', 'thermostat'],
    ['Media', 'media'],
    ['Appliances', 'appliance'],
    ['Sensors', 'sensor'],
    ['Switches', 'switch'],
    ['Network', 'router'],
    ['Other', 'other'],
  ];
  return (
    kinds
      .map(([title, kind]) => {
        const devices = snapshot.devices.filter((device) => device.kind === kind);
        return devices.length ? section(`${title} · ${devices.length}`, grid(devices.map(deviceCard))) : '';
      })
      .join('') || empty('No devices', 'Nothing is paired yet.')
  );
}

function viewCollections(snapshot) {
  const action = `<button class="btn btn-sm btn-primary" data-act="new-collection">New</button>`;
  if (!snapshot.collections.length) {
    return section(
      'Collections',
      empty(
        'Group things your own way',
        'A collection cuts across rooms: "Evening", "Desk", "Away lights". Pick any devices, rooms or zones and control them together.',
        `<button class="btn btn-primary" data-act="new-collection">Create a collection</button>`
      ),
      action
    );
  }
  return section('Collections', grid(snapshot.collections.map(collectionCard)), action);
}

const REACH = {
  full: ['success', 'controllable'],
  read: ['secondary', 'read-only'],
  identify: ['secondary', 'listed only'],
};

const sourceLabel = (snapshot, source) =>
  ((snapshot.sources || []).find((item) => item.source === source) || {}).label || source;

function viewSettings(snapshot) {
  const bridges = snapshot.bridges
    .map(
      (bridge) => `<li class="list-group-item d-flex align-items-center gap-3">
        <div class="min-w-0 flex-grow-1">
          <div class="fw-semibold text-truncate">${esc(bridge.name)}
            <span class="badge text-bg-light">${esc(sourceLabel(snapshot, bridge.source))}</span>${
        bridge.demo ? ' <span class="badge text-bg-secondary">demo</span>' : ''
      }</div>
          <div class="small text-secondary text-truncate">${esc(bridge.ip)} · ${esc(
        bridge.connected ? (bridge.stream === 'connected' ? 'live updates' : 'polling') : bridge.error || 'offline'
      )}</div>
        </div>
        <button class="btn btn-sm btn-outline-danger" data-act="forget-bridge" data-id="${esc(bridge.id)}">Remove</button>
      </li>`
    )
    .join('');

  const found = (App.found || [])
    .map(
      (item) => `<li class="list-group-item d-flex align-items-center gap-3">
        <div class="min-w-0 flex-grow-1">
          <div class="fw-semibold text-truncate">${esc(item.name)}
            <span class="badge text-bg-light">${esc(sourceLabel(snapshot, item.source))}</span></div>
          <div class="small text-secondary">${esc(item.ip)} · ${esc(item.model || '')}</div>
        </div>
        <button class="btn btn-sm btn-primary" data-act="${item.source === 'hue' ? 'pair' : 'adopt'}"
          data-ip="${esc(item.ip)}" data-source="${esc(item.source)}" ${item.paired ? 'disabled' : ''}>${
        item.paired ? 'Added' : item.source === 'hue' ? 'Pair' : 'Add'
      }</button>
      </li>`
    )
    .join('');

  const cameras = (snapshot.cameras || [])
    .map((camera) => {
      const room = byId(snapshot.groups, camera.room);
      const trouble = camera.mode === 'needs_ffmpeg' || camera.mode === 'unconfigured';
      return `<li class="list-group-item d-flex align-items-center gap-3">
        <div class="min-w-0 flex-grow-1">
          <div class="fw-semibold text-truncate">${esc(camera.name)}
            <span class="badge text-bg-${trouble ? 'warning' : 'light'}">${esc(camera.mode.replace(/_/g, ' '))}</span></div>
          <div class="small text-secondary text-truncate">${esc(camera.url || camera.snapshot_url || 'no address')}${
        room ? ` · ${esc(room.name)}` : ''
      }</div>
        </div>
        <button class="btn btn-sm btn-outline-secondary" data-act="edit-camera" data-id="${esc(camera.id)}">Edit</button>
        <button class="btn btn-sm btn-outline-danger" data-act="delete-camera" data-id="${esc(camera.id)}">Remove</button>
      </li>`;
    })
    .join('');

  const network = snapshot.network || {};
  const hosts = (network.hosts || [])
    .map(
      (host) => `<li class="list-group-item d-flex gap-3 small">
        <span class="font-monospace text-secondary" style="width:9rem">${esc(host.ip)}</span>
        <span class="min-w-0 flex-grow-1 text-truncate">${esc(host.hostname || '—')}</span>
        <span class="text-secondary text-truncate">${esc((host.labels || []).slice(0, 2).join(' · '))}</span>
      </li>`
    )
    .join('');

  return [
    section(
      'Bridges and devices',
      `<div class="card"><ul class="list-group list-group-flush">${
        bridges || '<li class="list-group-item text-secondary">Nothing paired yet.</li>'
      }</ul>
        <div class="card-body">
          ${(snapshot.sources || [])
            .map((item) => {
              const [tone, reach] = REACH[item.control] || REACH.read;
              return `<div class="d-flex align-items-start gap-3 py-2 border-bottom" data-source-row="${esc(
                item.source
              )}">
                <div class="min-w-0 flex-grow-1">
                  <div class="fw-semibold">${esc(item.label)}
                    <span class="badge text-bg-${tone}">${esc(reach)}</span>${
                item.needs ? ` <span class="badge text-bg-warning">needs a ${esc(item.needs)}</span>` : ''
              }</div>
                  <div class="small text-secondary">${esc(item.note)}</div>
                </div>
                <button class="btn btn-sm btn-outline-primary" data-act="find" data-source="${esc(item.source)}"
                  ${App.searching ? 'disabled' : ''}>${
                App.searching === item.source ? 'Searching…' : 'Search'
              }</button>
              </div>`;
            })
            .join('')}
          ${found ? `<ul class="list-group mt-3">${found}</ul>` : ''}
          <h3 class="h6 text-secondary text-uppercase mt-4">Add by address</h3>
          <div class="row g-2">
            <div class="col-12 col-md"><select class="form-select" id="manual-source" aria-label="Kind of device">
              ${(snapshot.sources || [])
                .map(
                  (item, index) => `<option value="${esc(item.source)}" ${index === 0 ? 'selected' : ''}>${esc(
                    item.label
                  )}</option>`
                )
                .join('')}
            </select></div>
            <div class="col-12 col-md"><input type="text" class="form-control" id="manual-ip"
              placeholder="192.168.1.42" inputmode="decimal" /></div>
            <div class="col-12 col-md"><input type="text" class="form-control" id="manual-secret"
              placeholder="Device key or password, if it needs one" autocomplete="off" /></div>
            <div class="col-auto"><button class="btn btn-primary" data-act="add-by-address">Add</button></div>
          </div>
          <p class="small text-secondary mt-2 mb-0">A Hue bridge needs its link button pressed within 30
            seconds. A Meross needs the device key from your account; the Internet-Box needs its password.</p>
          <div class="mt-3"><button class="btn btn-outline-secondary" data-act="add-demo">Add demo bridge</button></div>
        </div></div>`
    ),
    section(
      'Cameras',
      `<div class="card"><ul class="list-group list-group-flush">${
        cameras || '<li class="list-group-item text-secondary">No cameras yet.</li>'
      }</ul>
        <div class="card-body">
          <button class="btn btn-primary" data-act="new-camera">Add a camera</button>
          ${snapshot.ffmpeg
            ? '<p class="small text-secondary mt-2 mb-0">ffmpeg was found, so RTSP streams can be shown.</p>'
            : `<p class="small text-secondary mt-2 mb-0">ffmpeg was not found, so RTSP cannot be converted for
                the browser. Install it, or give the camera its snapshot URL instead.</p>`}
        </div></div>`
    ),
    section(
      'Other devices on the network',
      `<div class="card"><div class="card-body">
        <p class="text-secondary mb-3">Found over mDNS and SSDP. Visible, but not controllable here — Hue is
          the integration that exists today.</p>
        <button class="btn btn-outline-secondary" data-act="scan" ${network.scanning ? 'disabled' : ''}>${
        network.scanning ? 'Scanning…' : 'Scan the network'
      }</button>
      </div>
      ${hosts ? `<ul class="list-group list-group-flush">${hosts}</ul>` : ''}</div>`
    ),
  ].join('');
}

function viewSetup() {
  const found = App.discovered;
  const list = !found
    ? ''
    : found.length
    ? `<ul class="list-group list-group-flush mb-3">${found
        .map(
          (bridge) => `<li class="list-group-item d-flex align-items-center gap-3">
            <div class="min-w-0 flex-grow-1">
              <div class="fw-semibold text-truncate">${esc(bridge.name)}</div>
              <div class="small text-secondary">${esc(bridge.ip)} · API ${esc(bridge.api_version)}</div>
            </div>
            <button class="btn btn-primary" data-act="pair" data-ip="${esc(bridge.ip)}" ${
            bridge.paired ? 'disabled' : ''
          }>${bridge.paired ? 'Paired' : 'Pair'}</button>
          </li>`
        )
        .join('')}</ul>`
    : `<p class="text-secondary">No bridge answered. Check that it is powered and on this network, or enter its
        address below.</p>`;

  return `<div class="mx-auto" style="max-width: 34rem">
    <div class="card"><div class="card-body">
      <h2 class="h4">Connect your Hue bridge</h2>
      <ol class="text-secondary">
        <li>Make sure the bridge is on this network.</li>
        <li>Press <strong>Search</strong>.</li>
        <li>Press the round <strong>link button</strong> on the bridge, then <strong>Pair</strong> within 30 seconds.</li>
      </ol>
      <div class="d-flex flex-wrap gap-2 mb-3">
        <button class="btn btn-primary" data-act="discover" ${App.searching ? 'disabled' : ''}>${
    App.searching ? 'Searching…' : 'Search for bridges'
  }</button>
        <button class="btn btn-outline-secondary" data-act="add-demo">Try the demo</button>
      </div>
      ${list}
      <label class="form-label" for="manual-ip">Or enter an address</label>
      <div class="input-group">
        <input type="text" class="form-control" id="manual-ip" placeholder="192.168.1.42" inputmode="decimal" />
        <button class="btn btn-outline-secondary" data-act="pair-manual">Pair</button>
      </div>
    </div></div>
  </div>`;
}

// --- detail panel ------------------------------------------------------------

function colourControls(id, state, capabilities) {
  const blocks = [];
  if (capabilities.includes('color')) {
    blocks.push(`<h3 class="h6 text-secondary text-uppercase mt-4">Colour</h3>
      <div class="d-flex flex-wrap gap-2">
        ${PRESETS.map(
          (colour) => `<button type="button" class="btn border p-0" style="background:${colour};width:2.75rem;height:2.75rem"
            data-act="colour" data-colour="${colour}" data-id="${esc(id)}" aria-label="Set ${colour}"></button>`
        ).join('')}
        <input type="color" class="form-control form-control-color p-1" style="width:2.75rem;height:2.75rem"
          value="${esc(state.hex || '#ffffff')}" data-act="colour-pick" data-id="${esc(id)}" aria-label="Pick a colour" />
      </div>`);
  }
  if (capabilities.includes('color_temp') && state.mirek_range) {
    const mirek = state.mirek || 300;
    blocks.push(`<h3 class="h6 text-secondary text-uppercase mt-4">White</h3>
      ${rangeInput({
        id, action: 'mirek', value: mirek,
        min: state.mirek_range[0], max: state.mirek_range[1],
        label: 'Temperature', readout: `${Math.round(1000000 / mirek)} K`,
      })}`);
  }
  if (capabilities.includes('effects') && (state.effects || []).length > 1) {
    blocks.push(`<h3 class="h6 text-secondary text-uppercase mt-4">Effect</h3>
      <select class="form-select" data-act="effect" data-id="${esc(id)}" aria-label="Effect">
        ${state.effects
          .map(
            (effect) => `<option value="${esc(effect)}" ${effect === state.effect ? 'selected' : ''}>${esc(
              effect.replace(/_/g, ' ')
            )}</option>`
          )
          .join('')}
      </select>`);
  }
  return blocks.join('');
}

const detailRow = (key, value) => `
  <div class="d-flex justify-content-between gap-3 py-2 border-bottom">
    <span class="text-secondary">${esc(key)}</span><span class="text-end">${esc(value)}</span>
  </div>`;

function panelDevice(device) {
  const state = device.state || {};
  return `
    ${device.controllable
      ? `<div class="d-flex align-items-center gap-3 mb-3">
          <span class="flex-grow-1">${esc(device.product || device.kind)}</span>
          ${device.capabilities.includes('transport')
            ? ''
            : switchInput(device.id, state.on, device.reachable === false)}
        </div>
        ${brightness(device.id, state)}
        ${device.capabilities.includes('target_temperature') ? thermostatControls(device) : ''}
        ${device.capabilities.includes('transport') ? mediaControls(device) : ''}
        ${colourControls(device.id, state, device.capabilities)}`
      : ''}
    ${device.note ? `<div class="alert alert-secondary small">${esc(device.note)}</div>` : ''}
    ${(device.lights || []).length > 1
      ? `<h3 class="h6 text-secondary text-uppercase mt-4">Lights in this device</h3>${device.lights
          .map(
            (light) => `<div class="d-flex align-items-center gap-3 py-2 border-bottom">
              <span class="flex-grow-1 text-truncate">${esc(light.name)}</span>
              ${switchInput(light.id, light.state.on)}
            </div>`
          )
          .join('')}`
      : ''}
    ${(device.readings || []).length
      ? `<h3 class="h6 text-secondary text-uppercase mt-4">Readings</h3>${device.readings
          .map((reading) => detailRow(reading.label, `${reading.display}${reading.updated ? ` · ${since(reading.updated)}` : ''}`))
          .join('')}`
      : ''}
    ${(device.buttons || []).length
      ? `<h3 class="h6 text-secondary text-uppercase mt-4">Buttons</h3>${device.buttons
          .map((button) => detailRow(`Button ${button.control_id ?? ''}`, `${button.display} · ${since(button.updated)}`))
          .join('')}`
      : ''}
    <h3 class="h6 text-secondary text-uppercase mt-4">Details</h3>
    ${detailRow('Room', device.room_name || '—')}
    ${detailRow('Model', device.model || '—')}
    ${detailRow('Made by', device.manufacturer || '—')}
    ${detailRow('Firmware', device.software || '—')}
    ${detailRow('Zigbee', device.reachable === null ? '—' : device.reachable ? 'Connected' : 'Unreachable')}
    ${device.battery ? detailRow('Battery', `${device.battery.level}% · ${device.battery.state || ''}`) : ''}
    <div class="d-flex flex-wrap gap-2 mt-4">
      <button class="btn btn-outline-secondary" data-act="identify" data-id="${esc(device.id)}">Identify</button>
      <button class="btn btn-outline-secondary" data-act="rename" data-id="${esc(device.id)}">Rename</button>
    </div>`;
}

// Every value any device reports, as one flat list to choose from.
const readableValues = (snapshot) =>
  snapshot.devices.flatMap((device) =>
    (device.readings || []).map((reading) => ({
      value: `${device.id}|${reading.kind}`,
      text: `${device.name} — ${reading.label} (${reading.display})`,
      label: reading.label,
    }))
  );

// The first option carries `selected`: without it morphdom syncs the select
// to selectedIndex -1 and the control renders empty.
function roomValues(group, snapshot) {
  const mine = inRoom(snapshot.readouts, group.id);
  const options = readableValues(snapshot);
  return `
    <h3 class="h6 text-secondary text-uppercase mt-4">Values</h3>
    ${mine
      .map(
        (readout) => `<div class="d-flex align-items-center gap-3 py-2 border-bottom">
          <span class="flex-grow-1 text-truncate">${esc(readout.label)}
            <span class="text-secondary small">${esc(readout.device_name || 'device is gone')}</span></span>
          <span class="fw-semibold">${esc(readout.display)}</span>
          <button class="btn btn-sm btn-outline-danger" data-act="delete-readout"
            data-id="${esc(readout.id)}">Remove</button>
        </div>`
      )
      .join('')}
    ${options.length
      ? `<div class="row g-2 mt-2" data-static>
          <div class="col-12"><select class="form-select" id="value-source" aria-label="Value to show">
            ${options
              .map(
                (option, index) => `<option value="${esc(option.value)}" data-label="${esc(option.label)}" ${
                  index === 0 ? 'selected' : ''
                }>${esc(option.text)}</option>`
              )
              .join('')}
          </select></div>
          <div class="col"><input type="text" class="form-control" id="value-label" placeholder="Label, e.g. Temperature" /></div>
          <div class="col-auto"><button class="btn btn-primary" data-act="add-readout"
            data-id="${esc(group.id)}">Add value</button></div>
        </div>`
      : '<p class="text-secondary small mb-0">No device is reporting a value yet.</p>'}`;
}

function roomCameras(group, snapshot) {
  const cameras = inRoom(snapshot.cameras, group.id);
  if (!cameras.length) return '';
  return `<h3 class="h6 text-secondary text-uppercase mt-4">Cameras</h3>
    ${cameras
      .map((camera) =>
        camera.mode === 'needs_ffmpeg' || camera.mode === 'unconfigured'
          ? `<div class="alert alert-warning py-2 small">${esc(camera.name)}: ${
              camera.mode === 'needs_ffmpeg'
                ? 'ffmpeg is not installed, so this RTSP stream cannot be shown here.'
                : 'no RTSP or snapshot address is set.'
            }</div>`
          : `<figure class="mb-3">
              <img class="camera-frame rounded" alt="${esc(camera.name)}"
                src="/api/cameras/${encodeURIComponent(camera.id)}/stream" />
              <figcaption class="small text-secondary mt-1">${esc(camera.name)} · live</figcaption>
            </figure>`
      )
      .join('')}`;
}

function panelGroup(group, snapshot) {
  const state = group.state || {};
  const members = group.device_ids.map((id) => byId(snapshot.devices, id)).filter(Boolean);
  return `
    <div class="d-flex align-items-center gap-3 mb-3">
      <span class="flex-grow-1">${state.on_count || 0} of ${state.count || 0} on</span>
      ${switchInput(group.id, state.any_on, !state.count)}
    </div>
    ${state.count ? brightness(group.id, { ...state, on: state.any_on }) : ''}
    ${colourControls(group.id, state, group.capabilities || [])}
    ${group.scenes.length
      ? `<h3 class="h6 text-secondary text-uppercase mt-4">Scenes</h3>${sceneButtons(snapshot.scenes, group.scenes)}`
      : ''}
    ${roomCameras(group, snapshot)}
    ${roomValues(group, snapshot)}
    <h3 class="h6 text-secondary text-uppercase mt-4">Devices</h3>
    ${members
      .map(
        (device) => `<div class="d-flex align-items-center gap-3 py-2 border-bottom">
          <span class="flex-grow-1 text-truncate">${esc(device.name)}</span>
          ${device.controllable
            ? switchInput(device.id, device.state.on)
            : `<span class="small text-secondary">${esc(
                (device.readings || []).map((reading) => reading.display).join(' · ') || '—'
              )}</span>`}
        </div>`
      )
      .join('')}
    <div class="d-flex gap-2 mt-4">
      <button class="btn btn-outline-secondary" data-act="rename" data-id="${esc(group.id)}">Rename</button>
    </div>`;
}

function panelCollection(collection, snapshot) {
  const state = collection.state || {};
  const members = collection.member_ids
    .map((id) => byId(snapshot.devices, id) || byId(snapshot.groups, id))
    .filter(Boolean);
  return `
    <div class="d-flex align-items-center gap-3 mb-3">
      <span class="flex-grow-1">${members.length} members</span>
      ${switchInput(collection.id, state.any_on, !state.count)}
    </div>
    ${state.count ? brightness(collection.id, { ...state, on: state.any_on }) : ''}
    ${colourControls(collection.id, state, collection.capabilities || [])}
    <h3 class="h6 text-secondary text-uppercase mt-4">Members</h3>
    ${members
      .map(
        (member) => `<div class="d-flex align-items-center gap-3 py-2 border-bottom">
          <span class="flex-grow-1 text-truncate">${esc(member.name)}</span>
          ${switchInput(member.id, member.state.any_on ?? member.state.on)}
        </div>`
      )
      .join('')}
    <div class="d-flex flex-wrap gap-2 mt-4">
      <button class="btn btn-outline-secondary" data-act="edit-collection" data-id="${esc(collection.id)}">Edit</button>
      <button class="btn btn-outline-danger" data-act="delete-collection" data-id="${esc(collection.id)}">Delete</button>
    </div>`;
}

function panelCamera(snapshot, camera) {
  const rooms = snapshot.groups.filter((group) => group.kind === 'room' || group.kind === 'zone');
  return `
    <label class="form-label" for="camera-name">Name</label>
    <input type="text" class="form-control mb-3" id="camera-name" placeholder="Front door"
      value="${esc(camera ? camera.name : '')}" />

    <label class="form-label" for="camera-rtsp">RTSP address</label>
    <input type="text" class="form-control" id="camera-rtsp" spellcheck="false" autocapitalize="off"
      placeholder="rtsp://user:password@192.168.1.50:554/stream1" value="${esc(camera ? camera.url : '')}" />
    <p class="form-text">${
      snapshot.ffmpeg
        ? 'Converted to a picture the browser can show, by ffmpeg on this machine.'
        : 'ffmpeg was not found on this machine, so an RTSP address cannot be shown until it is installed.'
    }</p>

    <label class="form-label" for="camera-snapshot">Snapshot address (optional)</label>
    <input type="text" class="form-control" id="camera-snapshot" spellcheck="false" autocapitalize="off"
      placeholder="http://192.168.1.50/snapshot.jpg" value="${esc(camera ? camera.snapshot_url : '')}" />
    <p class="form-text">Most cameras also serve a still image. It needs no ffmpeg and updates once a second.</p>

    <label class="form-label" for="camera-room">Room</label>
    <select class="form-select mb-4" id="camera-room">
      <option value="">Not in a room</option>
      ${rooms
        .map(
          (room) => `<option value="${esc(room.id)}" ${
            camera && camera.room === room.id ? 'selected' : ''
          }>${esc(room.name)}</option>`
        )
        .join('')}
    </select>

    <div class="d-flex gap-2">
      <button class="btn btn-primary" data-act="save-camera" data-id="${esc(camera ? camera.id : '')}">Save</button>
      <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    </div>`;
}

function panelEditor(snapshot, collection) {
  const chosen = new Set(collection ? collection.members : []);
  const candidates = [
    ...snapshot.groups.map((group) => ({ ...group, note: group.kind === 'room' ? 'Room' : 'Zone' })),
    ...snapshot.devices
      .filter((device) => device.kind !== 'bridge')
      .map((device) => ({ ...device, note: device.room_name || device.kind })),
  ];
  return `
    <label class="form-label" for="collection-name">Name</label>
    <input type="text" class="form-control mb-4" id="collection-name" placeholder="Evening, Desk, Away lights…"
      value="${esc(collection ? collection.name : '')}" />
    <h3 class="h6 text-secondary text-uppercase">Members</h3>
    <div class="picker border rounded-3 px-2 mb-4">
      ${candidates
        .map(
          (item, index) => `<div class="form-check">
            <input class="form-check-input m-0" type="checkbox" value="${esc(item.id)}" id="pick-${index}" ${
            chosen.has(item.id) ? 'checked' : ''
          } />
            <label class="form-check-label flex-grow-1 min-w-0 text-truncate" for="pick-${index}">${esc(item.name)}</label>
            <span class="small text-secondary">${esc(item.note)}</span>
          </div>`
        )
        .join('')}
    </div>
    <div class="d-flex gap-2">
      <button class="btn btn-primary" data-act="save-collection" data-id="${esc(collection ? collection.id : '')}">Save</button>
      <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    </div>`;
}

// --- painting ----------------------------------------------------------------

// A control being held must never be replaced under the finger; everything
// else is patched in place so hover, focus and scroll survive an update.
let holding = null;

function paint(root, html) {
  const next = root.cloneNode(false);
  next.innerHTML = html;
  morphdom(root, next, {
    childrenOnly: true,
    onBeforeElUpdated: (from, to) => {
      // Only controls that hold a value are protected from being updated: a
      // button keeps focus after a tap, and must still be allowed to change
      // (play becomes pause the moment the speaker does).
      const holdsValue = ['INPUT', 'SELECT', 'TEXTAREA'].includes(from.tagName);
      if (holdsValue && (from === holding || from === document.activeElement)) return false;
      // A form the user is part-way through filling in is theirs, not the
      // server's: leave it, and its children, exactly as they left it.
      if (from.dataset && from.dataset.static !== undefined) return false;
      return !from.isEqualNode(to);
    },
  });
}

function render() {
  const snapshot = App.snapshot;
  applyTheme();
  paint(
    $('#tabs'),
    VIEWS.map(
      ([key, label]) => `<li class="nav-item"><button class="nav-link${
        App.view === key ? ' active' : ''
      }" data-act="view" data-view="${key}">${label}</button></li>`
    ).join('')
  );

  if (!snapshot) return;
  showStatus(snapshot);

  const onboarding = !snapshot.bridges.length;
  $('#tabs').parentElement.hidden = onboarding;
  if (onboarding) {
    paint($('#main'), viewSetup());
    return;
  }
  const views = {
    rooms: viewRooms,
    devices: viewDevices,
    collections: viewCollections,
    settings: viewSettings,
  };
  paint($('#main'), (views[App.view] || viewRooms)(snapshot));
  renderPanel();
}

function renderPanel() {
  const { snapshot, panel } = App;
  if (!panel || !snapshot) return;
  const body = $('#panel-body');
  // The editor is a form, not a view of live data: once open it is the user's.
  if (panel.kind === 'edit' || panel.kind === 'camera') {
    const key = `${panel.kind}:${panel.id || 'new'}`; // '' would collide with "nothing rendered"
    if (body.dataset.editor === key) return;
    if (panel.kind === 'camera') {
      const camera = panel.id ? byId(snapshot.cameras, panel.id) : null;
      $('#panel-title').textContent = camera ? 'Edit camera' : 'Add a camera';
      body.innerHTML = panelCamera(snapshot, camera);
    } else {
      const collection = panel.id ? byId(snapshot.collections, panel.id) : null;
      $('#panel-title').textContent = collection ? 'Edit collection' : 'New collection';
      body.innerHTML = panelEditor(snapshot, collection);
    }
    body.dataset.editor = key;
    return;
  }
  body.dataset.editor = '';
  const device = byId(snapshot.devices, panel.id);
  const group = byId(snapshot.groups, panel.id);
  const collection = byId(snapshot.collections, panel.id);
  const subject = device || group || collection;
  if (!subject) return;
  $('#panel-title').textContent = subject.name;
  paint(
    body,
    device ? panelDevice(device) : group ? panelGroup(group, snapshot) : panelCollection(collection, snapshot)
  );
}

function showStatus(snapshot) {
  const bridges = snapshot.bridges || [];
  const broken = bridges.filter((bridge) => !bridge.connected);
  const streaming = bridges.some((bridge) => bridge.stream === 'connected' || bridge.demo);
  const label = !bridges.length ? 'no bridge' : broken.length ? `${broken.length} offline` : streaming ? 'live' : 'polling';
  const tone = broken.length ? 'danger' : bridges.length ? 'success' : 'secondary';
  const badge = $('#status');
  badge.textContent = label;
  badge.className = `badge rounded-pill text-bg-${tone}`;
  badge.title = broken.map((bridge) => `${bridge.name}: ${bridge.error || 'offline'}`).join('\n');
}

function applyTheme() {
  const dark = App.theme === 'dark' || (App.theme === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.bsTheme = dark ? 'dark' : 'light';
  document.documentElement.dataset.themeMode = App.theme;
  $('#theme').textContent = { auto: 'Auto', light: 'Light', dark: 'Dark' }[App.theme];
}

// --- panel plumbing ----------------------------------------------------------

const panelElement = () => bootstrap.Modal.getOrCreateInstance($('#panel'));

function openPanel(kind, id) {
  App.panel = { kind, id };
  $('#panel-body').dataset.editor = '';
  $('#panel-body').innerHTML = '';
  renderPanel();
  panelElement().show();
}

$('#panel').addEventListener('hidden.bs.modal', () => {
  App.panel = null;
  $('#panel-body').dataset.editor = '';
  // Emptying it closes any live camera connection the modal was holding open.
  $('#panel-body').innerHTML = '';
});

// --- actions -----------------------------------------------------------------

const findAny = (id) =>
  byId(App.snapshot.devices, id) || byId(App.snapshot.groups, id) || byId(App.snapshot.collections, id);

const actions = {
  view: (element) => {
    App.view = element.dataset.view;
    localStorage.setItem('view', App.view);
    render();
  },
  open: (element) => openPanel('detail', element.dataset.id),
  scene: (element) => api('POST', `/api/scenes/${encodeURIComponent(element.dataset.id)}/recall`, {}).catch(fail),
  media: (element) => command(element.dataset.id, { [element.dataset.command]: true }),
  colour: (element) => command(element.dataset.id, { on: true, hex: element.dataset.colour }),
  identify: (element) =>
    api('POST', `/api/devices/${encodeURIComponent(element.dataset.id)}/identify`, {})
      .then(() => toast('Blinking'))
      .catch(fail),
  rename: (element) => {
    const subject = findAny(element.dataset.id);
    const name = prompt('New name', subject ? subject.name : '');
    if (name && name.trim()) {
      api('PUT', `/api/targets/${encodeURIComponent(element.dataset.id)}/name`, { name: name.trim() })
        .then(() => toast('Renamed'))
        .catch(fail);
    }
  },
  'new-collection': () => openPanel('edit', ''),
  'edit-collection': (element) => openPanel('edit', element.dataset.id),
  'save-collection': (element) => {
    const name = $('#collection-name').value.trim() || 'Untitled';
    const members = [...document.querySelectorAll('.picker input:checked')].map((input) => input.value);
    const id = element.dataset.id;
    const request = id
      ? api('PUT', `/api/collections/${encodeURIComponent(id)}`, { name, members })
      : api('POST', '/api/collections', { name, members });
    request
      .then(() => {
        panelElement().hide();
        toast('Saved');
        return refresh();
      })
      .catch(fail);
  },
  'delete-collection': (element) => {
    if (!confirm('Delete this collection? The devices themselves are untouched.')) return;
    api('DELETE', `/api/collections/${encodeURIComponent(element.dataset.id)}`)
      .then(() => {
        panelElement().hide();
        return refresh();
      })
      .catch(fail);
  },
  discover: () => actions.find({ dataset: { source: 'hue' } }),
  find: (element) => {
    const source = element.dataset.source || 'hue';
    const secret = source === 'meross' ? ($('#manual-secret')?.value || '').trim() : '';
    if (source === 'meross' && !secret) {
      return toast('Meross devices are only reachable with your device key — put it in the field below', true);
    }
    App.searching = source;
    render();
    api('POST', '/api/discover', { source, deep: true, key: secret })
      .then((result) => {
        App.found = result.bridges;
        if (source === 'hue') App.discovered = result.bridges;
        if (!result.bridges.length) {
          toast(`Nothing answered as ${sourceLabel(App.snapshot || {}, source)} on this network`, true);
        }
      })
      .catch(fail)
      .finally(() => {
        App.searching = false;
        render();
      });
  },
  adopt: (element) => adopt(element.dataset.source, element.dataset.ip, ''),
  'add-by-address': () => {
    const source = $('#manual-source').value;
    const ip = $('#manual-ip').value.trim();
    const secret = $('#manual-secret').value.trim();
    if (!ip) return toast('An address is needed', true);
    if (source === 'hue') return pair(ip);
    adopt(source, ip, secret);
  },
  'add-readout': (element) => {
    const picker = $('#value-source');
    const [device, kind] = picker.value.split('|');
    const chosen = picker.selectedOptions[0];
    const label = $('#value-label').value.trim() || (chosen ? chosen.dataset.label : '') || kind;
    api('POST', '/api/readouts', { device, kind, label, room: element.dataset.id })
      .then(() => {
        toast('Value added');
        return refresh();
      })
      .catch(fail);
  },
  'delete-readout': (element) =>
    api('DELETE', `/api/readouts/${encodeURIComponent(element.dataset.id)}`).then(refresh).catch(fail),
  'new-camera': () => openPanel('camera', ''),
  'edit-camera': (element) => openPanel('camera', element.dataset.id),
  'save-camera': (element) => {
    const body = {
      name: $('#camera-name').value.trim(),
      rtsp_url: $('#camera-rtsp').value.trim(),
      snapshot_url: $('#camera-snapshot').value.trim(),
      room: $('#camera-room').value || null,
    };
    const id = element.dataset.id;
    // A masked URL left untouched must not be written back over the real one.
    if (id && /:\*\*\*@/.test(body.rtsp_url)) delete body.rtsp_url;
    if (id && /:\*\*\*@/.test(body.snapshot_url)) delete body.snapshot_url;
    const request = id
      ? api('PUT', `/api/cameras/${encodeURIComponent(id)}`, body)
      : api('POST', '/api/cameras', body);
    request
      .then(() => {
        panelElement().hide();
        toast('Camera saved');
        return refresh();
      })
      .catch(fail);
  },
  'delete-camera': (element) => {
    if (!confirm('Remove this camera?')) return;
    api('DELETE', `/api/cameras/${encodeURIComponent(element.dataset.id)}`).then(refresh).catch(fail);
  },
  pair: (element) => pair(element.dataset.ip),
  'pair-manual': () => {
    const value = $('#manual-ip').value.trim();
    if (value) pair(value);
  },
  'add-bridge': () => {
    App.snapshot = { ...App.snapshot, bridges: [] };
    render();
  },
  'add-demo': () =>
    api('POST', '/api/demo', {})
      .then(refresh)
      .then(() => toast('Demo bridge added'))
      .catch(fail),
  'forget-bridge': (element) => {
    if (!confirm('Remove this bridge? Its devices disappear from the dashboard.')) return;
    api('DELETE', `/api/bridges/${encodeURIComponent(element.dataset.id)}`).then(refresh).catch(fail);
  },
  scan: () => api('POST', '/api/scan', {}).then(() => toast('Scanning…')).catch(fail),
};

function adopt(source, ip, secret) {
  api('POST', '/api/adopt', { source, ip, key: secret, password: secret })
    .then((result) => {
      App.found = null;
      toast(`Added ${result.device.name}`);
      return refresh();
    })
    .catch(fail);
}

function pair(ip) {
  toast(`Pairing with ${ip} — press the link button`);
  api('POST', '/api/pair', { ip })
    .then(() => {
      App.discovered = null;
      toast('Bridge paired');
      return refresh();
    })
    .catch((error) => {
      if (/link button/i.test(error.message)) toast('Press the link button on the bridge, then Pair again', true);
      else fail(error);
    });
}

document.addEventListener('click', (event) => {
  const element = event.target.closest('[data-act]');
  const action = element && actions[element.dataset.act];
  if (!action) return;
  event.preventDefault();
  action(element);
});

// Switches, selects and colour inputs report on change; ranges stream on input.
document.addEventListener('change', (event) => {
  const element = event.target;
  const { act, id } = element.dataset;
  if (act === 'toggle') command(id, { on: element.checked });
  else if (act === 'effect') command(id, { effect: element.value });
  else if (act === 'colour-pick') command(id, { on: true, hex: element.value });
  else if (act === 'brightness') command(id, { brightness: Number(element.value) });
  else if (act === 'mirek') command(id, { mirek: Number(element.value) });
  else if (act === 'target') command(id, { target: Number(element.value) });
  else if (act === 'volume') command(id, { volume: Number(element.value) });
});

document.addEventListener('input', (event) => {
  const element = event.target;
  const { act, id } = element.dataset;
  const streamed = { brightness: 'brightness', mirek: 'mirek', target: 'target', volume: 'volume' };
  if (!streamed[act]) return;
  const value = Number(element.value);
  const shown = {
    brightness: `${value}%`,
    mirek: `${Math.round(1000000 / value)} K`,
    target: `${value.toFixed(1)}°C`,
    volume: `${value}%`,
  }[act];
  const readout = element.parentElement.querySelector('[data-role="value"]');
  if (readout) readout.textContent = shown;
  commandSoon(id, { [act]: value });
});

document.addEventListener('pointerdown', (event) => {
  if (event.target.matches('input')) holding = event.target;
});
const release = () => {
  holding = null;
};
document.addEventListener('pointerup', release);
document.addEventListener('pointercancel', release);

$('#theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.bsTheme === 'dark';
  App.theme = App.theme === 'auto' ? (dark ? 'light' : 'dark') : App.theme === 'dark' ? 'light' : 'auto';
  localStorage.setItem('theme', App.theme);
  api('PUT', '/api/ui', { theme: App.theme }).catch(() => {});
  render();
});

$('#refresh').addEventListener('click', () => api('POST', '/api/refresh', {}).catch(fail));

// --- live feed ---------------------------------------------------------------

function refresh() {
  return api('GET', '/api/state').then((snapshot) => {
    App.snapshot = snapshot;
    render();
  });
}

function connect() {
  const source = new EventSource('/api/events');
  source.addEventListener('state', (event) => {
    App.snapshot = JSON.parse(event.data);
    if (App.theme === 'auto' && App.snapshot.ui?.theme && !localStorage.getItem('theme')) {
      App.theme = App.snapshot.ui.theme;
    }
    render();
  });
  source.addEventListener('error', () => {
    const badge = $('#status');
    badge.textContent = 'reconnecting';
    badge.className = 'badge rounded-pill text-bg-warning';
  });
}

matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => App.theme === 'auto' && render());

render();
refresh().catch(() => {});
connect();
