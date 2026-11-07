import type { AdvancedFilterState, Language } from '../types';
import { getLanguageFilterValues } from './languageFilters';

interface BuildSearchQueryOptions {
  searchInput: string;
  showAdvanced: boolean;
  advancedFilters: AdvancedFilterState;
  bookLanguages: Language[];
  defaultLanguage: string[];
  searchMode?: 'direct' | 'universal';
}

export const buildSearchQuery = ({
  searchInput,
  showAdvanced: _showAdvanced,
  advancedFilters,
  bookLanguages,
  defaultLanguage,
  searchMode = 'direct',
}: BuildSearchQueryOptions): string => {
  const queryParts: string[] = [];

  const basic = searchInput.trim();
  if (basic) {
    queryParts.push(`query=${encodeURIComponent(basic)}`);
  }

  const selectedLanguages = getLanguageFilterValues(
    advancedFilters.lang,
    bookLanguages,
    defaultLanguage,
  );
  selectedLanguages?.forEach((code) => queryParts.push(`lang=${encodeURIComponent(code)}`));

  // Universal mode shares the native language preference with direct searches.
  // Provider-specific fields are handled separately via searchFieldValues
  if (searchMode === 'universal') {
    if (advancedFilters.sort) {
      queryParts.push(`sort=${encodeURIComponent(advancedFilters.sort)}`);
    }
    return queryParts.join('&');
  }

  // Direct mode: include the active direct search settings regardless of panel visibility.
  const { isbn, author, title, content, formats } = advancedFilters;

  if (isbn) queryParts.push(`isbn=${encodeURIComponent(isbn)}`);
  if (author) queryParts.push(`author=${encodeURIComponent(author)}`);
  if (title) queryParts.push(`title=${encodeURIComponent(title)}`);

  if (content) queryParts.push(`content=${encodeURIComponent(content)}`);
  formats.forEach((format) => queryParts.push(`format=${encodeURIComponent(format)}`));

  // "downloads" is a client-side-only sort — send empty string so the server
  // doesn't receive an invalid sort parameter.
  const serverSort = advancedFilters.sort === 'downloads' ? '' : advancedFilters.sort;
  if (serverSort) {
    queryParts.push(`sort=${encodeURIComponent(serverSort)}`);
  }

  return queryParts.join('&');
};
