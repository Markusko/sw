/* End-to-end check against a running demo bridge, driven as a tablet.
 *
 *   python3 -m homeiot --demo &
 *   node homeiot/tests/e2e.mjs
 *
 * The context has a touchscreen and a tablet viewport, and every interaction
 * below is a tap rather than a click, because that is how this thing is used.
 * Needs Playwright; point PLAYWRIGHT_MODULE at it if the import cannot resolve,
 * and HOMEIOT_URL at a server on another port.
 */

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.HOMEIOT_URL || 'http://127.0.0.1:8712';
const state = async () => (await fetch(`${base}/api/state`)).json();
const find = (list, name) => list.find((item) => item.name === name);
const errors = [];
let failures = 0;
const check = (label, ok, detail = '') => {
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
};

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 820, height: 1180 }, // iPad-ish, portrait
  hasTouch: true,
  isMobile: false,
  deviceScaleFactor: 2,
});
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
page.on('dialog', (dialog) => dialog.accept('Renamed lamp'));
page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));
page.on('requestfailed', (request) => errors.push(`request failed: ${request.url()}`));
await page.goto(base, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1200);

const card = (name) => page.locator('.card').filter({ hasText: name }).first();
const openPanel = async (name) => {
  await card(name).getByRole('button', { name: new RegExp(name) }).tap();
  await page.waitForSelector('#panel.show', { state: 'visible' });
  await page.waitForTimeout(400);
};
const closePanel = async () => {
  await page.locator('#panel .btn-close').tap();
  await page.waitForSelector('#panel.show', { state: 'detached' }).catch(() => {});
  await page.waitForTimeout(400);
};

// 1. everything the finger has to hit is big enough to hit
const sizes = await page.evaluate(() => {
  const measure = (selector) =>
    [...document.querySelectorAll(selector)].map((element) => {
      const box = element.getBoundingClientRect();
      return Math.min(box.width, box.height);
    });
  return {
    switches: measure('input[role=switch]'),
    ranges: measure('input[type=range]'),
    tabs: measure('#tabs .nav-link'),
    buttons: measure('.btn:not(.btn-link)'),
  };
});
const smallest = (values) => Math.min(...values);
check('switches are a comfortable touch target', smallest(sizes.switches) >= 28,
  `smallest side ${smallest(sizes.switches).toFixed(0)}px`);
check('dimmers are a comfortable touch target', smallest(sizes.ranges) >= 40,
  `smallest side ${smallest(sizes.ranges).toFixed(0)}px`);
check('tabs are a comfortable touch target', smallest(sizes.tabs) >= 40,
  `smallest side ${smallest(sizes.tabs).toFixed(0)}px`);
check('buttons are a comfortable touch target', smallest(sizes.buttons) >= 36,
  `smallest side ${smallest(sizes.buttons).toFixed(0)}px`);

// 2. tapping a room switch reaches the bridge
await card('Living room').locator('input[role=switch]').tap();
await page.waitForTimeout(1300);
let snapshot = await state();
check('tapping a room switch reaches the server', find(snapshot.groups, 'Living room').state.on_count === 0,
  `on_count=${find(snapshot.groups, 'Living room').state.on_count}`);

// 3. a scene button on the room card
await card('Living room').getByRole('button', { name: 'Movie night' }).tap();
await page.waitForTimeout(1300);
snapshot = await state();
check('tapping a scene recalls it', find(snapshot.scenes, 'Movie night').active === true);
check('the scene switched lamps on', find(snapshot.groups, 'Living room').state.on_count > 0,
  `on_count=${find(snapshot.groups, 'Living room').state.on_count}`);

// 4. the detail panel opens by tap, and its dimmer drives the lamp
await page.locator('#tabs .nav-link', { hasText: 'Devices' }).tap();
await page.waitForTimeout(500);
await openPanel('Sofa left');
const dimmer = page.locator('#panel input[data-act="brightness"]');
await dimmer.fill('22');
await dimmer.dispatchEvent('change');
await page.waitForTimeout(1200);
snapshot = await state();
check('the panel dimmer reaches the server', find(snapshot.devices, 'Sofa left').state.brightness === 22,
  `brightness=${find(snapshot.devices, 'Sofa left').state.brightness}`);

