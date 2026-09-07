/* homeiot dashboard -- no framework, no build step.
 *
 * The shape of the thing: a snapshot arrives over server-sent events, pure
 * functions turn it into HTML, and one delegated listener turns clicks back
 * into API calls.  Re-rendering is frozen while a slider is being dragged.
 */

const App = {
  snapshot: null,
  view: localStorage.getItem('view') || 'rooms',
  query: '',
  theme: localStorage.getItem('theme') || 'auto',
  drawer: null, // {kind: 'detail'|'edit', id}
  dragging: false,
  pointerActive: false,
  pending: false,
  connection: 'connecting',
  discovered: null,
};

const VIEWS = [
  ['rooms', 'Rooms'],
  ['devices', 'Devices'],
  ['collections', 'Collections'],
  ['scenes', 'Scenes'],
  ['network', 'Network'],
];

const PRESETS = [
  '#ffffff', '#ffe9c4', '#ffd1a1', '#ffb26b', '#ff8c42', '#ff5c5c',
  '#ff6bb5', '#c86bff', '#6b7cff', '#4fc3ff', '#5ce1c4', '#8bff9d',
];

// --- tiny helpers ------------------------------------------------------------

const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const byId = (list, id) => (list || []).find((item) => item.id === id);
const attr = (id) => esc(id);

const icons = {
  bulb: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.6.5.9 1.2.9 1.9V16h5.4v-.3c0-.7.3-1.4.9-1.9A6 6 0 0 0 12 3Z"/></svg>',
  plug: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8ZM12 17v4"/></svg>',
  motion: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="5" r="2"/><path d="m9 21 2-6-2-3V9l4-1 3 3 2 1M9 12l-3 2"/></svg>',
  switch: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><rect x="6" y="3" width="12" height="18" rx="3"/><circle cx="12" cy="8" r="1.4"/><path d="M9 14h6M9 17.5h6"/></svg>',
  door: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 21h16M6 21V4a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v17"/><circle cx="13" cy="12" r="1"/></svg>',
  temp: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z"/></svg>',
  lux: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/></svg>',
  room: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/></svg>',
  zone: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="8" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/></svg>',
  collection: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="m3 13 9 5 9-5M3 17.5l9 5 9-5"/></svg>',
  refresh: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v4h-4"/></svg>',
  sun: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/></svg>',
  moon: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/></svg>',
  auto: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor" stroke="none"/></svg>',
  close: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  plus: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  bridge: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg>',
  chip: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" stroke-linecap="round"/></svg>',
};

const kindIcon = (device) =>
  ({ light: icons.bulb, plug: icons.plug, sensor: icons.motion, switch: icons.switch, bridge: icons.bridge }[device.kind] ||
  icons.chip);

const readingIcon = { temperature: icons.temp, light_level: icons.lux, motion: icons.motion, contact: icons.door };

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
    const wait_left = wait - (Date.now() - last);
    if (wait_left <= 0) {
      last = Date.now();
      fn(...queued);
      queued = null;
    } else if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        if (queued) {
          last = Date.now();
          fn(...queued);
          queued = null;
        }
      }, wait_left);
    }
  };
}

// --- API ---------------------------------------------------------------------

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

const command = (id, body) => api('PUT', `/api/targets/${encodeURIComponent(id)}/state`, body).catch(fail);
const commandSoon = throttle((id, body) => command(id, body), 120);
const fail = (error) => toast(error.message || String(error), true);

function toast(message, bad) {
  const node = $('#toast');
  node.innerHTML = `<div class="toast${bad ? ' bad' : ''}">${esc(message)}</div>`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (node.innerHTML = ''), bad ? 5200 : 2600);
}

// --- pieces ------------------------------------------------------------------

const toggleButton = (id, state, disabled) => {
  const pressed = state.mixed || (state.any_on !== undefined && state.any_on && !state.all_on) ? 'mixed' : String(!!state.on);
  return `<button class="toggle" data-act="toggle" data-id="${attr(id)}" aria-pressed="${pressed}"
    aria-label="Toggle" ${disabled ? 'disabled' : ''}><span></span></button>`;
};

const brightnessSlider = (id, state, label = 'Brightness') => {
  if (state.brightness === null || state.brightness === undefined) return '';
  const value = Math.round(state.brightness);
  const fill = state.on ? esc(state.hex || 'currentColor') : 'var(--surface-3)';
  return `<label class="slider brightness" style="--fill:${value}%;--fill-color:${fill}">
      <span class="label"><span>${label}</span><span data-role="value">${value}%</span></span>
      <input type="range" min="1" max="100" value="${value}" data-act="brightness" data-id="${attr(id)}"
        ${state.on ? '' : 'disabled'} aria-label="${label}" />
    </label>`;
};

