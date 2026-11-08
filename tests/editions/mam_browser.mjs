// Synthetic responses through the actual release modal; all external traffic blocked.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
const out = process.env.SCREENSHOT_DIR || '/artifacts';
await mkdir(out, { recursive: true });
await writeFile('mam-review.html', '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/src/tests/mamBrowserHarness.tsx"></script></body></html>');
const server = spawn('node', ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1'], { stdio: 'ignore' });
for (let n = 0; n < 100; n++) {
  try { if ((await fetch('http://127.0.0.1:5173/mam-review.html')).ok) break; } catch {}
  await new Promise(resolve => setTimeout(resolve, 100));
}
const browser = await chromium.launch({ args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [], searches = [];
let delayedSearch;
page.on('pageerror', error => errors.push(error.message));
const release = { source: 'prowlarr', source_id: '7:fixture-123', title: 'Synthetic MAM Dark Age Part 1',
  indexer: 'MAM fixture', content_type: 'audiobook', format: 'm4b', language: 'en',
  protocol: 'torrent', seeders: 5, extra: { formats: ['m4b'], narrator: 'Fixture cast' } };
await page.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.hostname !== '127.0.0.1') return route.abort();
  if (url.pathname === '/api/releases/inspect' && request.method() === 'POST')
    return route.fulfill({ json: { inspected: false, files: [], plan: null, reason: 'synthetic fixture' } });
  if (request.method() !== 'GET') return route.abort();
  if (url.pathname === '/api/release-sources') return route.fulfill({ json: [{ name: 'prowlarr', display_name: 'Prowlarr', enabled: true, supported_content_types: ['audiobook'] }] });
  if (url.pathname === '/api/releases') {
    searches.push(Object.fromEntries(url.searchParams));
    if (url.searchParams.get('book_id') === 'fixture-part-2') {
      await new Promise(resolve => { delayedSearch = resolve; });
    }
    return route.fulfill({ json: { releases: [release, { ...release, source_id: '7:fixture-unsupported',
      title: 'Unsupported FLAC fixture', format: 'flac', extra: { formats: ['flac'] } }],
      sources_searched: ['prowlarr'], search_info: { prowlarr: { search_type: 'categories' } },
      column_config: { columns: [], grid_template: 'minmax(0,1fr)', supported_filters: ['format', 'language', 'indexer'],
        available_indexers: ['MAM fixture'], supported_formats: ['m4b', 'mp3'], action_button: { label: 'Expand search' } } } });
  }
  if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
  return route.continue();
});
try {
  await page.goto('http://127.0.0.1:5173/mam-review.html');
  assert.equal(searches.length, 0);
  await page.locator('details summary').click();
  for (const label of ['List view', 'Compact view', 'Card view']) {
    await page.getByRole('button', { name: label, exact: true }).click();
  }
  await page.getByTestId('rerender').click();
  assert.equal(searches.length, 0, 'Grouping, views and parent renders do not search releases');
  await page.getByRole('button', { name: /^Get this edition releases for/ }).first().click();
  await page.getByText(release.title, { exact: true }).first().waitFor();
  assert.equal(await page.getByText(release.title, { exact: true }).filter({ visible: true }).count(), 1);
  assert.equal(await page.getByText('Unsupported FLAC fixture', { exact: true }).count(), 0);
  assert.equal(searches[0].book_id, 'fixture-part-1');
  assert.equal(searches[0].provider, 'manual');
  assert.equal(searches[0].title, 'Dark Age Part 1');
  assert.equal(searches[0].content_type, 'audiobook');
  assert.equal(searches.length, 1);
  for (const id of ['rerender', 'late-metadata', 'rerender']) {
    await page.getByTestId(id).evaluate(button => button.click());
  }
  await page.waitForTimeout(150);
  assert.equal(searches.length, 1, 'Display metadata and parent renders preserve the search session');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: /^Get this edition releases for/ }).first().click();
  await page.getByText(release.title, { exact: true }).first().waitFor();
  assert.equal(searches.length, 1, 'Reopening a completed release search uses native UI cache');
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${out}/mam-release-modal.png`, fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: 'Expand search', exact: true }).click();
  await page.waitForFunction(() => !document.body.textContent.includes('Searching...'));
  await page.getByRole('button', { name: 'Manual search query' }).click();
  await page.getByRole('textbox', { name: 'Custom search query' }).fill('Dark Age Part 1');
  await page.getByRole('textbox', { name: 'Custom search query' }).press('Enter');
  await page.waitForTimeout(300);
  assert(searches.some(s => s.expand_search === 'true'));
  assert(searches.some(s => s.manual_query === 'Dark Age Part 1'));
  assert.equal(searches.length, 3, 'Only explicit expanded and manual searches add requests');
  await page.getByRole('button', { name: /^Download/ }).last().click();
  await page.waitForFunction(() => document.querySelector('[data-testid="payload"]').textContent.length > 0);
  const payload = JSON.parse(await page.getByTestId('payload').textContent());
  assert.equal(payload.edition_context.provider_id, 'fixture-part-1');
  assert.equal(payload.source_id, release.source_id);
  assert.equal(payload.content_type, 'audiobook');
  await page.keyboard.press('Escape');
  const childRequest = page.waitForRequest(request => request.url().includes('/api/releases?') && request.url().includes('fixture-part-2'));
  await page.getByRole('button', { name: /^Get this edition releases for/ }).nth(1).click();
  await childRequest;
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: /^Get this edition releases for/ }).first().click();
  await page.getByText(release.title, { exact: true }).first().waitFor();
  assert.equal(searches.length, 4, 'Switching back uses part 1 cache while part 2 is pending');
  delayedSearch();
  await page.waitForTimeout(150);
  assert.equal(searches.length, 4, 'A stale response does not start another release search');
  await page.getByRole('button', { name: /^Download/ }).last().click();
  assert.equal(JSON.parse(await page.getByTestId('payload').textContent()).edition_context.provider_id, 'fixture-part-1');
  assert.deepEqual(errors, []);
  console.log('PASS: release API counts: browse/group/views=0, open=1, rerender/late display/reopen=0 extra, expand=1 extra, manual=1 extra, other child=1 extra; stale response=0 extra; exact child payload; no downloads');
} catch (error) {
  await page.screenshot({ path: `${out}/mam-failure.png`, fullPage: true });
  throw error;
} finally { await browser.close(); server.kill(); }
