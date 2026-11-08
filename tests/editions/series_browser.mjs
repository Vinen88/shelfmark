// Real cards and payload builders; externally isolated disposable Chromium only.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';

const out = process.env.SCREENSHOT_DIR || '/artifacts';
await mkdir(out, { recursive: true });
await writeFile('series-review.html', '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/src/tests/seriesBrowserHarness.tsx"></script></body></html>');
const server = spawn('node', ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1'], { stdio: 'ignore' });
for (let n = 0; n < 100; n++) {
  try { if ((await fetch('http://127.0.0.1:5173/series-review.html')).ok) break; } catch {}
  await new Promise(resolve => setTimeout(resolve, 100));
}
const browser = await chromium.launch({ args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
try {
  for (const [fixture, query, parents, children, ownedWorks, ownedEditions] of [
    ['', 'Harry Potter', 4, 6, 2, 2],
    ['catalog_fixture', 'The Lantern Road', 10, 13, 1, 1],
    ['work_fixture', 'The Primal Hunter', 3, 4, 2, 1],
    ['last_battle_fixture', 'The Last Battle', 1, 4, 1, 0],
  ]) {
    await page.goto(`http://127.0.0.1:5173/series-review.html?${fixture}=1&query=${encodeURIComponent(query)}`);
    await page.locator('details summary').first().waitFor();
    for (const [view, width] of [['Card', 1280], ['Compact', 1280], ['List', 1280], ['Compact', 390], ['List', 390]]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.getByRole('button', { name: `${view} view`, exact: true }).click();
      const disclosures = page.locator('details');
      for (const disclosure of await disclosures.all()) {
        if (await disclosure.getAttribute('open') !== null) await disclosure.locator('summary').click();
      }
      assert.equal(await disclosures.count(), parents);
      const getBooks = page.getByRole('button', { name: /^Get book releases for/ });
      const getEditions = page.getByRole('button', { name: /^Get this edition releases for/ });
      assert.equal(await getBooks.count(), parents);
      assert.equal(await getEditions.count(), 0);
      assert.equal(await page.getByText('In library', { exact: true }).count(), ownedWorks);
      await getBooks.first().click();
      let payload = JSON.parse(await page.getByTestId('action').textContent());
      assert(payload.work_context && !payload.edition_context);
      assert.equal(payload.content_type, 'audiobook');
      const candidates = payload.work_context.candidate_ids;
      assert(candidates.includes(payload.work_context.provider_id));
      for (const disclosure of await disclosures.all()) {
        await disclosure.locator('summary').focus();
        await page.keyboard.press('Enter');
      }
      assert.equal(await getEditions.count(), children);
      assert.equal(await page.getByText('Owned', { exact: true }).count(), ownedEditions);
      const selected = [];
      for (const button of await getEditions.all()) {
        await button.click();
        payload = JSON.parse(await page.getByTestId('action').textContent());
        assert(payload.edition_context && !payload.work_context);
        selected.push(payload.edition_context.provider_id);
      }
      assert.equal(new Set(selected).size, children, 'Every returned exact recording remains selectable');
      assert(candidates.every(id => selected.includes(id)));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      await page.screenshot({ path: `${out}/book-first-${fixture || 'series'}-${view}-${width}.png`, fullPage: parents < 5, animations: 'disabled' });
    }
    console.log(`PASS ${query}: ${parents} literary parents, ${children} exact children, work/edition ownership, Get intentions and keyboard expansion in every layout`);
  }
  let refreshes = 0;
  await page.route('**/api/metadata/ownership', async route => {
    const { ids } = route.request().postDataJSON();
    assert.equal(ids.length, 4, 'One batch for all visible recordings');
    refreshes++;
    await route.fulfill({ json: { books: Object.fromEntries(ids.map(id => [id, {
      work_owned: !id.startsWith('primal-2-') || refreshes > 1,
      library: { audiobook: id.endsWith('-owned') ? 'owned' : null },
    }])) } });
  });
  await page.goto('http://127.0.0.1:5173/series-review.html?work_fixture=1&query=The%20Primal%20Hunter');
  await page.locator('details summary').first().waitFor();
  assert.equal(refreshes, 0, 'Browsing never polls ownership per book');
  await page.getByTestId('complete').evaluate(button => button.click());
  for (let wait = 0; refreshes < 1 && wait < 400; wait++) await new Promise(resolve => setTimeout(resolve, 25));
  assert(refreshes >= 1, 'Completion triggers an ownership batch');
  assert.equal(await page.getByText('In library', { exact: true }).count(), 2,
    'Completion/scan acknowledgement alone does not make book 2 owned');
  for (let wait = 0; refreshes < 2 && wait < 400; wait++) await new Promise(resolve => setTimeout(resolve, 25));
  assert(refreshes >= 2, 'Async ingestion receives a bounded follow-up refresh');
  await page.waitForFunction(() => [...document.querySelectorAll('span')].filter(element => element.textContent === 'In library').length === 3);
  for (const disclosure of await page.locator('details').all()) await disclosure.locator('summary').click();
  assert.equal(await page.getByText('Owned', { exact: true }).count(), 1, 'Confirmed work import never propagates recording ownership');
  console.log('PASS native completion hook: batched async ownership refresh, no premature ownership and no child propagation');
  assert.deepEqual(errors, []);
} catch (error) {
  await page.screenshot({ path: `${out}/book-first-failure.png`, fullPage: true, animations: 'disabled' });
  throw error;
} finally {
  await browser.close();
  server.kill();
}
