"""Replaceable, network-free catalog fixtures (also usable for future adapters)."""

from shelfmark.metadata_providers.editions.catalog_types import CatalogSource, SourceRecord
from shelfmark.metadata_providers.editions.multisource import equivalent, normalized_isbn
from shelfmark.metadata_providers.editions.presentation import display_title


class MockCatalogSource(CatalogSource):
    @property
    def marketplace(self) -> str | None:
        return self._marketplace

    def __init__(
        self,
        key: str,
        family: str,
        records: tuple[SourceRecord, ...],
        *,
        marketplace: str | None = None,
        searchable: bool = True,
    ) -> None:
        self.key, self.family, self.records = key, family, records
        self._marketplace, self.searchable = marketplace, searchable

    def search(
        self, query: str, search_type: str, limit: int, timeout: float
    ) -> tuple[SourceRecord, ...]:
        if not self.searchable:
            return ()
        if search_type == "isbn":
            isbn = normalized_isbn(query)
            return tuple(
                r
                for r in self.records
                if isbn
                and isbn
                in {
                    normalized_isbn(r.edition.isbn_10 or ""),
                    normalized_isbn(r.edition.isbn_13 or ""),
                }
            )[:limit]
        if search_type not in {"general", "title", "author"}:
            return ()
        return tuple(
            r
            for r in self.records
            if query.casefold()
            in (
                " ".join(r.edition.authors)
                if search_type == "author"
                else f"{display_title(r.edition)} {r.edition.canonical_title or ''}"
                if search_type == "title"
                else f"{display_title(r.edition)} {r.edition.canonical_title or ''} {' '.join(r.edition.authors)}"
            ).casefold()
        )[:limit]

    def get(self, record_id: str, timeout: float) -> SourceRecord | None:
        return next((r for r in self.records if r.reference.record_id == record_id), None)

    def lookup(self, selected: SourceRecord, timeout: float) -> tuple[SourceRecord, ...]:
        return tuple(r for r in self.records if equivalent(selected, r))
