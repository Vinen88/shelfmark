/* oxlint-disable no-empty-function -- Isolated fixture harness; callbacks and minimal responses are synthetic. */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, it, expect } from 'vitest';

import { ResultsSection } from '../components/ResultsSection';
import { SearchModeProvider } from '../contexts/SearchModeContext';
import type { Book } from '../types';
import { editionSections, partitionEditionBooks } from '../utils/editionPresentation';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';
import { catalogBooks } from './catalogFixtures';

const [catalog, identified] = catalogBooks;
describe('additional unidentified catalog matches', () => {
  it('keeps ten identified releases ordered and all thirteen exact objects selectable', () => {
    const before = JSON.stringify(catalogBooks);
    const result = partitionEditionBooks(catalogBooks);
    expect(result.books.map((b) => b.series_position)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(result.additional.map((b) => b.series_position)).toEqual([1, 3, 10]);
    expect(result.additional[1].library?.audiobook).toBe('owned');
    for (const child of [...result.books, ...result.additional]) {
      expect(catalogBooks.find((b) => b.id === child.id)).toBe(child);
      expect(
        buildReleaseDownloadPayload(
          child,
          { source: 'prowlarr', source_id: 'fixture', title: child.title },
          'audiobook',
        ).edition_context?.provider_id,
      ).toBe(child.provider_id);
    }
    expect(JSON.stringify(catalogBooks)).toBe(before);
    const reversed = partitionEditionBooks([...catalogBooks].toReversed());
    expect(reversed.books).toEqual([...result.books].toReversed());
    expect(reversed.additional).toEqual([...result.additional].toReversed());
  });

  it('preserves AudiobookDB-only discovery, including unidentified and ISBN-only releases', () => {
    const isbnRelease = {
      ...catalog,
      id: 'non-audible',
      provider_id: 'non-audible',
      isbn_13: '9781234567897',
    };
    for (const books of [[catalog], [catalog, isbnRelease], [isbnRelease, identified]]) {
      const result = partitionEditionBooks(books);
      expect(result.books).toContain(books.includes(isbnRelease) ? isbnRelease : catalog);
    }
    const unknownOnly = catalogBooks.filter((b) => b.provider_id?.startsWith('catalog-'));
    expect(partitionEditionBooks(unknownOnly)).toEqual({ books: unknownOnly, additional: [] });
    expect(
      partitionEditionBooks([{ ...catalog, isbn_10: '123456789X' }, identified]).additional,
    ).toEqual([]);
    expect(
      partitionEditionBooks([
        catalog,
        { ...identified, display_fields: [{ label: 'ASIN', value: ' ' }] },
      ]).additional,
    ).toEqual([]);
  });

  it('requires work evidence, never title similarity or a source-specific rule', () => {
    const changes: Partial<Book>[] = [
      { edition_work_id: undefined },
      { edition_work_title: 'Another title' },
      { edition_work_title: undefined },
      { edition_series_name: 'Another series' },
      { edition_series_name: undefined },
      { series_position: 2 },
      { series_position: undefined },
      { authors: ['Another Author'] },
      { authors: [] },
      { provider: 'hardcover' },
    ];
    for (const change of changes) {
      expect(partitionEditionBooks([{ ...catalog, ...change }, identified]).additional).toEqual([]);
    }
    expect(
      partitionEditionBooks([catalog, { ...identified, provider: 'hardcover' }]).additional,
    ).toEqual([]);
    const sameWork = {
      ...catalog,
      edition_work_id: identified.edition_work_id,
      edition_series_name: undefined,
    };
    expect(partitionEditionBooks([sameWork, identified]).additional).toEqual([sameWork]);
    const otherSource = {
      ...catalog,
      display_fields: [],
      provider_id: 'another-catalog',
      authors: ['Example AUTHOR'],
      edition_work_title: 'The Lantern Road: 1',
    };
    expect(partitionEditionBooks([otherSource, identified]).additional).toEqual([otherSource]);
  });

  it('retains all identified performances and GraphicAudio parts without regrouping them', () => {
    const parts = [1, 2].map((part) =>
      Object.assign({}, identified, {
        id: `part-${part}`,
        provider_id: `part-${part}`,
        display_fields: [
          { label: 'ASIN', value: `B00000000${part}` },
          { label: 'Part', value: `${part} of 2` },
        ],
      }),
    );
    expect(partitionEditionBooks([...parts, catalog])).toEqual({
      books: parts,
      additional: [catalog],
    });
    expect(editionSections(catalogBooks, 'The Lantern Road')[0].additional).toHaveLength(3);
    expect(
      editionSections(
        [catalog, { ...identified, edition_series_name: 'Another series' }],
        'The Lantern Road',
      )[0].books,
    ).toEqual([catalog]);
  });

  it('renders a closed, clearly qualified disclosure with unchanged child controls and total count', () => {
    const markup = renderToStaticMarkup(
      <SearchModeProvider searchMode="universal">
        <ResultsSection
          books={catalogBooks}
          visible
          query="The Lantern Road"
          sortValue="relevance"
          onSortChange={() => {}}
          onDetails={async () => {}}
          onDownload={async () => {}}
          onGetReleases={async () => {}}
          getButtonState={() => ({ text: 'Get', state: 'download' })}
          getUniversalButtonState={() => ({ text: 'Get', state: 'download' })}
        />
      </SearchModeProvider>,
    );
    expect(markup).toContain('13 catalog listings returned');
    expect(markup).toContain('Additional catalog matches (3)');
    expect(markup).toContain('that relationship is unverified');
    expect(markup).toContain('Includes owned recordings');
    expect(markup.match(/<details\b/g)).toHaveLength(1);
    expect(markup).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/);
    expect(markup.match(/aria-label="Get releases for/g)).toHaveLength(13);
    expect(markup).toContain('B000000010');
    expect(markup.indexOf('Additional catalog matches')).toBeGreaterThan(
      markup.indexOf('B000000010'),
    );
  });
});
