import type { Book } from '../types';

export const seriesBooks: Book[] = [4, 6]
  .flatMap((position) =>
    ['A', 'B'].map<Book>((narrator, index) => {
      const title = `Harry Potter and the ${position === 4 ? 'Goblet of Fire' : 'Half-Blood Prince'}`;
      return {
        id: `edition_metadata:fixture-${position}-${narrator}`,
        provider: 'edition_metadata',
        provider_id: `fixture-${position}-${narrator}`,
        title,
        author: 'J.K. Rowling',
        authors: ['J.K. Rowling'],
        year: index ? '2026' : '2020',
        publisher: `Publisher ${narrator}`,
        edition_work_id: `series-position:fixture:${position}`,
        edition_work_title: title,
        edition_series_name: 'Harry Potter',
        series_name: 'Harry Potter',
        series_position: position,
        content_type: 'audiobook',
        cover_aspect: 'square',
        library: index ? undefined : { audiobook: 'owned' as const },
        display_fields: [
          { label: 'Narrators', value: `Narrator ${narrator}`, icon: 'microphone' },
          { label: 'Production', value: index ? 'Full-Cast Edition' : 'Conventional' },
        ],
      };
    }),
  )
  .concat([
    {
      id: 'quiz',
      provider: 'edition_metadata',
      provider_id: 'quiz',
      title: 'Harry Potter Quiz',
      author: 'Companion Author',
      publisher: 'Quiz Publisher',
      year: '2024',
      content_type: 'audiobook',
    },
    {
      id: 'history',
      provider: 'edition_metadata',
      provider_id: 'history',
      title: 'Harry Potter: A History of Magic',
      author: 'Pottermore Publishing; Ben Davies',
      year: '2018',
      content_type: 'audiobook',
      display_fields: [{ label: 'Narrators', value: 'Natalie Dormer' }],
    },
  ]);