const sceneChips = (scenes, ids) => {
  const chosen = (ids || []).map((id) => byId(scenes, id)).filter(Boolean);
  if (!chosen.length) return '';
  return `<div class="chiprow">${chosen
    .map(
      (scene) => `<button class="chip" data-act="scene" data-id="${attr(scene.id)}" aria-pressed="${!!scene.active}">
        ${scene.colors && scene.colors.length ? `<span class="dots">${scene.colors
          .map((color) => `<i style="background:${esc(color)}"></i>`)
          .join('')}</span>` : ''}${esc(scene.name)}</button>`
    )
    .join('')}</div>`;
};

const readingRow = (reading) => {
  const alert = (reading.kind === 'motion' && reading.value) || (reading.kind === 'contact' && !reading.value);
  return `<div class="reading">
      <span class="k">${readingIcon[reading.kind] || ''}${esc(reading.label)}</span>
      <span class="v${alert ? ' alert' : ''}">${esc(reading.display)}</span>
    </div>`;
};

const batteryBadge = (battery) =>
  battery && battery.level !== null && battery.level !== undefined
    ? `<span class="badge${battery.level <= 15 ? ' low' : ''}">${battery.level}%</span>`
    : '';

// --- cards -------------------------------------------------------------------

function deviceCard(device) {
  const state = device.state || {};
  const on = !!state.on;
  const dimmable = device.capabilities.includes('dimming');
  const classes = ['card', on ? 'on' : 'off', device.reachable === false ? 'unreachable' : ''].join(' ');
  const subtitle = device.reachable === false ? 'Unreachable' : device.room_name || device.product || 'No room';

  return `<article class="${classes}" style="--tint:${esc(state.hex || 'transparent')}">
    <div class="glow"></div>
    <button class="card-open" data-act="open" data-id="${attr(device.id)}" aria-label="Open ${esc(device.name)}"></button>
    <div class="card-head">
      <span class="swatch">${kindIcon(device)}</span>
      <span class="card-title"><h3>${esc(device.name)}</h3><p>${esc(subtitle)}</p></span>
      ${batteryBadge(device.battery)}
      ${device.controllable ? toggleButton(device.id, state, device.reachable === false) : ''}
    </div>
    ${dimmable ? `<div class="controls">${brightnessSlider(device.id, state)}</div>` : ''}
    ${device.readings && device.readings.length ? `<div class="readings">${device.readings.map(readingRow).join('')}</div>` : ''}
    ${device.buttons && device.buttons.length ? `<div class="readings">${readingRow({
      kind: 'switch',
      label: 'Last press',
      display: `${device.buttons.map((b) => b.display).filter(Boolean)[0] || '—'} · ${since(
        device.buttons.map((b) => b.updated).sort().reverse()[0]
      )}`,
    })}</div>` : ''}
  </article>`;
}

function groupCard(group, scenes) {
  const state = group.state || {};
  const summary = state.count
    ? `${state.on_count} of ${state.count} on`
    : group.kind === 'zone'
    ? 'Zone'
    : 'No lights';
  return `<article class="card ${state.any_on ? 'on' : 'off'}" style="--tint:${esc(state.hex || 'transparent')}">
    <div class="glow"></div>
    <button class="card-open" data-act="open" data-id="${attr(group.id)}" aria-label="Open ${esc(group.name)}"></button>
    <div class="card-head">
      <span class="swatch">${group.kind === 'room' ? icons.room : icons.zone}</span>
      <span class="card-title"><h3>${esc(group.name)}</h3><p>${esc(summary)}</p></span>
      ${toggleButton(group.id, state, !state.count)}
    </div>
    ${state.count ? `<div class="controls">${brightnessSlider(group.id, { ...state, on: state.any_on })}</div>` : ''}
    ${sceneChips(scenes, group.scenes)}
  </article>`;
}

function collectionCard(collection) {
  const state = collection.state || {};
  const summary = state.count ? `${state.on_count} of ${state.count} on` : `${collection.member_ids.length} members`;
  return `<article class="card ${state.any_on ? 'on' : 'off'}" style="--tint:${esc(state.hex || 'transparent')}">
    <div class="glow"></div>
    <button class="card-open" data-act="open" data-id="${attr(collection.id)}" aria-label="Open ${esc(collection.name)}"></button>
    <div class="card-head">
      <span class="swatch">${icons.collection}</span>
      <span class="card-title"><h3>${esc(collection.name)}</h3><p>${esc(summary)}${
        collection.missing ? ` · ${collection.missing} missing` : ''
      }</p></span>
      ${toggleButton(collection.id, state, !state.count)}
    </div>
    ${state.count ? `<div class="controls">${brightnessSlider(collection.id, { ...state, on: state.any_on })}</div>` : ''}
  </article>`;
}

const section = (title, inner, action = '') =>
  `<section class="section"><header><h2>${esc(title)}</h2><span class="rule"></span>${action}</header>${inner}</section>`;

const grid = (cards, wide) => `<div class="grid${wide ? ' wide' : ''}">${cards.join('')}</div>`;

const emptyState = (title, body, action = '') =>
  `<div class="empty"><h3>${esc(title)}</h3><p>${esc(body)}</p>${action}</div>`;

