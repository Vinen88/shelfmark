import { describe, it, expect } from 'vitest';

import { groupEditionBooks, editionSections } from '../utils/editionPresentation';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';
import { seriesBooks } from './seriesFixtures';

describe('evidenced work presentation', () => {
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
  it('does not infer work identity from matching titles or change Hardcover', () => {
    expect(
      groupEditionBooks(seriesBooks.map((b) => ({ ...b, edition_work_id: undefined }))),
    ).toHaveLength(6);
    expect(
      groupEditionBooks(seriesBooks.map((b) => ({ ...b, provider: 'hardcover' }))),
    ).toHaveLength(6);
  });
  it('shows a series only for a matching broad query, using discovered books', () => {
    const sections = editionSections(seriesBooks, 'Harry Potter');
    expect(sections[0].title).toBe('Harry Potter');
    expect(new Set(sections[0].books.map((b) => b.series_position))).toEqual(new Set([4, 6]));
    expect(sections[1].books.map((b) => b.id)).toEqual(['quiz', 'history']);
    expect(
      editionSections(seriesBooks, 'Harry Potter and the Goblet of Fire')[0].title,
    ).toBeUndefined();
  });
});
