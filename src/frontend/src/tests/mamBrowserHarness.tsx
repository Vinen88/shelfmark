/* oxlint-disable import/no-unassigned-import, no-empty-function, typescript/no-misused-promises -- Isolated fixture harness; callbacks and minimal responses are synthetic. */
// Synthetic release modal, never connected to production or download clients.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';

import { ReleaseModal } from '../components/ReleaseModal';
import { ResultsSection } from '../components/ResultsSection';
import { SearchModeProvider } from '../contexts/SearchModeContext';
import type { Book } from '../types';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';

import '../styles.css';

const books: Book[] = [1, 2].map((part) => ({
  id: `edition:fixture-part-${part}`,
  provider: 'edition_metadata',
  provider_id: `fixture-part-${part}`,
  title: `Dark Age (Part ${part} of 3) [Dramatized Adaptation]`,
  author: 'Pierce Brown',
  search_title: `Dark Age Part ${part}`,
  content_type: 'audiobook',
  edition_work_id: 'series-position:fixture-dark-age',
  edition_work_title: 'Dark Age',
  display_fields: [{ label: 'Part', value: `${part} of 3` }],
}));
function Harness() {
  const [book, setBook] = useState<Book | null>(null);
  const [action, setAction] = useState('');
  const [revision, setRevision] = useState(0);
  const open = async (value: Book) => setBook(value);
  return (
    <SearchModeProvider searchMode="universal">
      <main className="p-5">
        <button
          type="button"
          data-testid="rerender"
          onClick={() => setRevision((value) => value + 1)}
        >
          Rerender {revision}
        </button>
        <button
          type="button"
          data-testid="late-metadata"
          onClick={() =>
            setBook((value) => value && { ...value, publisher: 'Updated fixture publisher' })
          }
        >
          Update display metadata
        </button>
        <ResultsSection
          books={books}
          visible
          sortValue="relevance"
          onSortChange={() => {}}
          onDetails={async () => {}}
          onDownload={open}
          onGetReleases={open}
          getButtonState={() => ({ text: 'Get', state: 'download' })}
          getUniversalButtonState={() => ({ text: 'Get', state: 'download' })}
        />
        <ReleaseModal
          book={book}
          onClose={() => setBook(null)}
          contentType="audiobook"
          supportedFormats={['epub']}
          supportedAudiobookFormats={['m4b', 'mp3']}
          defaultLanguages={['en']}
          bookLanguages={[]}
          currentStatus={{}}
          defaultAudiobookReleaseSource="prowlarr"
          onDownload={async (child, release, contentType) =>
            setAction(JSON.stringify(buildReleaseDownloadPayload(child, release, contentType)))
          }
        />
        <output data-testid="payload" className="hidden">
          {action}
        </output>
      </main>
    </SearchModeProvider>
  );
}
const mount = document.getElementById('root');
if (mount) createRoot(mount).render(<Harness />);