// --- views -------------------------------------------------------------------

function matches(item, query) {
  if (!query) return true;
  const haystack = [item.name, item.room_name, item.product, item.model, item.kind].join(' ').toLowerCase();
  return haystack.includes(query);
}

function viewRooms(snapshot, query) {
  const rooms = snapshot.groups.filter((group) => group.kind === 'room' && matches(group, query));
  const zones = snapshot.groups.filter((group) => group.kind === 'zone' && matches(group, query));
  const loose = snapshot.devices.filter(
    (device) => !device.room && device.kind !== 'bridge' && matches(device, query)
  );
  const blocks = [];
  if (rooms.length) blocks.push(section('Rooms', grid(rooms.map((room) => groupCard(room, snapshot.scenes)))));
  if (zones.length) blocks.push(section('Zones', grid(zones.map((zone) => groupCard(zone, snapshot.scenes)))));
  if (loose.length) blocks.push(section('Not in a room', grid(loose.map(deviceCard))));
  return blocks.join('') || emptyState('Nothing here yet', 'No rooms matched. Try the Devices tab.');
}

function viewDevices(snapshot, query) {
  const groupsOf = [
    ['Lights', 'light'],
    ['Plugs', 'plug'],
    ['Sensors', 'sensor'],
    ['Switches', 'switch'],
    ['Other', 'other'],
  ];
  const visible = snapshot.devices.filter((device) => device.kind !== 'bridge' && matches(device, query));
  const blocks = groupsOf
    .map(([title, kind]) => {
      const devices = visible.filter((device) => device.kind === kind);
      return devices.length ? section(`${title} · ${devices.length}`, grid(devices.map(deviceCard))) : '';
    })
    .filter(Boolean);
  return blocks.join('') || emptyState('No devices', 'Nothing matched that search.');
}

function viewCollections(snapshot) {
  const action = `<button class="btn ghost" data-act="new-collection">${icons.plus} New</button>`;
  if (!snapshot.collections.length) {
    return section(
      'Collections',
      emptyState(
        'Group things your own way',
        'A collection cuts across rooms: "Evening", "Desk", "Downstairs lamps", "Away lights". Pick any devices, rooms or zones and control them together.',
        `<button class="btn primary" data-act="new-collection">Create a collection</button>`
      ),
      action
    );
  }
  return section('Collections', grid(snapshot.collections.map(collectionCard)), action);
}

function viewScenes(snapshot, query) {
  const scenes = snapshot.scenes.filter((scene) => matches(scene, query));
  if (!scenes.length) return emptyState('No scenes', 'Scenes you create in the Hue app show up here.');
  const rooms = [...new Set(scenes.map((scene) => scene.group_name || 'Other'))];
  return rooms
    .map((room) => {
      const owned = scenes.filter((scene) => (scene.group_name || 'Other') === room);
      return section(
        room,
        `<div class="chiprow">${owned
          .map(
            (scene) => `<button class="chip" data-act="scene" data-id="${attr(scene.id)}" aria-pressed="${!!scene.active}">
              ${scene.colors && scene.colors.length ? `<span class="dots">${scene.colors
                .map((color) => `<i style="background:${esc(color)}"></i>`)
                .join('')}</span>` : ''}${esc(scene.name)}</button>`
          )
          .join('')}</div>`
      );
    })
    .join('');
}

function viewNetwork(snapshot) {
  const bridges = snapshot.bridges
    .map(
      (bridge) => `<div class="bridge-row">
        <span class="swatch">${icons.bridge}</span>
        <span class="grow"><strong>${esc(bridge.name)}</strong>${bridge.demo ? ' <span class="badge">demo</span>' : ''}
          <p>${esc(bridge.ip)} · ${esc(bridge.model || 'Hue')} · ${esc(
        bridge.connected ? (bridge.stream === 'connected' ? 'live event stream' : 'polling') : bridge.error || 'offline'
      )}</p></span>
        <button class="btn danger ghost" data-act="forget-bridge" data-id="${attr(bridge.id)}">Remove</button>
      </div>`
    )
    .join('');

  const network = snapshot.network || {};
  const hosts = (network.hosts || [])
    .map(
      (host) => `<div class="host">
        <span class="ip">${esc(host.ip)}</span>
        <span class="name">${esc(host.hostname || '—')}</span>
        <span class="meta">${esc((host.labels || []).slice(0, 3).join(' · ') || 'unknown')}</span>
      </div>`
    )
    .join('');

  return [
    section(
      'Bridges',
      `<div class="card">${bridges || '<p class="meta">No bridges paired.</p>'}
        <div class="btnrow" style="margin-top:14px">
          <button class="btn primary" data-act="add-bridge">Add a bridge</button>
          <button class="btn ghost" data-act="add-demo">Add demo bridge</button>
        </div></div>`
    ),
    section(
      'Everything else on the network',
      `<div class="card" style="margin-bottom:12px">
        <p class="meta">Passive discovery over mDNS and SSDP. These devices are visible but not yet
          controllable here &mdash; Hue is the integration that exists today.</p>
        <div class="btnrow" style="margin-top:12px">
          <button class="btn ${network.scanning ? '' : 'primary'}" data-act="scan" ${network.scanning ? 'disabled' : ''}>
            ${network.scanning ? 'Scanning…' : 'Scan the network'}</button>
          ${network.scanned_at ? `<span class="meta" style="align-self:center">${esc(
            new Date(network.scanned_at * 1000).toLocaleTimeString()
          )}</span>` : ''}
        </div>
      </div>
      <div class="hostlist">${hosts || '<p class="meta">Nothing scanned yet.</p>'}</div>`
    ),
  ].join('');
}

