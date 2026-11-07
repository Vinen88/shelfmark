/* oxlint-disable no-new-func, no-empty-function, typescript/no-unsafe-type-assertion, typescript/no-unsafe-call, typescript/no-implied-eval -- Isolated fixture harness; callbacks and minimal responses are synthetic. */
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';

import { describe, it, expect } from 'vitest';

import type { Book } from '../types';
import { cardDisplayFields, editionDetailFields } from '../utils/editionPresentation';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';

const book: Book = {
  id: 'edition_metadata:aggregate:exact',
  provider: 'edition_metadata',
  provider_id: 'aggregate:exact',
  title: 'Dark Age (1 of 3) [Dramatized Adaptation]',
  author: 'Pierce Brown',
  display_fields: [
    { label: 'ASIN', value: 'B0FF5F3BJC' },
    { label: 'Part', value: '1 of 3' },
    { label: 'Production', value: 'GraphicAudio' },
    { label: 'Narrators', value: 'Long Cast '.repeat(500) },
    { label: 'Catalog', value: 'aggregate' },
    { label: 'Metadata incomplete', value: 'deadline '.repeat(500) },
  ],
};

describe('edition card projection', () => {
  it('shows part, production and bounded narrator credits without mutating metadata or identity', () => {
    const before = JSON.stringify(book);
    expect(cardDisplayFields(book).map((f) => f.label)).toEqual([
      'ASIN',
      'Part',
      'Production',
      'Narrators',
    ]);
    expect(cardDisplayFields(book).at(-1)?.value.length).toBeLessThanOrEqual(90);
    expect(editionDetailFields(book).map((f) => f.label)).toEqual([
      'ASIN',
      'Part',
      'Production',
      'Narrators',
    ]);
    expect(JSON.stringify(book)).toBe(before);
    const payload = buildReleaseDownloadPayload(
      book,
      { source: 'prowlarr', source_id: 'release', title: 'Dark Age Part 1' },
      'audiobook',
    );
    expect(payload.edition_context).toEqual({
      version: 1,
      provider: 'edition_metadata',
      provider_id: 'aggregate:exact',
    });
  });
  it('preserves ordinary native display fields', () => {
    const native = { ...book, provider: 'hardcover' };
    expect(cardDisplayFields(native)).toEqual(book.display_fields);
  });
  it('identifies a source listing without borrowing another recording identifier', () => {
    const sparse: Book = {
      ...book,
      provider_id: 'release-without-asin',
      display_fields: [{ label: 'Metadata sources', value: 'AudiobookDB' }],
    };
    const before = JSON.stringify(sparse);
    for (const fields of [cardDisplayFields(sparse), editionDetailFields(sparse)]) {
      expect(fields).toContainEqual({ label: 'Metadata sources', value: 'AudiobookDB' });
      expect(fields).toContainEqual({
        label: 'Recording identifiers',
        value: 'Catalog listing · ASIN/ISBN not supplied',
      });
    }
    expect(
      cardDisplayFields({ ...sparse, isbn_13: '9781234567897' }).some(
        (f) => f.label === 'Recording identifiers',
      ),
    ).toBe(false);
    expect(JSON.stringify(sparse)).toBe(before);
  });
});

// Execute the exact App handler, including its async guards. This does not
// substitute a second implementation of its close/selection behavior.
describe('native detail request lifetime', () => {
  const source = readFileSync(new URL('../App.tsx', import.meta.url), 'utf8');
  function session() {
    const start = source.indexOf('  const handleShowDetails = async');
    const end = source.indexOf('\n  };', start) + 5;
    const code = stripTypeScriptTypes(source.slice(start, end));
    const request = { current: 0 };
    const selected: Array<Book | null> = [];
    const pending: Array<(value: Book) => void> = [];
    const api = () => new Promise<Book>((resolve) => pending.push(resolve));
    const run = new Function(
      'books',
      'detailsRequest',
      'setSelectedBook',
      'isMetadataBook',
      'getMetadataBookInfo',
      'getSourceRecordInfo',
      'showToast',
      `${code}; return handleShowDetails;`,
    )(
      [book, { ...book, id: 'second', provider_id: 'second' }],
      request,
      (value: Book | null) => selected.push(value),
      () => true,
      api,
      api,
      () => {},
    ) as (id: string) => Promise<void>;
    return { run, request, selected, pending };
  }
  it('does not reopen closed details when the fetch resolves', async () => {
    const s = session();
    const task = s.run(book.id);
    s.request.current += 1;
    s.selected.push(null);
    s.pending[0]({ ...book, description: 'late' });
    await task;
    expect(s.selected.at(-1)).toBeNull();
  });
  it('only the latest result may update details', async () => {
    const s = session();
    const first = s.run(book.id);
    const second = s.run('second');
    s.pending[1]({ ...book, description: 'second' });
    await second;
    s.pending[0]({ ...book, description: 'stale first' });
    await first;
    expect(s.selected.at(-1)?.id).toBe('second');
    expect(s.selected.at(-1)?.description).toBe('second');
  });
});
