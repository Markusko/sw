/* End-to-end check against a running demo bridge.
 *
 *   python3 -m homeiot --demo &
 *   node homeiot/tests/e2e.mjs
 *
 * Needs Playwright.  If it is installed somewhere this script cannot resolve,
 * point PLAYWRIGHT_MODULE at it, and HOMEIOT_URL at a server on another port.
 */

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.HOMEIOT_URL || 'http://127.0.0.1:8712';
const state = async () => (await fetch(`${base}/api/state`)).json();
const find = (list, name) => list.find((item) => item.name === name);
const errors = [];
const check = (label, ok, detail = '') => console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push(e.message));
page.on('dialog', (d) => d.accept('Renamed lamp'));
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
await page.goto(base, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(900);

// 1. room toggle reaches the bridge
const room = page.locator('.card:has-text("Living room")').first();
await room.locator('.toggle').click();
await page.waitForTimeout(1200);
let s = await state();
check('room toggle off reaches the server', find(s.groups, 'Living room').state.on_count === 0,
  `on_count=${find(s.groups, 'Living room').state.on_count}`);

// 2. scene recall
await page.locator('[data-view="scenes"]').click();
await page.waitForTimeout(400);
await page.locator('.chip:has-text("Movie night")').first().click();
await page.waitForTimeout(1300);
s = await state();
check('scene recall marks the scene active', find(s.scenes, 'Movie night').active === true);
check('scene recall switched lamps on', find(s.groups, 'Living room').state.on_count > 0,
  `on_count=${find(s.groups, 'Living room').state.on_count}`);

// 3. drawer: brightness + colour on one lamp
await page.locator('[data-view="devices"]').click();
await page.waitForTimeout(400);
await page.click('.card:has-text("Sofa left") .card-open', { position: { x: 120, y: 18 } });
await page.waitForTimeout(500);
const slider = page.locator('.drawer input[data-act="brightness"]').first();
await slider.fill('22');
await slider.dispatchEvent('input');
await page.waitForTimeout(1200);
s = await state();
check('drawer brightness reaches the server', find(s.devices, 'Sofa left').state.brightness === 22,
  `brightness=${find(s.devices, 'Sofa left').state.brightness}`);

await page.locator('.drawer .swatches button').nth(9).click(); // a blue preset
await page.waitForTimeout(1200);
s = await state();
const hex = find(s.devices, 'Sofa left').state.hex;
check('colour preset reaches the server', /^#[0-9a-f]{6}$/.test(hex) && hex !== '#ffffff', `hex=${hex}`);

// 4. colour temperature
await page.locator('.drawer [data-act="close"]').first().click();
await page.waitForTimeout(300);
await page.locator('[data-view="devices"]').click();
await page.waitForTimeout(300);
await page.click('.card:has-text("Bedside left") .card-open', { position: { x: 120, y: 18 } });
await page.waitForTimeout(500);
const ct = page.locator('.drawer input[data-act="mirek"]').first();
await ct.fill('454');
await ct.dispatchEvent('input');
await page.waitForTimeout(1200);
s = await state();
check('colour temperature reaches the server', find(s.devices, 'Bedside left').state.mirek === 454,
  `mirek=${find(s.devices, 'Bedside left').state.mirek}`);

// 5. an effect
await page.locator('.drawer [data-act="close"]').first().click();
await page.waitForTimeout(300);
await page.click('.card:has-text("Counter strip") .card-open', { position: { x: 120, y: 18 } });
await page.waitForTimeout(500);
await page.locator('.drawer .chip:has-text("candle")').click();
await page.waitForTimeout(1200);
s = await state();
check('effect reaches the server', find(s.devices, 'Counter strip').state.effect === 'candle',
  `effect=${find(s.devices, 'Counter strip').state.effect}`);
await page.locator('.drawer [data-act="close"]').first().click();

// 6. collection round trip through the UI
await page.waitForTimeout(300);
await page.locator('[data-view="collections"]').click();
await page.waitForTimeout(400);
await page.locator('[data-act="new-collection"]').first().click();
await page.waitForTimeout(400);
await page.fill('#collection-name', 'Evening');
await page.locator('.picker label:has-text("Bedside left") input').check();
await page.locator('.picker label:has-text("Hallway bulb") input').check();
await page.locator('[data-act="save-collection"]').click();
await page.waitForTimeout(1000);
s = await state();
const collection = find(s.collections, 'Evening');
check('collection created with 2 members', !!collection && collection.member_ids.length === 2);

await page.locator('.card:has-text("Evening") .toggle').click();
await page.waitForTimeout(1300);
s = await state();
check('collection toggle drives every member',
  find(s.devices, 'Bedside left').state.on === find(s.devices, 'Hallway bulb').state.on,
  `${find(s.devices, 'Bedside left').state.on} / ${find(s.devices, 'Hallway bulb').state.on}`);

await page.click('.card:has-text("Evening") .card-open', { position: { x: 120, y: 18 } });
await page.waitForTimeout(400);
await page.locator('[data-act="delete-collection"]').click();
await page.waitForTimeout(800);
s = await state();
check('collection deleted', !find(s.collections, 'Evening'));

// 7. search + live updates keep working
await page.locator('[data-view="devices"]').click();
await page.fill('#search', 'bedside');
await page.waitForTimeout(400);
check('search filters the grid', (await page.locator('.card').count()) === 2,
  `cards=${await page.locator('.card').count()}`);
await page.fill('#search', '');

// 8. theme toggle
const modes = [await page.getAttribute('html', 'data-theme-mode')];
for (let i = 0; i < 3; i += 1) {
  await page.click('#theme');
  await page.waitForTimeout(250);
  modes.push(await page.getAttribute('html', 'data-theme-mode'));
}
check('theme cycles through all three modes', new Set(modes).size === 3, modes.join(' -> '));

console.log(errors.length ? 'CONSOLE ERRORS:\n' + errors.join('\n') : 'no console errors');
await browser.close();