function viewSetup() {
  const found = App.discovered;
  const list = found
    ? found.length
      ? found
          .map(
            (bridge) => `<div class="bridge-row">
              <span class="swatch">${icons.bridge}</span>
              <span class="grow"><strong>${esc(bridge.name)}</strong><p>${esc(bridge.ip)} · ${esc(
              bridge.model
            )} · API ${esc(bridge.api_version)}</p></span>
              <button class="btn primary" data-act="pair" data-ip="${esc(bridge.ip)}" ${
              bridge.paired ? 'disabled' : ''
            }>${bridge.paired ? 'Paired' : 'Pair'}</button>
            </div>`
          )
          .join('')
      : '<p class="meta">No bridge answered. Check that it is powered and on this network, or enter its address below.</p>'
    : '';

  return `<div class="setup">
    <div class="card">
      <h2>Connect your Hue bridge</h2>
      <ol>
        <li>Make sure the bridge is on the same network as this machine.</li>
        <li>Press <strong>Search</strong> below.</li>
        <li>Press the round <strong>link button</strong> on top of the bridge, then press Pair within 30 seconds.</li>
      </ol>
      <div class="btnrow">
        <button class="btn primary" data-act="discover">${App.discovering ? 'Searching…' : 'Search for bridges'}</button>
        <button class="btn ghost" data-act="add-demo">Try the demo</button>
      </div>
      ${list}
      <div class="block" style="margin-top:22px">
        <h4>Or enter an address</h4>
        <div class="btnrow">
          <input type="text" id="manual-ip" placeholder="192.168.1.42" style="flex:1;min-width:180px" />
          <button class="btn" data-act="pair-manual">Pair</button>
        </div>
      </div>
    </div>
  </div>`;
}

// --- drawer ------------------------------------------------------------------

function colorControls(id, state, capabilities) {
  const blocks = [];
  if (capabilities.includes('color')) {
    blocks.push(`<div class="block"><h4>Colour</h4>
      <div class="swatches">${PRESETS.map(
        (color) => `<button data-act="color" data-id="${attr(id)}" data-color="${esc(color)}"
          style="background:${esc(color)}" aria-label="${esc(color)}"></button>`
      ).join('')}</div>
      <label class="slider hue-track" style="margin-top:12px">
        <span class="label"><span>Hue</span></span>
        <input type="range" min="0" max="360" value="${Math.round(hueOf(state.hex))}" data-act="hue" data-id="${attr(id)}" />
      </label></div>`);
  }
  if (capabilities.includes('color_temp') && state.mirek_range) {
    const [min, max] = state.mirek_range;
    blocks.push(`<div class="block"><h4>White</h4>
      <label class="slider ct-track">
        <span class="label"><span>Temperature</span><span data-role="value">${
          state.kelvin ? `${state.kelvin} K` : '—'
        }</span></span>
        <input type="range" min="${min}" max="${max}" value="${state.mirek || 300}"
          data-act="mirek" data-id="${attr(id)}" />
      </label></div>`);
  }
  if (capabilities.includes('effects') && (state.effects || []).length) {
    blocks.push(`<div class="block"><h4>Effects</h4><div class="btnrow">${state.effects
      .map(
        (effect) => `<button class="chip" data-act="effect" data-id="${attr(id)}" data-effect="${esc(effect)}"
          aria-pressed="${state.effect === effect}">${esc(effect.replace(/_/g, ' '))}</button>`
      )
      .join('')}</div></div>`);
  }
  return blocks.join('');
}

function hueOf(hex) {
  if (!hex) return 0;
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max === min) return 0;
  const d = max - min;
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return h * 60;
}

const hslHex = (hue, saturation = 0.9, lightness = 0.55) => {
  const f = (n) => {
    const k = (n + hue / 30) % 12;
    const a = saturation * Math.min(lightness, 1 - lightness);
    const value = lightness - a * Math.max(-1, Math.min(k - 3, Math.min(9 - k, 1)));
    return Math.round(255 * value)
      .toString(16)
      .padStart(2, '0');
  };
  return `#${f(0)}${f(8)}${f(4)}`;
};

