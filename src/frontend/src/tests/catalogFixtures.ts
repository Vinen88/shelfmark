import type { Book } from '../types';

// Synthetic shape: ten identified releases and three unrelated catalog IDs.
export const catalogBooks: Book[] = Array.from({ length: 10 }, (_, i) => i + 1).flatMap(
  (position) => {
    const book: Book = {
      id: `edition_metadata:identified-${position}`,
      provider: 'edition_metadata',
      provider_id: `identified-${position}`,
      title: `The Lantern Road ${position}`,
      author: 'Example Author',
      authors: ['Example Author'],
      edition_work_title: `The Lantern Road ${position}`,
      edition_work_id: `series-position:lantern:${position}`,
      edition_series_name: 'The Lantern Road',
      series_name: 'The Lantern Road',
      series_position: position,
      content_type: 'audiobook',
      display_fields: [{ label: 'ASIN', value: `B${String(position).padStart(9, '0')}` }],
    };
    return [1, 3, 10].includes(position)
      ? [
          {
            ...book,
            id: `edition_metadata:catalog-${position}`,
            provider_id: `catalog-${position}`,
            edition_work_id: `audiobookdb:work-${position}`,
            library: position === 3 ? { audiobook: 'owned' as const } : undefined,
            display_fields: [{ label: 'Metadata sources', value: 'AudiobookDB' }],
          },
          book,
        ]
      : [book];
  },
);