// 5. tapping the track of a native range moves it -- the browser's own handling
const track = await dimmer.boundingBox();
await page.touchscreen.tap(track.x + track.width * 0.75, track.y + track.height / 2);
await page.waitForTimeout(1300);
snapshot = await state();
const dragged = find(snapshot.devices, 'Sofa left').state.brightness;
check('tapping along the dimmer track changes brightness', dragged > 40, `brightness=${dragged}`);

// 6. colour, by preset and by the native colour input
await page.locator('#panel [data-act="colour"]').nth(8).tap();
await page.waitForTimeout(1300);
snapshot = await state();
const hex = find(snapshot.devices, 'Sofa left').state.hex;
check('tapping a colour preset reaches the server', /^#[0-9a-f]{6}$/.test(hex) && hex !== '#ffffff', `hex=${hex}`);
check('the panel offers a native colour input', await page.locator('#panel input[type=color]').count() === 1);

// 7. colour temperature and effects
await closePanel();
await openPanel('Bedside left');
const warmth = page.locator('#panel input[data-act="mirek"]');
await warmth.fill('454');
await warmth.dispatchEvent('change');
await page.waitForTimeout(1200);
snapshot = await state();
check('colour temperature reaches the server', find(snapshot.devices, 'Bedside left').state.mirek === 454,
  `mirek=${find(snapshot.devices, 'Bedside left').state.mirek}`);

await closePanel();
await openPanel('Counter strip');
await page.locator('#panel select[data-act="effect"]').selectOption('candle');
await page.waitForTimeout(1200);
snapshot = await state();
check('choosing an effect reaches the server', find(snapshot.devices, 'Counter strip').state.effect === 'candle',
  `effect=${find(snapshot.devices, 'Counter strip').state.effect}`);
await closePanel();

// 8. collections, created and driven entirely by touch
await page.locator('#tabs .nav-link', { hasText: 'Collections' }).tap();
await page.waitForTimeout(500);
await page.locator('[data-act="new-collection"]').first().tap();
await page.waitForSelector('#collection-name');
await page.fill('#collection-name', 'Evening');
await page.locator('.picker .form-check', { hasText: 'Bedside left' }).locator('input').tap();
await page.locator('.picker .form-check', { hasText: 'Hallway bulb' }).locator('input').tap();
await page.locator('[data-act="save-collection"]').tap();
await page.waitForTimeout(1200);
snapshot = await state();
const collection = find(snapshot.collections, 'Evening');
check('a collection can be built by touch', !!collection && collection.member_ids.length === 2);

await card('Evening').locator('input[role=switch]').tap();
await page.waitForTimeout(1400);
snapshot = await state();
check('a collection switch drives every member',
  find(snapshot.devices, 'Bedside left').state.on === find(snapshot.devices, 'Hallway bulb').state.on,
  `${find(snapshot.devices, 'Bedside left').state.on} / ${find(snapshot.devices, 'Hallway bulb').state.on}`);

await openPanel('Evening');
await page.locator('[data-act="delete-collection"]').tap();
await page.waitForTimeout(1000);
snapshot = await state();
check('a collection can be deleted', !find(snapshot.collections, 'Evening'));

// 9. live updates patch the page rather than rebuilding it under the finger
await page.locator('#tabs .nav-link', { hasText: 'Devices' }).tap();
await page.waitForTimeout(500);
const steady = await page.evaluate(async () => {
  const target = [...document.querySelectorAll('.card')].find((c) => c.textContent.includes('Hallway bulb'));
  let replaced = 0;
  const observer = new MutationObserver((records) => {
    replaced += records.reduce((count, record) => count + record.addedNodes.length, 0);
  });
  observer.observe(document.getElementById('main'), { childList: true });
  target.querySelector('input[role=switch]').click();
  await new Promise((done) => setTimeout(done, 5000)); // long enough to cover a poll
  observer.disconnect();
  return { replaced, alive: target.isConnected };
});
check('live updates patch the page instead of rebuilding it', steady.replaced === 0 && steady.alive,
  `nodes replaced=${steady.replaced}, card kept=${steady.alive}`);

// 10. an open editor belongs to the user, not to the incoming state
await page.locator('#tabs .nav-link', { hasText: 'Collections' }).tap();
await page.waitForTimeout(400);
await page.locator('[data-act="new-collection"]').first().tap();
await page.waitForSelector('#collection-name');
await page.fill('#collection-name', 'Half typed');
await page.locator('.picker .form-check', { hasText: 'Wardrobe light' }).locator('input').tap();
await page.waitForTimeout(5000);
check('the open editor keeps what you typed',
  (await page.inputValue('#collection-name')) === 'Half typed' &&
    (await page.locator('.picker .form-check', { hasText: 'Wardrobe light' }).locator('input').isChecked()));
await closePanel();

// 11. theme
const modes = [await page.getAttribute('html', 'data-theme-mode')];
for (let index = 0; index < 3; index += 1) {
  await page.locator('#theme').tap();
  await page.waitForTimeout(300);
  modes.push(await page.getAttribute('html', 'data-theme-mode'));
}
check('theme cycles through all three modes', new Set(modes).size === 3, modes.join(' -> '));
check('dark mode reaches Bootstrap', ['light', 'dark'].includes(await page.getAttribute('html', 'data-bs-theme')));

// 12. nothing spills sideways, on a tablet or a phone
for (const [label, width, height] of [['phone', 390, 844], ['tablet portrait', 820, 1180], ['tablet landscape', 1180, 820]]) {
  await page.setViewportSize({ width, height });
  await page.waitForTimeout(500);
  const spill = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(`no sideways scrolling on ${label}`, spill <= 0, `overflow=${spill}px`);
}

// 13. the detail panel is a modal, above the page, not a side panel
await page.setViewportSize({ width: 820, height: 1180 });
await page.locator('#tabs .nav-link', { hasText: 'Rooms' }).tap();
await page.waitForTimeout(600);
await openPanel('Living room');
const box = await page.locator('#panel .modal-dialog').boundingBox();
check('the detail panel is a centred modal, not a side panel',
  box.x > 20 && box.x + box.width < 820 - 20, `x=${Math.round(box.x)} w=${Math.round(box.width)}`);
check('the modal has a backdrop', (await page.locator('.modal-backdrop').count()) === 1);

// --- pinned values show in the room modal and on the card
const valueRows = await page.locator('#panel', { hasText: 'VALUES' }).count();
check('the room modal has a Values section', valueRows > 0);
await closePanel();

const strip = await card('Living room').locator('.readout').textContent();
check('the room card shows its pinned values', /Temperature/.test(strip) && /Humidity/.test(strip),
  strip.replace(/\s+/g, ' ').trim().slice(0, 80));

// --- add a value to another room, through the UI
await openPanel('Kitchen');
await page.selectOption('#value-source', 'shelly:demoshelly1pm:switch:0|power');
await page.fill('#value-label', 'Boiler draw');
await page.locator('[data-act="add-readout"]').tap();
await page.waitForTimeout(1200);
snapshot = await state();
const added = snapshot.readouts.find((r) => r.label === 'Boiler draw');
check('a value can be pinned to a room from the UI', !!added && /W$/.test(added.display),
  added ? `${added.label} = ${added.display}` : 'not created');
await closePanel();
await page.waitForTimeout(800);
check('the new value appears on that room card',
  /Boiler draw/.test(await card('Kitchen').locator('.readout').textContent()));

// --- and can be removed again
await openPanel('Kitchen');
await page.locator('#panel [data-act="delete-readout"]').first().tap();
await page.waitForTimeout(1000);
snapshot = await state();
check('a pinned value can be removed', !snapshot.readouts.some((r) => r.label === 'Boiler draw'));
await closePanel();

// --- the camera: still on the card, live stream in the modal
const thumb = card('Hallway').locator('img.camera-thumb');
check('the room card shows a camera still', (await thumb.count()) === 1);
const loaded = await thumb.evaluate((img) =>
  img.complete ? img.naturalWidth > 0 : new Promise((done) => {
    img.addEventListener('load', () => done(img.naturalWidth > 0), { once: true });
    img.addEventListener('error', () => done(false), { once: true });
  })
);
check('the camera still actually renders', loaded);

await openPanel('Hallway');
const live = page.locator('#panel img.camera-frame');
check('the room modal offers a live view', (await live.count()) === 1);
const playing = await live.evaluate((img) =>
  new Promise((done) => {
    if (img.naturalWidth > 0) return done(true);
    img.addEventListener('load', () => done(true), { once: true });
    img.addEventListener('error', () => done(false), { once: true });
    setTimeout(() => done(img.naturalWidth > 0), 4000);
  })
);
check('the live view is receiving frames', playing);
await closePanel();

// --- Shelly device is present and controllable
await page.locator('#tabs .nav-link', { hasText: 'Devices' }).tap();
await page.waitForTimeout(600);
const relay = card('Boiler relay');
check('the Shelly device has a card', (await relay.count()) === 1);
const readings = await relay.textContent();
check('the Shelly card shows its measurements', /W/.test(readings) && /°C/.test(readings),
  readings.replace(/\s+/g, ' ').trim().slice(0, 90));

const wasOn = (await state()).devices.find((d) => d.name === 'Boiler relay').state.on;
await relay.locator('input[role=switch]').tap();
await page.waitForTimeout(1400);
snapshot = await state();
const shelly = snapshot.devices.find((d) => d.name === 'Boiler relay');
check('tapping the Shelly switch reaches the device', shelly.state.on === !wasOn,
  `${wasOn} -> ${shelly.state.on}`);
check('the Shelly reports the power it now draws',
  shelly.readings.some((r) => r.kind === 'power' && (shelly.state.on ? r.value > 0 : r.value === 0)));

// --- adding a camera through the UI
await page.locator('#tabs .nav-link', { hasText: 'Settings' }).tap();
await page.waitForTimeout(500);
check('settings lists both integrations',
  /Hue/.test(await page.locator('#main').textContent()) && /Shelly/.test(await page.locator('#main').textContent()));
check('settings offers a Shelly search', (await page.locator('[data-act="find"][data-source="shelly"]').count()) === 1);

await page.locator('[data-act="new-camera"]').tap();
await page.waitForSelector('#camera-name');
await page.fill('#camera-name', 'Garage');
await page.fill('#camera-rtsp', 'rtsp://admin:hunter2@10.0.0.9:554/h264');
await page.selectOption('#camera-room', { label: 'Kitchen' });
await page.locator('[data-act="save-camera"]').tap();
await page.waitForTimeout(1200);
snapshot = await state();
const camera = snapshot.cameras.find((c) => c.name === 'Garage');
check('a camera can be added from the UI', !!camera, camera ? camera.mode : 'not created');
check('the API never hands back the camera password', !!camera && !/hunter2/.test(JSON.stringify(snapshot)),
  camera ? camera.url : '');

await page.locator(`[data-act="delete-camera"][data-id="${camera.id}"]`).tap();
await page.waitForTimeout(1000);
snapshot = await state();
check('a camera can be removed', !snapshot.cameras.some((c) => c.name === 'Garage'));

console.log(errors.length ? 'CONSOLE ERRORS:\n' + errors.join('\n') : 'no console errors');
console.log(failures ? `${failures} FAILED` : 'all checks passed');
await browser.close();
process.exit(failures || errors.length ? 1 : 0);
