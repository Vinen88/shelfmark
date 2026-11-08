import type { Book, ButtonStateInfo, DisplayField } from '../types';

/** A completed transfer does not establish ownership of the selected recording. */
export function editionButtonState(book: Book, state: ButtonStateInfo): ButtonStateInfo {
  return book.provider === 'edition_metadata' &&
    book.download_intent !== 'work' &&
    state.state === 'complete' &&
    book.library?.audiobook !== 'owned'
    ? { text: 'Get', state: 'download' }
    : state;
}

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
  if (book.download_intent === 'work') return fields;
  return book.provider === 'edition_metadata'
    ? fields
        .filter((field) =>
          ['ASIN', 'Part', 'Production', 'Narrators', 'Metadata sources', 'Work evidence'].includes(
            field.label,
          ),
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
        .concat(book.language ? [{ label: 'Language', value: book.language }] : [])
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

const normalized = (value: string) =>
  value
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();

/** Browsing groups only. Catalog IDs and recording identities remain untouched. */
export function groupEditionBooks(books: Book[]): Book[][] {
  const groups = new Map<string, Book[]>();
  for (const book of books) {
    const authors = book.authors?.length ? book.authors : [book.author];
    const knownAuthors =
      authors.length > 0 &&
      authors.every(
        (value) => !['', 'unknown', 'unknown author', 'n a'].includes(normalized(value)),
      );
    const key =
      book.provider === 'edition_metadata' && !book.edition_work_conflicts?.length && knownAuthors
        ? JSON.stringify([
            normalized(book.edition_work_title || book.title),
            authors.map(normalized).toSorted(),
          ])
        : book.id;
    groups.set(key, [...(groups.get(key) || []), book]);
  }
  return [...groups.values()].flatMap((group) => {
    const series = new Set(
      group
        .map((book) => normalized(book.edition_series_name || book.series_name || ''))
        .filter(Boolean),
    );
    const positions = new Set(
      group.map((book) => book.series_position).filter((value) => value != null),
    );
    if (series.size <= 1 && positions.size <= 1) return [group];
    const separate = new Map<string, Book[]>();
    for (const book of group) {
      const key = JSON.stringify([
        normalized(book.edition_series_name || book.series_name || ''),
        book.series_position ?? null,
      ]);
      separate.set(key, [...(separate.get(key) || []), book]);
    }
    return Array.from(separate.values());
  });
}

export function workBook(group: Book[], peers: Book[] = group): Book {
  const first = group.toSorted(
    (a, b) =>
      Number(Boolean(b.series_name && b.series_position != null)) -
        Number(Boolean(a.series_name && a.series_position != null)) || a.id.localeCompare(b.id),
  )[0];
  const series = group.find((book) => book.series_name);
  const title = first.edition_work_title || first.title;
  const authors = first.authors?.length ? first.authors : [first.author];
  const base = [normalized(title), authors.map(normalized).toSorted()];
  const conflicting =
    groupEditionBooks(peers).filter((items) => {
      const peer = items[0];
      return (
        peer.provider === 'edition_metadata' &&
        normalized(peer.edition_work_title || peer.title) === base[0] &&
        JSON.stringify(
          (peer.authors?.length ? peer.authors : [peer.author]).map(normalized).toSorted(),
        ) === JSON.stringify(base[1])
      );
    }).length > 1;
  const id =
    'work:' +
    JSON.stringify([
      ...base,
      conflicting
        ? [
            normalized(first.edition_series_name || first.series_name || ''),
            first.series_position ?? null,
          ]
        : null,
      first.edition_work_conflicts?.length ? first.id : null,
    ]);
  return {
    ...first,
    id,
    title,
    download_intent: 'work',
    edition_candidate_ids: group.flatMap((book) => (book.provider_id ? [book.provider_id] : [])),
    search_title:
      normalized(title) === normalized(series?.series_name || '') && series?.series_position != null
        ? `${title} Book ${series.series_position}`
        : title,
    isbn_10: undefined,
    isbn_13: undefined,
    year: undefined,
    publisher: undefined,
    subtitle: undefined,
    language: undefined,
    source_url: undefined,
    preview: group.find((book) => book.preview)?.preview,
    series_name: series?.series_name,
    series_position: series?.series_position,
    library: group.some((book) => book.work_owned || book.library?.audiobook === 'owned')
      ? { audiobook: 'owned' }
      : undefined,
    display_fields: [
      {
        label: 'Catalog editions',
        value: `${group.length} catalog ${group.length === 1 ? 'edition' : 'editions'}`,
      },
      ...(series?.series_name
        ? [
            {
              label: 'Series',
              value: `${series.series_name}${series.series_position != null ? ` #${series.series_position}` : ''}`,
            },
          ]
        : []),
      ...(first.display_fields || []).filter((field) => field.label === 'Work evidence'),
    ],
  };
}

export function editionSections(books: Book[], query: string): { title?: string; books: Book[] }[] {
  const groups = groupEditionBooks(books);
  const matching = groups.filter((group) =>
    group.some(
      (book) =>
        book.provider === 'edition_metadata' &&
        book.edition_series_name &&
        normalized(book.edition_series_name) === normalized(query),
    ),
  );
  const sections = matching.length
    ? [
        {
          title: matching
            .flat()
            .find((book) => normalized(book.edition_series_name || '') === normalized(query))
            ?.edition_series_name,
          books: matching.flat(),
        },
        { books: groups.filter((group) => !matching.includes(group)).flat() },
      ]
    : [{ books }];
  return sections;
}
