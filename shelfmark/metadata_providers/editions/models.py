"""Internal release-level metadata model, independent of any catalog API."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date


@dataclass(frozen=True, slots=True)
class AudiobookEdition:
    """A single, independently identifiable audiobook *release*.

    Two parts of one GraphicAudio adaptation should have different
    (catalog, edition_id) tuples even if they share author or work title.
    ASIN and ISBN are optional: not every catalog supplies both.
    """

    catalog: str
    edition_id: str
    title: str
    authors: tuple[str, ...] = ()
    subtitle: str | None = None
    isbn_10: str | None = None
    isbn_13: str | None = None
    asin: str | None = None
    cover_url: str | None = None
    description: str | None = None
    publisher: str | None = None
    publish_year: int | None = None
    language: str | None = None
    narrators: tuple[str, ...] = ()
    series_name: str | None = None
    series_position: float | None = None
    source_url: str | None = None
    part_number: int | None = None
    part_total: int | None = None
    parent_work_id: str | None = None
    search_title: str | None = None
    release_date: date | None = None
    # Optional additions preserve the original backend constructor and title.
    canonical_title: str | None = None
    edition_label: str | None = None

    def __post_init__(self) -> None:
        if not self.catalog or ":" in self.catalog:
            raise ValueError("catalog must be non-empty and must not contain ':'")
        if not self.edition_id:
            raise ValueError("edition_id must be non-empty")
        if not self.title:
            raise ValueError("title must be non-empty")
        if self.canonical_title is not None and not self.canonical_title.strip():
            raise ValueError("canonical_title must be non-blank when supplied")
        if self.edition_label is not None and not self.edition_label.strip():
            raise ValueError("edition_label must be non-blank when supplied")
        if self.part_number is not None and self.part_number < 1:
            raise ValueError("part_number must be positive")
        if self.part_total is not None and self.part_total < 1:
            raise ValueError("part_total must be positive")
        if self.part_number and self.part_total and self.part_number > self.part_total:
            raise ValueError("part_number cannot exceed part_total")

    @property
    def provider_id(self) -> str:
        """Stable unique Shelfmark ID (not the shared work's ID)."""
        return f"{self.catalog}:{self.edition_id}"


def split_provider_id(provider_id: str) -> tuple[str, str] | None:
    """Decode release identity for a later detail lookup.

    Returns a tuple of (catalog, edition_id) if valid, otherwise None.
    """
    catalog, separator, edition_id = provider_id.partition(":")
    if not separator or not catalog or not edition_id:
        return None
    return catalog, edition_id
