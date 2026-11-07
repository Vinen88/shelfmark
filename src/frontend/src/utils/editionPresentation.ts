import type { Book, DisplayField } from '../types';

// Edition provider fields contain only server-attested recording identifiers.
function hasRecordingIdentifier(book: Book): boolean {
  return Boolean(
    book.isbn_10?.trim() ||
    book.isbn_13?.trim() ||
    book.display_fields?.some((field) => field.label === 'ASIN' && field.value.trim()),
  );
}

/** Projection only: never change the selected book or its download identity. */
export function cardDisplayFields(book: Book): DisplayField[] {
  const fields = book.display_fields || [];
  return book.provider === 'edition_metadata'
    ? fields
        .filter((field) =>
          ['ASIN', 'Part', 'Production', 'Narrators', 'Metadata sources'].includes(field.label),
        )
        .map((field) =>
          field.label === 'Narrators'
            ? {
                label: field.label,
                icon: 'microphone',
                value: field.value.length > 90 ? field.value.slice(0, 87) + '…' : field.value,
              }
            : field,
        )
        .concat(book.publisher ? [{ label: 'Publisher', value: book.publisher.slice(0, 90) }] : [])
        .concat(
          !hasRecordingIdentifier(book)
            ? [
                {
                  label: 'Recording identifiers',
                  value: 'Catalog listing · ASIN/ISBN not supplied',
                },
              ]
            : [],
        )
    : fields.filter((field) => field.icon !== 'editions');
}

export function editionDetailFields(book: Book): DisplayField[] {
  const fields = book.display_fields || [];
  if (book.provider !== 'edition_metadata') return fields;
  return fields
    .filter((field) =>
      ['ASIN', 'Part', 'Production', 'Narrators', 'Release date', 'Metadata sources'].includes(
        field.label,
      ),
    )
    .concat(cardDisplayFields(book).filter((field) => field.label === 'Recording identifiers'));
}

/** Stable presentation groups only; retain every original Book object and action. */
export function groupEditionBooks(books: Book[]): Book[][] {
  const groups = new Map<string, Book[]>();
  books.forEach((book) => {
    const key =
      book.provider === 'edition_metadata' && book.edition_work_id ? book.edition_work_id : book.id;
    const group = groups.get(key);
    if (group) group.push(book);
    else groups.set(key, [book]);
  });
  return [...groups.values()];
}

const normalized = (value: string) =>
  value
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();

/** De-emphasize additional catalog matches, never infer recording equivalence or regroup works. */
export function partitionEditionBooks(books: Book[]): { books: Book[]; additional: Book[] } {
  const identified = books.filter(
    (book) => book.provider === 'edition_metadata' && hasRecordingIdentifier(book),
  );
  const main: Book[] = [],
    additional: Book[] = [];
  for (const book of books) {
    const matched =
      book.provider === 'edition_metadata' &&
      !hasRecordingIdentifier(book) &&
      identified.some((peer) =>
        Boolean(
          book.edition_work_id &&
          peer.edition_work_id &&
          (book.edition_work_id === peer.edition_work_id ||
            (book.edition_work_title &&
              peer.edition_work_title &&
              normalized(book.edition_work_title) === normalized(peer.edition_work_title) &&
              book.edition_series_name &&
              peer.edition_series_name &&
              normalized(book.edition_series_name) === normalized(peer.edition_series_name) &&
              book.series_position != null &&
              book.series_position === peer.series_position &&
              book.authors?.length &&
              peer.authors?.length &&
              JSON.stringify(book.authors.map(normalized).toSorted()) ===
                JSON.stringify(peer.authors.map(normalized).toSorted()))),
        ),
      );
    (matched ? additional : main).push(book);
  }
  return { books: main, additional };
}

export function editionSections(
  books: Book[],
  query: string,
): { title?: string; books: Book[]; additional: Book[] }[] {
  const matching = books.filter(
    (book) =>
      book.provider === 'edition_metadata' &&
      book.edition_series_name &&
      normalized(book.edition_series_name) === normalized(query),
  );
  const sections = matching.length
    ? [
        { title: matching[0].edition_series_name, books: matching },
        { books: books.filter((book) => !matching.includes(book)) },
      ]
    : [{ books }];
  return sections.map((section) =>
    Object.assign({}, section, partitionEditionBooks(section.books)),
  );
}
