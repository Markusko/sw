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

console.log(errors.length ? 'CONSOLE ERRORS:\n' + errors.join('\n') : 'no console errors');
console.log(failures ? `${failures} FAILED` : 'all checks passed');
await browser.close();
process.exit(failures || errors.length ? 1 : 0);
