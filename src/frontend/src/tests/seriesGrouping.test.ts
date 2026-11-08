import { describe, it, expect } from 'vitest';

import { transformMetadataToBook } from '../utils/bookTransformers';
import { groupEditionBooks, editionSections } from '../utils/editionPresentation';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';
import { seriesBooks, workBooks } from './seriesFixtures';

describe('evidenced work presentation', () => {
  it('keeps work ownership separate through API transformation, grouping and exact Get', () => {
    const book = transformMetadataToBook({
      provider: 'edition_metadata',
      provider_id: 'only-returned-recording',
      title: 'The Primal Hunter',
      authors: ['Zogarth'],
      work_owned: true,
      library: { audiobook: null },
    });
    expect(book.work_owned).toBe(true);
    expect(book.library?.audiobook).toBeNull();
    const before = JSON.stringify(workBooks);
    const groups = groupEditionBooks(workBooks);
    expect(groups.map((group) => group.length)).toEqual([2, 1, 1]);
    expect(groups[0].map((child) => child.library?.audiobook)).toEqual(['owned', undefined]);
    expect(groups[1][0].work_owned).toBe(false);
    expect(groups[2][0].work_owned).toBe(true);
    for (const child of groups.flat()) {
      const payload = buildReleaseDownloadPayload(
        child,
        { source: 'prowlarr', source_id: 'synthetic', title: child.title },
        'audiobook',
      );
      expect(payload.edition_context?.provider_id).toBe(child.provider_id);
      expect(payload).not.toHaveProperty('work_owned');
    }
    expect(JSON.stringify(workBooks)).toBe(before);
  });
  it('groups only returned children and preserves each exact payload and ownership', () => {
    const before = JSON.stringify(seriesBooks);
    const groups = groupEditionBooks(seriesBooks);
    expect(groups.map((g) => g.length)).toEqual([2, 2, 1, 1]);
    expect(groups[0][0]).toBe(seriesBooks[0]);
    expect(groups[0][0].library?.audiobook).toBe('owned');
    expect(groups[0][1].library).toBeUndefined();
    for (const child of groups.flat()) {
      const payload = buildReleaseDownloadPayload(
        child,
        { source: 'prowlarr', source_id: 'synthetic', title: child.title },
        'audiobook',
      );
      expect(payload.edition_context).toEqual({
        version: 1,
        provider: child.provider,
        provider_id: child.provider_id,
      });
    }
    expect(JSON.stringify(seriesBooks)).toBe(before);
  });
  it('groups browsing works without requiring catalog work IDs and leaves Hardcover alone', () => {
    expect(
      groupEditionBooks(seriesBooks.map((b) => ({ ...b, edition_work_id: undefined }))),
    ).toHaveLength(4);
    expect(
      groupEditionBooks(seriesBooks.map((b) => ({ ...b, provider: 'hardcover' }))),
    ).toHaveLength(6);
  });
  it('shows a series only for a matching broad query, using discovered books', () => {
    const sections = editionSections(seriesBooks, 'Harry Potter');
    expect(sections[0].title).toBe('Harry Potter');
    expect(new Set(sections[0].books.map((b) => b.series_position))).toEqual(new Set([4, 6]));
    expect(sections[1].books.map((b) => b.id)).toEqual(['quiz', 'history']);
    const sparse = Object.assign({}, seriesBooks[0], {
      id: 'sparse',
      series_name: undefined,
      edition_series_name: undefined,
      series_position: undefined,
    });
    expect(editionSections([...seriesBooks, sparse], 'Harry Potter')[0].books).toContain(sparse);
    expect(
      editionSections(seriesBooks, 'Harry Potter and the Goblet of Fire')[0].title,
    ).toBeUndefined();
  });
});