function drawerDevice(device, snapshot) {
  const state = device.state || {};
  const lights = device.lights || [];
  return `
    <header>
      <span class="swatch" style="--tint:${esc(state.hex || 'transparent')}">${kindIcon(device)}</span>
      <span class="card-title" style="flex:1">
        <h2>${esc(device.name)}</h2>
        <p>${esc(device.product || device.kind)}${device.room_name ? ` · ${esc(device.room_name)}` : ''}</p>
      </span>
      ${device.controllable ? toggleButton(device.id, state, device.reachable === false) : ''}
      <button class="iconbtn" data-act="close">${icons.close}</button>
    </header>
    ${device.controllable
      ? `<div class="block">${brightnessSlider(device.id, state)}</div>${colorControls(
          device.id,
          state,
          device.capabilities
        )}`
      : ''}
    ${lights.length > 1
      ? `<div class="block"><h4>Individual lights</h4>${lights
          .map(
            (light) => `<div class="row"><span class="k">${esc(light.name)}</span>
              <span class="v">${toggleButton(light.id, light.state)}</span></div>
              <div style="padding-bottom:8px">${brightnessSlider(light.id, light.state, '')}</div>`
          )
          .join('')}</div>`
      : ''}
    ${device.readings && device.readings.length
      ? `<div class="block"><h4>Readings</h4>${device.readings
          .map(
            (reading) => `<div class="row"><span class="k">${esc(reading.label)}</span>
              <span class="v">${esc(reading.display)}${reading.updated ? ` <span class="meta">${since(
              reading.updated
            )}</span>` : ''}</span></div>`
          )
          .join('')}</div>`
      : ''}
    ${device.buttons && device.buttons.length
      ? `<div class="block"><h4>Buttons</h4>${device.buttons
          .map(
            (button) => `<div class="row"><span class="k">Button ${esc(button.control_id ?? '')}</span>
              <span class="v">${esc(button.display)} <span class="meta">${since(button.updated)}</span></span></div>`
          )
          .join('')}</div>`
      : ''}
    <div class="block"><h4>Details</h4>
      ${row('Model', device.model || '—')}
      ${row('Made by', device.manufacturer || '—')}
      ${row('Firmware', device.software || '—')}
      ${row('Zigbee', device.reachable === null ? '—' : device.reachable ? 'Connected' : 'Unreachable')}
      ${device.battery ? row('Battery', `${device.battery.level}% · ${device.battery.state || ''}`) : ''}
      ${row('Bridge', (byId(snapshot.bridges, device.bridge) || {}).name || device.bridge)}
    </div>
    <div class="block"><h4>Actions</h4>
      <div class="btnrow">
        <button class="btn" data-act="identify" data-id="${attr(device.id)}">Identify</button>
        <button class="btn" data-act="rename" data-id="${attr(device.id)}">Rename</button>
        ${device.capabilities.includes('alert')
          ? `<button class="btn" data-act="alert" data-id="${attr(device.id)}">Blink</button>`
          : ''}
      </div>
    </div>`;
}

const row = (key, value) => `<div class="row"><span class="k">${esc(key)}</span><span class="v">${esc(value)}</span></div>`;

function drawerGroup(group, snapshot) {
  const state = group.state || {};
  const members = group.device_ids.map((id) => byId(snapshot.devices, id)).filter(Boolean);
  return `
    <header>
      <span class="swatch" style="--tint:${esc(state.hex || 'transparent')}">${
    group.kind === 'room' ? icons.room : icons.zone
  }</span>
      <span class="card-title" style="flex:1"><h2>${esc(group.name)}</h2>
        <p>${esc(group.kind === 'room' ? 'Room' : 'Zone')} · ${state.on_count || 0} of ${state.count || 0} on</p></span>
      ${toggleButton(group.id, state, !state.count)}
      <button class="iconbtn" data-act="close">${icons.close}</button>
    </header>
    <div class="block">${brightnessSlider(group.id, { ...state, on: state.any_on })}</div>
    ${colorControls(group.id, state, group.capabilities || [])}
    ${group.scenes.length ? `<div class="block"><h4>Scenes</h4>${sceneChips(snapshot.scenes, group.scenes)}</div>` : ''}
    <div class="block"><h4>Devices</h4>${members
      .map(
        (device) => `<div class="row"><span class="k">${esc(device.name)}</span>
          <span class="v">${device.controllable ? toggleButton(device.id, device.state) : esc(
            (device.readings || []).map((r) => r.display).join(' · ') || '—'
          )}</span></div>`
      )
      .join('')}</div>
    <div class="block"><div class="btnrow">
      <button class="btn" data-act="rename" data-id="${attr(group.id)}">Rename</button>
    </div></div>`;
}

