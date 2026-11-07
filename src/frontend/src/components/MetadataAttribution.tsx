import type { Book } from '../types';

const sources = [
  { name: 'AudiobookDB', url: 'https://audiobookdb.org/' },
  { name: 'Audnexus', url: 'https://github.com/laxamentumtech/audnexus' },
];

/** Fixed attribution links only; IDs, source URLs and diagnostics are never rendered here. */
export function MetadataAttribution({ books }: { books: readonly Book[] }) {
  const used = sources.filter((source) =>
    books.some(
      (book) =>
        book.provider === 'edition_metadata' &&
        book.display_fields?.some(
          (field) =>
            field.label === 'Metadata sources' && field.value.split(', ').includes(source.name),
        ),
    ),
  );
  if (!used.length) return null;
  return (
    <p className="my-2 flex flex-wrap items-center gap-x-1 text-xs leading-relaxed text-gray-600 dark:text-gray-400">
      <span>Metadata includes</span>
      {used.map((source, index) => (
        <span key={source.name}>
          {index > 0 && ', '}
          <a
            href={source.url}
            target="_blank"
            rel="noopener noreferrer"
            className="underline underline-offset-2"
          >
            {source.name}
          </a>
        </span>
      ))}
    </p>
  );
}
