// Run from a disposable frontend copy with Playwright installed; never production.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';

const out = process.env.SCREENSHOT_DIR || '/artifacts';
await mkdir(out, { recursive: true });
await writeFile('series-review.html', '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/src/tests/seriesBrowserHarness.tsx"></script></body></html>');
const server = spawn('node', ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1'], { stdio: 'ignore' });
for (let attempt = 0; attempt < 100; attempt++) {
  try { if ((await fetch('http://127.0.0.1:5173/series-review.html')).ok) break; } catch {}
  await new Promise(resolve => setTimeout(resolve, 100));
}
const browser = await chromium.launch({ args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1'
  ? route.continue() : route.abort());
try {
  await page.goto('http://127.0.0.1:5173/series-review.html');
  await page.getByRole('heading', { name: 'Harry Potter', exact: true }).waitFor();
  assert.equal(await page.locator('details').count(), 2);
  assert.equal(await page.locator('details[open]').count(), 0);
  assert.equal(await page.getByRole('button', { name: /^Get releases for/ }).count(), 2);
  await page.locator('details summary').first().click();
  await page.getByRole('button', { name: 'Card view', exact: true }).click();
  await page.screenshot({ path: `${out}/series-grid.png`, fullPage: true, animations: 'disabled' });
  const group = page.locator('details').first();
  await group.getByRole('button', { name: /^Get releases for/ }).last().click();
  const payload = JSON.parse(await page.getByTestId('action').textContent());
  assert.equal(payload.edition_context.provider_id, 'fixture-4-B');
  assert.equal(payload.content_type, 'audiobook');
  await group.locator('article').first().hover();
  await group.getByRole('button', { name: 'Book details' }).first().click();
  await page.locator('body').getByRole('button', { name: 'View series', exact: true }).last().waitFor();
  await page.screenshot({ path: `${out}/series-details.png`, fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: 'View series', exact: true }).last().click();
  assert.equal(await page.getByTestId('action').textContent(), 'series:Harry Potter');
  await page.getByRole('button', { name: 'Compact view', exact: true }).click();
  await page.screenshot({ path: `${out}/series-compact.png`, fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: 'List view', exact: true }).click();
  await page.screenshot({ path: `${out}/series-list.png`, fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Compact view', exact: true }).click();
  assert(await group.getByText('Narrator A', { exact: true }).isVisible());
  assert(await group.getByText('Publisher A', { exact: true }).isVisible());
  await page.screenshot({ path: `${out}/series-mobile.png`, fullPage: true, animations: 'disabled' });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await group.locator('summary').click();
  assert.equal(await group.getAttribute('open'), null);
  assert.deepEqual(errors, []);
  console.log('PASS: grid/compact/list/mobile, expansion, owned children, exact child Get payload, details and series navigation');
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto('http://127.0.0.1:5173/series-review.html?catalog_fixture=1&query=The%20Lantern%20Road');
  const additional = page.locator('details').filter({ hasText: 'Additional catalog matches (3)' });
  await additional.locator('summary').waitFor();
  assert.equal(await additional.getAttribute('open'), null);
  const getButtons = page.getByRole('button', { name: /^Get releases for/ });
  assert.equal(await getButtons.count(), 10);
  for (const [view, width] of [['Card', 1280], ['Compact', 1280], ['List', 1280], ['Compact', 390], ['List', 390]]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.getByRole('button', { name: `${view} view`, exact: true }).click();
    assert.equal(await getButtons.count(), 10);
    const mainIds = [];
    for (let index = 0; index < 10; index++) {
      await getButtons.nth(index).click();
      mainIds.push(JSON.parse(await page.getByTestId('action').textContent()).edition_context.provider_id);
    }
    assert.deepEqual(mainIds, Array.from({ length: 10 }, (_, index) => `identified-${index + 1}`));
    await additional.locator('summary').focus();
    await page.keyboard.press('Enter');
    await additional.getByText('that relationship is unverified.', { exact: false }).waitFor();
    assert.equal(await getButtons.count(), 13);
    assert.equal(await additional.getByText('Owned', { exact: true }).count(), 1);
    const extraButtons = additional.getByRole('button', { name: /^Get releases for/ });
    for (const [index, position] of [1, 3, 10].entries()) {
      await extraButtons.nth(index).click();
      assert.equal(JSON.parse(await page.getByTestId('action').textContent()).edition_context.provider_id, `catalog-${position}`);
    }
    const detail = additional.getByRole('button', { name: /Book details|View details|^Details$/ }).first();
    if (view !== 'List') await additional.locator('article').first().hover();
    await detail.click();
    const modal = page.getByRole('dialog');
    await modal.waitFor();
    assert(await modal.getByText('Catalog listing · ASIN/ISBN not supplied', { exact: true }).isVisible());
    await page.keyboard.press('Escape');
    await modal.waitFor({ state: 'hidden' });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({ path: `${out}/catalog-${view.toLowerCase()}-${width}.png`, fullPage: true, animations: 'disabled' });
    await additional.locator('summary').click();
    assert.equal(await additional.getAttribute('open'), null);
  }
  assert.deepEqual(errors, []);
  console.log('PASS: ten prominent releases, three collapsed catalog matches, keyboard expansion, exact Get/Details and ownership in all layouts');
  if (process.env.METADATA_FIXTURE_PATH) {
    const fixture = JSON.parse(await readFile(process.env.METADATA_FIXTURE_PATH, 'utf8'));
    let calls = 0;
    await page.route('**/fixture-cover.svg', route => route.fulfill({ contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="#406482"/><text x="30" y="100" fill="white">Fixture cover</text></svg>' }));
    await page.route('**/api/metadata/search*', route => { calls++; return route.fulfill({ json: fixture }); });
    const query = fixture.books[0].edition_series_name;
    await page.goto(`http://127.0.0.1:5173/series-review.html?api_fixture=1&query=${encodeURIComponent(query)}`);
    await page.getByText(`Discovered books in series order · ${fixture.books.length} catalog listings returned`).waitFor();
    await page.locator('details:not([open]) summary').evaluateAll(elements => elements.forEach(element => element.click()));
    const buttons = page.getByRole('button', { name: /^Get releases for/ });
    assert.equal(await buttons.count(), fixture.books.length);
    const selected = [];
    for (let n = 0; n < fixture.books.length; n++) {
      await buttons.nth(n).click();
      selected.push(JSON.parse(await page.getByTestId('action').textContent()).edition_context.provider_id);
    }
    assert.deepEqual([...selected].sort(), fixture.books.map(book => book.provider_id).sort());
    assert.equal(new Set(selected).size, fixture.books.length);
    assert.equal(calls, 1, 'One completed metadata response');
    const unidentified = fixture.books.filter(book => !book.isbn_10 && !book.isbn_13
      && !book.display_fields.some(field => field.label === 'ASIN' && field.value.trim()));
    assert.equal(await page.getByText('Catalog listing · ASIN/ISBN not supplied', { exact: true }).count(), unidentified.length);
    assert.equal(await page.getByText('Owned', { exact: true }).count(),
      fixture.books.filter(book => book.library?.audiobook === 'owned').length);
    await page.screenshot({ path: `${out}/captured-results.png`, fullPage: true, animations: 'disabled' });
    assert.deepEqual(errors, []);
    console.log(`PASS: private API replay ${fixture.books.length} backend rows = ${selected.length} frontend children; exact Get IDs`);
  }
  await page.goto('http://127.0.0.1:5173/series-review.html?work_fixture=1&query=The%20Primal%20Hunter');
  const workGroups = page.locator('details');
  await workGroups.first().locator('summary').waitFor();
  assert.equal(await workGroups.count(), 2, 'mixed recordings and owned singleton each have a work parent');
  for (const [view, width] of [['Card', 1280], ['Compact', 1280], ['List', 1280], ['Compact', 390], ['List', 390]]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.getByRole('button', { name: `${view} view`, exact: true }).click();
    for (const group of await workGroups.all()) {
      assert.equal(await group.locator('summary').getByText('In library', { exact: true }).count(), 1);
      if (await group.getAttribute('open') === null) await group.locator('summary').click();
    }
    assert.equal(await workGroups.first().getByText('Owned', { exact: true }).count(), 1);
    assert.equal(await workGroups.last().getByText('Owned', { exact: true }).count(), 0,
      'ABS recording absent from catalog does not create an owned child');
    assert.equal(await page.locator('summary').count(), 2, 'unowned singleton stays available');
    const buttons = page.getByRole('button', { name: /^Get releases for/ });
    assert.equal(await buttons.count(), 4);
    const identities = [];
    for (const button of await buttons.all()) {
      await button.click();
      identities.push(JSON.parse(await page.getByTestId('action').textContent()).edition_context.provider_id);
    }
    assert.deepEqual(identities, ['primal-1-owned', 'primal-1-available', 'primal-2-available', 'primal-3-available']);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({ path: `${out}/work-${view}-${width}.png`, fullPage: true, animations: 'disabled' });
  }
  assert.deepEqual(errors, []);
  console.log('PASS: Primal Hunter work badges, mixed ownership, absent recording, unowned/single results and exact Get in grid/list/mobile');
} catch (error) {
  await page.screenshot({ path: `${out}/failure.png`, fullPage: true, animations: 'disabled' });
  throw error;
} finally {
  await browser.close();
  server.kill();
}