function drawerCollection(collection, snapshot) {
  const state = collection.state || {};
  const members = collection.member_ids
    .map((id) => byId(snapshot.devices, id) || byId(snapshot.groups, id))
    .filter(Boolean);
  return `
    <header>
      <span class="swatch" style="--tint:${esc(state.hex || 'transparent')}">${icons.collection}</span>
      <span class="card-title" style="flex:1"><h2>${esc(collection.name)}</h2>
        <p>Collection · ${members.length} members</p></span>
      ${toggleButton(collection.id, state, !state.count)}
      <button class="iconbtn" data-act="close">${icons.close}</button>
    </header>
    <div class="block">${brightnessSlider(collection.id, { ...state, on: state.any_on })}</div>
    ${colorControls(collection.id, state, collection.capabilities || [])}
    <div class="block"><h4>Members</h4>${members
      .map(
        (member) => `<div class="row"><span class="k">${esc(member.name)}</span>
          <span class="v">${member.controllable || member.grouped_light ? toggleButton(member.id, member.state) : '—'}</span></div>`
      )
      .join('')}</div>
    <div class="block"><div class="btnrow">
      <button class="btn" data-act="edit-collection" data-id="${attr(collection.id)}">Edit</button>
      <button class="btn danger ghost" data-act="delete-collection" data-id="${attr(collection.id)}">Delete</button>
    </div></div>`;
}

function drawerEditor(snapshot, collection) {
  const chosen = new Set(collection ? collection.members : []);
  const candidates = [
    ...snapshot.groups.map((group) => ({ ...group, note: group.kind === 'room' ? 'Room' : 'Zone' })),
    ...snapshot.devices
      .filter((device) => device.kind !== 'bridge')
      .map((device) => ({ ...device, note: device.room_name || device.kind })),
  ];
  return `
    <header>
      <span class="card-title" style="flex:1"><h2>${collection ? 'Edit collection' : 'New collection'}</h2>
        <p>Any mix of rooms, zones and devices.</p></span>
      <button class="iconbtn" data-act="close">${icons.close}</button>
    </header>
    <div class="block"><h4>Name</h4>
      <input type="text" id="collection-name" value="${esc(collection ? collection.name : '')}"
        placeholder="Evening, Desk, Away lights…" />
    </div>
    <div class="block"><h4>Members</h4>
      <div class="picker">${candidates
        .map(
          (item) => `<label><input type="checkbox" value="${attr(item.id)}" ${chosen.has(item.id) ? 'checked' : ''} />
            <span>${esc(item.name)}</span><span class="sub">${esc(item.note)}</span></label>`
        )
        .join('')}</div>
    </div>
    <div class="block"><div class="btnrow">
      <button class="btn primary" data-act="save-collection" data-id="${attr(collection ? collection.id : '')}">Save</button>
      <button class="btn ghost" data-act="close">Cancel</button>
    </div></div>`;
}

function renderDrawer() {
  const overlay = $('#overlay');
  if (!App.drawer) {
    overlay.innerHTML = '';
    return;
  }
  const snapshot = App.snapshot;
  const { kind, id } = App.drawer;
  let body = '';
  if (kind === 'edit') body = drawerEditor(snapshot, id ? byId(snapshot.collections, id) : null);
  else {
    const device = byId(snapshot.devices, id);
    const group = byId(snapshot.groups, id);
    const collection = byId(snapshot.collections, id);
    if (device) body = drawerDevice(device, snapshot);
    else if (group) body = drawerGroup(group, snapshot);
    else if (collection) body = drawerCollection(collection, snapshot);
    else {
      App.drawer = null;
      overlay.innerHTML = '';
      return;
    }
  }
  overlay.innerHTML = `<div class="scrim" data-act="close"></div><aside class="drawer" role="dialog" aria-modal="true">${body}</aside>`;
}

// --- render ------------------------------------------------------------------

// A repaint driven by incoming state must wait while the user is mid-gesture:
// replacing the DOM between mousedown and mouseup would swallow their click,
// and replacing a slider under the finger would drop the drag.
const sliderInUse = () =>
  App.dragging || (document.activeElement && document.activeElement.matches('input[type="range"]'));
const busy = () => App.pointerActive || sliderInUse();

function render(force) {
  if (!force && busy()) {
    App.pending = true;
    return;
  }
  const snapshot = App.snapshot;
  applyTheme();
  $('#tabs').innerHTML = VIEWS.map(
    ([key, label]) =>
      `<button role="tab" data-act="view" data-view="${key}" aria-selected="${App.view === key}">${label}</button>`
  ).join('');
  $('#refresh').innerHTML = icons.refresh;
  $('#theme').innerHTML = { auto: icons.auto, light: icons.sun, dark: icons.moon }[App.theme];
  $('#theme').title = { auto: 'Theme: follows the system', light: 'Theme: light', dark: 'Theme: dark' }[App.theme];

  if (!snapshot) {
    $('#main').innerHTML = emptyState('Connecting…', 'Reading the state of your home.');
    return;
  }
  updateLinkState(snapshot);

  const onboarding = !snapshot.bridges.length;
  $('#tabs').hidden = onboarding;
  $('#search').closest('.search').hidden = onboarding;
  if (onboarding) {
    $('#main').innerHTML = viewSetup();
    renderDrawer();
    return;
  }
  const query = App.query.trim().toLowerCase();
  const views = {
    rooms: () => viewRooms(snapshot, query),
    devices: () => viewDevices(snapshot, query),
    collections: () => viewCollections(snapshot),
    scenes: () => viewScenes(snapshot, query),
    network: () => viewNetwork(snapshot),
  };
  $('#main').innerHTML = (views[App.view] || views.rooms)();
  renderDrawer();
}

