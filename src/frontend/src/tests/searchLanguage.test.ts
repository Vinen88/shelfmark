/* oxlint-disable no-new-func, no-await-in-loop, typescript/no-unsafe-call, typescript/no-unsafe-type-assertion, typescript/no-implied-eval -- Isolated fixture harness; callbacks and minimal responses are synthetic. */
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';

import { describe, expect, it, vi } from 'vitest';

import { searchMetadata } from '../services/api';
import type { AdvancedFilterState, Book } from '../types';
import { buildSearchQuery } from '../utils/buildSearchQuery';

const filters: AdvancedFilterState = {
  isbn: '',
  author: '',
  title: '',
  lang: ['fr'],
  sort: 'series_order',
  content: '',
  formats: [],
};
const languages = [
  { code: 'en', language: 'English' },
  { code: 'fr', language: 'French' },
];

function session() {
  const source = readFileSync(new URL('../hooks/useSearch.ts', import.meta.url), 'utf8');
  const values: Record<string, unknown> = {};
  const pending: Array<(value: { books: Book[]; hasMore: boolean; totalFound: number }) => void> =
    [];
  const api = vi.fn(() => new Promise((resolve) => pending.push(resolve)));
  const context: Record<string, unknown> = {
    useCallback: (fn: unknown) => fn,
    URLSearchParams,
    searchRequest: { current: 0 },
    searchFieldValues: {},
    contentType: 'audiobook',
    advancedFilters: filters,
    searchMetadata: api,
    showToast: vi.fn(),
    handleSearchError: vi.fn(),
    LANGUAGE_OPTION_DEFAULT: 'default',
    DEFAULT_FORMAT_SELECTION: [],
    onSearchReset: undefined,
    lastSearchParamsRef: { current: null },
    currentPage: 1,
    hasMore: true,
    isLoadingMore: false,
    sortBooksByDownloads: (books: Book[]) => books,
  };
  for (const setter of new Set(source.match(/\bset[A-Z]\w+/g))) {
    context[setter] = (value: unknown) => {
      values[setter] = typeof value === 'function' ? value(values[setter]) : value;
    };
  }
  function handler(name: string, end: string) {
    const start = source.indexOf(`  const ${name} = useCallback(`);
    const code = stripTypeScriptTypes(source.slice(start, source.indexOf(end, start)));
    return new Function(...Object.keys(context), `${code}; return ${name};`)(
      ...Object.values(context),
    ) as (args: unknown) => Promise<void>;
  }
  const run = handler('handleSearch', '\n  const handleResetSearch');
  return {
    values,
    pending,
    api,
    run: (args: unknown) => run({ providerOverride: 'edition_metadata', ...(args as object) }),
    reset: handler('handleResetSearch', '\n  // Load more results'),
    more: handler('loadMore', '\n  return {'),
  };
}

describe('native language selection across metadata searches', () => {
  it('preserves multiple selections in ordinary and View Series query construction', () => {
    for (const searchInput of ['The Primal Hunter', '']) {
      const query = buildSearchQuery({
        searchInput,
        showAdvanced: true,
        advancedFilters: { ...filters, lang: ['en', 'fr'] },
        bookLanguages: languages,
        defaultLanguage: ['en'],
        searchMode: 'universal',
      });
      expect(new URLSearchParams(query).getAll('lang')).toEqual(['en', 'fr']);
    }
  });

  it('serializes explicit languages on the native metadata endpoint', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ books: [], page: 2, has_more: false })));
    vi.stubGlobal('fetch', fetcher);
    try {
      await searchMetadata(
        '',
        40,
        'series_order',
        { series: 'The Primal Hunter' },
        2,
        'audiobook',
        'edition_metadata',
        ['en', 'fr'],
      );
      const url = new URL(String(fetcher.mock.calls[0][0]), 'http://fixture.invalid');
      expect(url.searchParams.get('language')).toBe('en,fr');
      expect(url.searchParams.get('series')).toBe('The Primal Hunter');
      expect(url.searchParams.get('page')).toBe('2');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('rejects stale language responses and preserves the latest result count', async () => {
    const s = session();
    const en = s.run({
      query: 'query=The+Primal+Hunter&lang=en',
      config: null,
      searchMode: 'universal',
    });
    const fr = s.run({
      query: 'query=The+Primal+Hunter&lang=fr',
      config: null,
      searchMode: 'universal',
    });
    s.pending[1]({ books: [{ id: 'french' } as Book], hasMore: false, totalFound: 1 });
    await fr;
    s.pending[0]({ books: [{ id: 'english' } as Book], hasMore: true, totalFound: 40 });
    await en;
    expect(s.values.setBooks).toEqual([{ id: 'french' }]);
    expect(s.values.setTotalFound).toBe(1);
    expect(s.api.mock.calls.map((args) => (args as unknown[])[7])).toEqual([['en'], ['fr']]);
  });

  it('retains the selected language for pagination and ignores an older page after switching', async () => {
    const s = session();
    const first = s.run({
      query: 'query=The+Primal+Hunter&lang=en',
      config: null,
      searchMode: 'universal',
    });
    s.pending[0]({ books: [{ id: 'en-1' } as Book], hasMore: true, totalFound: 40 });
    await first;
    const next = s.more(null);
    expect((s.api.mock.calls[1] as unknown[])[7]).toEqual(['en']);
    const french = s.run({
      query: 'query=The+Primal+Hunter&lang=fr',
      config: null,
      searchMode: 'universal',
    });
    s.pending[2]({ books: [{ id: 'fr-1' } as Book], hasMore: false, totalFound: 1 });
    await french;
    s.pending[1]({ books: [{ id: 'en-2' } as Book], hasMore: true, totalFound: 40 });
    await next;
    expect(s.values.setBooks).toEqual([{ id: 'fr-1' }]);
  });

  it('clears loading state when an outstanding language search is reset or emptied', async () => {
    for (const reset of [true, false]) {
      const s = session();
      const pending = s.run({
        query: 'query=The+Primal+Hunter&lang=en',
        config: null,
        searchMode: 'universal',
      });
      if (reset) await s.reset(null);
      else await s.run({ query: '', config: null, searchMode: 'universal' });
      s.pending[0]({ books: [{ id: 'late' } as Book], hasMore: true, totalFound: 40 });
      await pending;
      expect(s.values.setBooks).toEqual([]);
      expect(s.values.setIsSearching).toBe(false);
      expect(s.values.setIsLoadingMore).toBe(false);
    }
  });

  it('preserves other native providers rather than sending them a comma-separated language option', async () => {
    const s = session();
    const task = s.run({
      query: 'query=Example&lang=en&lang=fr',
      config: null,
      searchMode: 'universal',
      providerOverride: 'googlebooks',
    });
    expect((s.api.mock.calls[0] as unknown[])[7]).toEqual([]);
    s.pending[0]({ books: [], hasMore: false, totalFound: 0 });
    await task;
  });
});
