"""Deterministic fictional audiobook releases for local testing only."""

from collections.abc import Mapping
from datetime import date

from shelfmark.metadata_providers.editions.backend import EditionBackend
from shelfmark.metadata_providers.editions.models import AudiobookEdition

MOCK_EDITIONS: tuple[AudiobookEdition, ...] = tuple(
    AudiobookEdition(
        catalog="mock",
        edition_id=f"lantern-ga-part-{part}",
        title="The Lantern Archive",
        authors=("Example Author",),
        canonical_title="The Lantern Archive",
        edition_label="GraphicAudio",
        subtitle=f"GraphicAudio adaptation - Part {part} of 2",
        isbn_10=isbn10,
        isbn_13=isbn13,
        asin=f"MOCKASIN0{part}",
        narrators=(f"Example Narrator {part}", f"Example Cast {part}"),
        publisher="GraphicAudio (fictional test fixture)",
        release_date=date(2025, part, 15),
        cover_url=f"https://example.invalid/mock/lantern-part-{part}.jpg",
        series_name="The Lantern Cycle",
        series_position=1,
        part_number=part,
        part_total=2,
        parent_work_id="lantern-work",
        language="en",
        search_title="The Lantern Archive",
        description=f"Fictional GraphicAudio Part {part}; not a real catalog record.",
    )
    for part, isbn10, isbn13 in (
        (1, "0000000019", "9780000000019"),
        (2, "0000000027", "9780000000026"),
    )
)


class MockBackend(EditionBackend):
    """In-memory fixtures; never fetches covers or makes network requests.

    Supports general, title, author and ISBN searches. Custom search fields
    are not supported. Blank text queries return the fixture catalog.
    """

    def is_available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        search_type: str = "general",
        limit: int = 40,
        page: int = 1,
        fields: Mapping[str, object] | None = None,
    ) -> list[AudiobookEdition]:
        if limit < 1 or page < 1 or fields:
            return []
        matches: list[AudiobookEdition]
        if search_type == "isbn":
            found = self.by_isbn(query)
            matches = [found] if found else []
        else:
            if search_type not in {"general", "title", "author"}:
                return []
            needle = query.strip().casefold()
            matches = []
            for edition in MOCK_EDITIONS:
                title = f"{edition.title} {edition.subtitle or ''}"
                author = " ".join(edition.authors)
                text = {"title": title, "author": author, "general": f"{title} {author}"}[
                    search_type
                ]
                if needle in text.casefold():
                    matches.append(edition)
        start = (page - 1) * limit
        return matches[start : start + limit]

    def get(self, catalog: str, edition_id: str) -> AudiobookEdition | None:
        return next(
            (
                edition
                for edition in MOCK_EDITIONS
                if (edition.catalog, edition.edition_id) == (catalog, edition_id)
            ),
            None,
        )

    def by_isbn(self, isbn: str) -> AudiobookEdition | None:
        normalized = "".join(isbn.split()).replace("-", "").upper()
        return next(
            (
                edition
                for edition in MOCK_EDITIONS
                if normalized in (edition.isbn_10, edition.isbn_13)
            ),
            None,
        )