function updateLinkState(snapshot) {
  const bridges = snapshot.bridges || [];
  const live = bridges.some((bridge) => bridge.connected);
  const streaming = bridges.some((bridge) => bridge.stream === 'connected' || bridge.demo);
  const broken = bridges.filter((bridge) => !bridge.connected);
  const label = !bridges.length
    ? 'no bridge'
    : broken.length
    ? `${broken.length} offline`
    : streaming
    ? 'live'
    : 'polling';
  const state = App.connection !== 'open' ? 'warn' : broken.length ? 'down' : live ? 'live' : 'warn';
  $('#linkstate').innerHTML = `<i class="dot ${state}"></i>${esc(label)}`;
  $('#linkstate').title = broken.map((bridge) => `${bridge.name}: ${bridge.error || 'offline'}`).join('\n');
}

function applyTheme() {
  const dark = App.theme === 'dark' || (App.theme === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.themeMode = App.theme; // the chosen mode, not the resolved one
}

// --- optimistic local edits --------------------------------------------------

function optimistic(id, changes) {
  const snapshot = App.snapshot;
  if (!snapshot) return;
  const patch = (item) => (item && item.id === id ? { ...item, state: { ...item.state, ...changes } } : item);
  App.snapshot = {
    ...snapshot,
    devices: snapshot.devices.map(patch),
    groups: snapshot.groups.map((group) =>
      group.id === id ? { ...group, state: { ...group.state, ...changes, any_on: changes.on ?? group.state.any_on } } : group
    ),
    collections: snapshot.collections.map((collection) =>
      collection.id === id
        ? { ...collection, state: { ...collection.state, ...changes, any_on: changes.on ?? collection.state.any_on } }
        : collection
    ),
  };
  render(true);
}

const findAny = (id) =>
  byId(App.snapshot.devices, id) ||
  byId(App.snapshot.groups, id) ||
  byId(App.snapshot.collections, id) ||
  (App.snapshot.devices.flatMap((device) => device.lights || []).find((light) => light.id === id) || null);

// --- events ------------------------------------------------------------------

const actions = {
  view: (element) => {
    App.view = element.dataset.view;
    localStorage.setItem('view', App.view);
    render(true);
  },
  open: (element) => {
    App.drawer = { kind: 'detail', id: element.dataset.id };
    render(true);
  },
  close: () => {
    App.drawer = null;
    render(true);
  },
  toggle: (element) => {
    const id = element.dataset.id;
    const target = findAny(id);
    const next = !(target && (target.state.any_on ?? target.state.on));
    optimistic(id, { on: next });
    command(id, { on: next });
  },
  scene: (element) => api('POST', `/api/scenes/${encodeURIComponent(element.dataset.id)}/recall`, {}).catch(fail),
  color: (element) => {
    optimistic(element.dataset.id, { hex: element.dataset.color, on: true });
    command(element.dataset.id, { on: true, hex: element.dataset.color });
  },
  effect: (element) => command(element.dataset.id, { effect: element.dataset.effect }),
  alert: (element) => command(element.dataset.id, { alert: 'breathe' }),
  identify: (element) =>
    api('POST', `/api/devices/${encodeURIComponent(element.dataset.id)}/identify`, {})
      .then(() => toast('Blinking'))
      .catch(fail),
  rename: (element) => {
    const target = findAny(element.dataset.id);
    const name = prompt('New name', target ? target.name : '');
    if (name && name.trim()) {
      api('PUT', `/api/targets/${encodeURIComponent(element.dataset.id)}/name`, { name: name.trim() })
        .then(() => toast('Renamed'))
        .catch(fail);
    }
  },
  'new-collection': () => {
    App.drawer = { kind: 'edit', id: null };
    render(true);
  },
  'edit-collection': (element) => {
    App.drawer = { kind: 'edit', id: element.dataset.id };
    render(true);
  },
  'save-collection': (element) => {
    const name = $('#collection-name').value.trim() || 'Untitled';
    const members = [...document.querySelectorAll('.picker input:checked')].map((input) => input.value);
    const id = element.dataset.id;
    const request = id
      ? api('PUT', `/api/collections/${encodeURIComponent(id)}`, { name, members })
      : api('POST', '/api/collections', { name, members });
    request
      .then(() => {
        App.drawer = null;
        toast('Saved');
        return refresh();
      })
      .catch(fail);
  },
  'delete-collection': (element) => {
    if (!confirm('Delete this collection? The devices themselves are untouched.')) return;
    api('DELETE', `/api/collections/${encodeURIComponent(element.dataset.id)}`)
      .then(() => {
        App.drawer = null;
        return refresh();
      })
      .catch(fail);
  },
  discover: (element) => {
    App.discovering = true;
    element.textContent = 'Searching…';
    api('POST', '/api/discover', { deep: true })
      .then((result) => {
        App.discovered = result.bridges;
        App.discovering = false;
        render(true);
        if (!result.bridges.length) toast('No bridge found on this network', true);
      })
      .catch((error) => {
        App.discovering = false;
        fail(error);
      });
  },
  pair: (element) => pair(element.dataset.ip),
  'pair-manual': () => {
    const value = $('#manual-ip').value.trim();
    if (value) pair(value);
  },
  'add-bridge': () => {
    App.view = 'rooms';
    App.snapshot = { ...App.snapshot, bridges: [] };
    render(true);
  },
  'add-demo': () => api('POST', '/api/demo', {}).then(refresh).then(() => toast('Demo bridge added')).catch(fail),
  'forget-bridge': (element) => {
    if (!confirm('Remove this bridge? Its devices disappear from the dashboard.')) return;
    api('DELETE', `/api/bridges/${encodeURIComponent(element.dataset.id)}`).then(refresh).catch(fail);
  },
  scan: () => api('POST', '/api/scan', {}).then(() => toast('Scanning the network…')).catch(fail),
};

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
  if (!element) return;
  const action = actions[element.dataset.act];
  if (!action) return;
  event.preventDefault();
  action(element);
});

