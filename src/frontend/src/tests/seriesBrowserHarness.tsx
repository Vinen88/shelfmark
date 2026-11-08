/* oxlint-disable import/no-unassigned-import, no-empty-function -- Isolated fixture harness; callbacks and minimal responses are synthetic. */
// Disposable browser fixture: real components; callbacks never contact download clients.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';

import { DetailsModal } from '../components/DetailsModal';
import { ResultsSection } from '../components/ResultsSection';
import { SearchModeProvider } from '../contexts/SearchModeContext';
import { useStatusChangeNotifications } from '../hooks/app/useStatusChangeNotifications';
import { useMountEffect } from '../hooks/useMountEffect';
import { searchMetadata, refreshEditionOwnership } from '../services/api';
import type { Book, ButtonStateInfo, StatusData } from '../types';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';
import { catalogBooks } from './catalogFixtures';
import { seriesBooks, workBooks, lastBattleBooks } from './seriesFixtures';

import '../styles.css';

const state: ButtonStateInfo = { text: 'Get', state: 'download' };
function Harness() {
  const [book, setBook] = useState<Book | null>(null);
  const [query, setQuery] = useState(
    new URLSearchParams(location.search).get('query') || 'Harry Potter',
  );
  const [books, setBooks] = useState(() => {
    const params = new URLSearchParams(location.search);
    if (params.has('catalog_fixture')) return catalogBooks;
    if (params.has('last_battle_fixture')) return lastBattleBooks;
    return params.has('work_fixture') ? workBooks : seriesBooks;
  });
  useMountEffect(() => {
    if (new URLSearchParams(location.search).has('api_fixture')) {
      void searchMetadata(query, 40, 'relevance', {}, 1, 'audiobook', 'edition_metadata').then(
        (result) => setBooks(result.books),
      );
    }
  });
  const [action, setAction] = useState('');
  const [status, setStatus] = useState<StatusData>({ queued: {} });
  useStatusChangeNotifications({
    currentStatus: status,
    config: null,
    showToast: () => {},
    openDownloadsSidebar: () => {},
    bookToReleaseMap: {},
    markBookCompleted: () => {},
    refreshOwnership: () => {
      void refreshEditionOwnership(
        books.flatMap((child) => (child.provider_id ? [child.provider_id] : [])),
      ).then(({ books: holdings }) => {
        setBooks((current) =>
          current.map((child) => Object.assign({}, child, holdings[child.provider_id || ''] || {})),
        );
      });
    },
  });
  const get = async (child: Book) =>
    setAction(
      JSON.stringify(
        buildReleaseDownloadPayload(
          child,
          { source: 'prowlarr', source_id: 'fixture-release', title: child.title },
          'audiobook',
        ),
      ),
    );
  const series = (name: string) => {
    setQuery(name);
    setBook(null);
    setAction(`series:${name}`);
  };
  return (
    <SearchModeProvider searchMode="universal">
      <main className="mx-auto max-w-7xl p-5">
        <h1 className="mb-5 text-2xl font-bold">Audiobook search: {query}</h1>
        <button
          type="button"
          className="hidden"
          data-testid="complete"
          onClick={() =>
            setStatus({
              complete: {
                fixture: {
                  id: 'fixture',
                  title: query,
                  author: 'Fixture',
                  content_type: 'audiobook',
                },
              },
            })
          }
        >
          Complete synthetic import
        </button>
        <ResultsSection
          books={books}
          visible
          query={query}
          sortValue="relevance"
          onSortChange={() => {}}
          onDetails={async (id) => setBook(books.find((b) => b.id === id) || null)}
          onDownload={get}
          onGetReleases={get}
          getButtonState={() => state}
          getUniversalButtonState={() => state}
          onViewChange={() => setBook(null)}
          onSearchSeries={series}
        />
        <DetailsModal
          book={book}
          onClose={() => setBook(null)}
          onDownload={get}
          onFindDownloads={(child) => {
            void get(child);
          }}
          onSearchSeries={series}
          buttonState={state}
        />
        <output data-testid="action" className="hidden">
          {action}
        </output>
      </main>
    </SearchModeProvider>
  );
}
const mount = document.getElementById('root');
if (mount) createRoot(mount).render(<Harness />);
