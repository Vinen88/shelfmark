"""Pure edition naming helpers; never access files or Shelfmark settings."""

import re
from typing import TYPE_CHECKING

from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.search_ranking import literary_title

if TYPE_CHECKING:
    from shelfmark.metadata_providers.editions.models import AudiobookEdition


def canonical_title(edition: AudiobookEdition) -> str:
    return edition.canonical_title if edition.canonical_title is not None else edition.title


def _qualify(title: str, edition: AudiobookEdition) -> str:
    labels: list[str] = []
    if edition.edition_label and edition.edition_label.casefold() not in title.casefold():
        labels.append(edition.edition_label)
    if edition.part_number is not None:
        part = f"Part {edition.part_number}"
        if edition.part_total is not None:
            part += f" of {edition.part_total}"
        # Publishers use both '(1 of 3)' and '(Part 1 of 3)'. Keep either spelling.
        pattern = (
            rf"\bpart\s+{edition.part_number}"
            if edition.part_total is None
            else rf"\b(?:part\s+)?{edition.part_number}\s+of\s+{edition.part_total}"
        )
        if re.search(pattern + r"\b", title, re.IGNORECASE) is None:
            labels.append(part)
    return f"{title} - {' '.join(labels)}" if labels else title


def display_title(edition: AudiobookEdition) -> str:
    """Preserve original release title; append known adaptation/part qualifiers."""
    return _qualify(edition.title, edition)


def search_query(edition: AudiobookEdition) -> str:
    """Retain backend query hints without dropping release-part qualifiers."""
    return _qualify(edition.search_title or edition.title, edition)


def release_search_query(edition: AudiobookEdition) -> str:
    """Native release-search hint, separate from frozen catalog identity/title.

    Indexers commonly use 'Part 1' rather than Audible's '(1 of 3)' and
    '[Dramatized Adaptation]'. Candidates still require explicit selection.
    """
    title = edition.search_title or edition.title
    title = re.sub(
        r"[\[(]?\s*(?:dramatized adaptation|graphicaudio)\s*[\])]?", "", title, flags=re.IGNORECASE
    )
    if edition.part_number is not None:
        title = re.sub(
            r"[\[(]?\s*(?:part\s+)?\d+\s+of\s+\d+\s*[\])]?", "", title, flags=re.IGNORECASE
        )
        title = re.sub(
            r"\bpart\s+" + str(edition.part_number) + r"\b", "", title, flags=re.IGNORECASE
        )
    title = re.sub(r"\s+", " ", title).strip(" -:;()[]")
    suffix = f" Part {edition.part_number}" if edition.part_number is not None else ""
    return (title + suffix).strip() or search_query(edition)


def work_presentation(edition: AudiobookEdition) -> tuple[str | None, str, str | None]:
    """Conservative display grouping; child recording identity is untouched."""
    title = literary_title(edition.title)
    series = literary_title(edition.series_name or "") or None
    numbered = re.search(r"\bBook\s+(\d+(?:\.\d+)?)\b", edition.title, re.IGNORECASE)
    if numbered and (not series or edition.series_position is None):
        title += f", Book {numbered[1]}"
    if isinstance(edition, AggregatedEdition) and "parent_work_id" in edition.provenance.conflicts:
        return None, title, series
    if edition.parent_work_id and edition.parent_work_id.startswith("audiobookdb:"):
        return edition.parent_work_id, title, series
    return None, title, series