document.addEventListener('input', (event) => {
  const element = event.target;
  const action = element.dataset.act;
  if (!action) return;
  const id = element.dataset.id;
  const value = Number(element.value);
  const label = element.closest('.slider')?.querySelector('[data-role="value"]');

  if (action === 'brightness') {
    if (label) label.textContent = `${value}%`;
    element.closest('.slider').style.setProperty('--fill', `${value}%`);
    commandSoon(id, { brightness: value });
  } else if (action === 'mirek') {
    if (label) label.textContent = `${Math.round(1000000 / value)} K`;
    commandSoon(id, { mirek: value });
  } else if (action === 'hue') {
    commandSoon(id, { on: true, hex: hslHex(value) });
  }
});

// A slider under the finger (or holding focus) must not be re-rendered away.
// Repainting is also deferred while any pointer is down: replacing the DOM
// between mousedown and mouseup would swallow the click that follows.
document.addEventListener('pointerdown', (event) => {
  App.pointerActive = true;
  if (event.target.matches('input[type="range"]')) App.dragging = true;
});

const thaw = () => {
  App.dragging = false;
  if (App.pending && !busy()) {
    App.pending = false;
    render(true);
  }
};
const thawSoon = () => setTimeout(() => App.pointerActive || thaw(), 0);
// (pointerup clears pointerActive first, so the deferred thaw runs after the click)

const pointerDone = () => {
  App.pointerActive = false;
  thawSoon();
};
document.addEventListener('pointerup', pointerDone);
document.addEventListener('pointercancel', pointerDone);
document.addEventListener('focusout', (event) => {
  if (event.target.matches && event.target.matches('input[type="range"]')) thawSoon();
});

$('#search').addEventListener('input', (event) => {
  App.query = event.target.value;
  render(true);
});

$('#theme').addEventListener('click', () => {
  const showingDark = document.documentElement.dataset.theme === 'dark';
  App.theme = App.theme === 'auto' ? (showingDark ? 'light' : 'dark') : App.theme === 'dark' ? 'light' : 'auto';
  localStorage.setItem('theme', App.theme);
  api('PUT', '/api/ui', { theme: App.theme }).catch(() => {});
  render(true);
});

$('#refresh').addEventListener('click', (event) => {
  const button = event.currentTarget;
  button.classList.add('spin');
  api('POST', '/api/refresh', {})
    .catch(fail)
    .finally(() => setTimeout(() => button.classList.remove('spin'), 500));
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && App.drawer) actions.close();
  if (event.key === '/' && document.activeElement !== $('#search')) {
    event.preventDefault();
    $('#search').focus();
  }
});

matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => App.theme === 'auto' && render());

// --- live feed ---------------------------------------------------------------

function refresh() {
  return api('GET', '/api/state').then((snapshot) => {
    App.snapshot = snapshot;
    render(true);
  });
}

function connect() {
  const source = new EventSource('/api/events');
  source.addEventListener('open', () => {
    App.connection = 'open';
  });
  source.addEventListener('state', (event) => {
    App.snapshot = JSON.parse(event.data);
    App.connection = 'open';
    if (App.theme === 'auto' && App.snapshot.ui && App.snapshot.ui.theme && !localStorage.getItem('theme')) {
      App.theme = App.snapshot.ui.theme;
    }
    render(); // state-driven: yields to a slider in use
  });
  source.addEventListener('error', () => {
    App.connection = 'lost';
    if (App.snapshot) updateLinkState(App.snapshot);
  });
}

function tickClock() {
  $('#clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

tickClock();
setInterval(tickClock, 20000);
render(true);
refresh().catch(() => {});
connect();
