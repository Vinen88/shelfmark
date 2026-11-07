import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, it, expect } from 'vitest';

import { MetadataAttribution } from '../components/MetadataAttribution';
import type { Book } from '../types';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';

const book: Book = {
  id: 'exact-part-2',
  provider: 'edition_metadata',
  provider_id: 'aggregate:exact-part-2',
  title: 'Lantern Part 2 of 3',
  author: 'Example Author',
  display_fields: [{ label: 'Metadata sources', value: 'AudiobookDB, Audnexus' }],
};

describe('metadata attribution', () => {
  it('renders fixed, named catalog links without exposing recording IDs', () => {
    const markup = renderToStaticMarkup(
      createElement(MetadataAttribution, { books: [book, book] }),
    );
    expect(markup).toContain('href="https://audiobookdb.org/"');
    expect(markup).toContain('href="https://github.com/laxamentumtech/audnexus"');
    expect(markup).not.toContain('exact-part-2');
    expect(markup.match(/href=/g)?.length).toBe(2);
  });
  it('does not attribute ordinary providers or sources that returned no data', () => {
    for (const candidate of [
      { ...book, provider: 'hardcover' },
      { ...book, display_fields: [] },
    ]) {
      expect(renderToStaticMarkup(createElement(MetadataAttribution, { books: [candidate] }))).toBe(
        '',
      );
    }
  });
  it('preserves the exact recording payload and input metadata', () => {
    const before = JSON.stringify(book);
    renderToStaticMarkup(createElement(MetadataAttribution, { books: [book] }));
    const payload = buildReleaseDownloadPayload(
      book,
      { source: 'prowlarr', source_id: 'fixture', title: book.title },
      'audiobook',
    );
    expect(payload.edition_context).toEqual({
      version: 1,
      provider: 'edition_metadata',
      provider_id: 'aggregate:exact-part-2',
    });
    expect(JSON.stringify(book)).toBe(before);
  });
});
