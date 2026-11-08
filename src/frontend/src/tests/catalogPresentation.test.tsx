/* oxlint-disable no-empty-function -- Synthetic render callbacks. */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, it, expect } from 'vitest';

import { ResultsSection } from '../components/ResultsSection';
import { SearchModeProvider } from '../contexts/SearchModeContext';
import type { Book } from '../types';
import { groupEditionBooks, workBook, editionButtonState } from '../utils/editionPresentation';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';
import { bookFromRequestData } from '../utils/requestFulfil';
import { buildMetadataBookRequestData } from '../utils/requestPayload';
import { catalogBooks } from './catalogFixtures';

const lastBattle: Book = {
  id: 'edition_metadata:last-battle-ca',
  provider: 'edition_metadata',
  provider_id: 'last-battle-ca',
  title: 'The Last Battle',
  edition_work_title: 'The Last Battle',
  author: 'C. S. Lewis',
  authors: ['C. S. Lewis'],
  series_name: 'The Chronicles of Narnia',
  series_position: 7,
  content_type: 'audiobook',
};

describe('book-first browsing without recording equivalence', () => {
  it('keeps an unconfirmed recording available after a completed or unidentified transfer', () => {
    expect(editionButtonState(lastBattle, { text: 'Downloaded', state: 'complete' }).state).toBe(
      'download',
    );
    expect(
      editionButtonState(Object.assign({}, lastBattle, { library: { audiobook: 'owned' } }), {
        text: 'Downloaded',
        state: 'complete',
      }).state,
    ).toBe('complete');
    expect(
      editionButtonState(lastBattle, { text: 'Downloading', state: 'downloading' }).state,
    ).toBe('downloading');
    expect(
      editionButtonState(workBook([lastBattle]), { text: 'Downloaded', state: 'complete' }).state,
    ).toBe('complete');
  });
  it('groups different work IDs, regions, narrators and missing IDs under one literary book', () => {
    const books = [
      lastBattle,
      Object.assign({}, lastBattle, {
        id: 'other',
        provider_id: 'other',
        edition_work_id: 'audiobookdb:different',
        publisher: 'Different publisher',
        year: '2026',
      }),
      Object.assign({}, lastBattle, {
        id: 'missing',
        provider_id: 'missing',
        series_name: undefined,
      }),
    ];
    expect(groupEditionBooks(books)).toEqual([books]);
    for (const order of [books, [...books].toReversed()]) {
      expect(workBook(groupEditionBooks(order)[0]).id).toBe(workBook(books).id);
    }
    const before = JSON.stringify(books);
    const parent = workBook(books);
    expect(parent.title).toBe('The Last Battle');
    expect(parent.download_intent).toBe('work');
    expect(parent.edition_candidate_ids).toEqual(['last-battle-ca', 'other', 'missing']);
    expect(parent.publisher).toBeUndefined();
    expect(parent.isbn_13).toBeUndefined();
    expect(JSON.stringify(books)).toBe(before);
  });
  it('separates different authors and conflicting explicit literary evidence', () => {
    for (const change of [
      { author: 'Another Author', authors: ['Another Author'] },
      { series_name: 'Different Series' },
      { series_position: 1 },
      { edition_work_conflicts: ['series_position'] },
    ]) {
      expect(
        groupEditionBooks([lastBattle, Object.assign({}, lastBattle, change, { id: 'other' })]),
      ).toHaveLength(2);
    }
  });
  it('reconciles pagination while preserving every original catalog object and exact selection', () => {
    const groups = groupEditionBooks(catalogBooks);
    expect(groups).toHaveLength(10);
    expect(groups.flat()).toHaveLength(13);
    const firstPage = groupEditionBooks(catalogBooks.slice(0, 6));
    expect(groups[0]).toContain(firstPage[0][0]);
    for (const child of groups.flat()) {
      expect(catalogBooks).toContain(child);
      expect(
        buildReleaseDownloadPayload(
          child,
          { source: 'prowlarr', source_id: 'native-release', title: child.title },
          'audiobook',
        ).edition_context?.provider_id,
      ).toBe(child.provider_id);
    }
  });
  it('keeps generic and exact release payloads distinct through requests and fulfilment', () => {
    const parent = workBook([lastBattle]);
    const release = {
      source: 'audiobookbay',
      source_id: 'native-id',
      title: 'Any recording of the book',
    };
    const payload = buildReleaseDownloadPayload(parent, release, 'audiobook');
    expect(payload.work_context).toEqual({
      provider_id: lastBattle.provider_id,
      candidate_ids: [lastBattle.provider_id],
    });
    expect(payload.edition_context).toBeUndefined();
    expect(payload.source_id).toBe('native-id');
    const request = buildMetadataBookRequestData(parent, 'audiobook');
    const restored = bookFromRequestData(request);
    expect(restored.download_intent).toBe('work');
    expect(buildReleaseDownloadPayload(restored, release, 'audiobook').work_context).toEqual(
      payload.work_context,
    );
    expect(
      buildReleaseDownloadPayload(lastBattle, release, 'audiobook').work_context,
    ).toBeUndefined();
  });
  it('renders one main card per work with all recordings in accessible disclosures', () => {
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
    expect(markup.match(/<details\b/g)).toHaveLength(10);
    expect(markup.match(/aria-label="Get book releases for/g)).toHaveLength(10);
    expect(markup.match(/aria-label="Get this edition releases for/g)).toHaveLength(13);
    expect(markup).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/);
    expect(markup).not.toContain('Additional catalog matches');
    expect(markup).toContain('In library');
    expect(markup).toContain('Owned');
  });
});
