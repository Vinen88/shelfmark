"""Auditable regional exception; the ordinary conflicting-ASIN rule is unchanged."""

import re

from shelfmark.metadata_providers.editions.catalog_types import RegionalEvidence, SourceRecord
from shelfmark.metadata_providers.editions.search_ranking import (
    base_title,
    fact,
    language,
    literary_title,
    words,
)

RULE = "libex-recording-sku-v1"


def recording_cast(names: tuple[str, ...]) -> frozenset[str]:
    """Normalize only the observed repeated generic credit, never person names.

    Keep raw cast values in snapshots/provenance. 'Full Cast Full Cast' is
    redundant catalog boilerplate, not a different named narrator.
    """
    return frozenset(
        "full cast"
        if re.fullmatch(r"(?:full\s+cast\s*)+", name.strip(), re.IGNORECASE)
        else name.casefold().strip()
        for name in names
    )


def libex_evidence(record: SourceRecord) -> RegionalEvidence | None:
    from shelfmark.metadata_providers.editions.multisource import normalized_isbn

    e = record.edition
    group, duration = fact(record, "skuGroup"), fact(record, "lengthMinutes")
    isbn = normalized_isbn(e.isbn_13 or e.isbn_10 or "")
    if (
        not isinstance(group, str)
        or not re.fullmatch(r"BK_[A-Z0-9]{4}_\d+", group)
        or type(duration) is not int
        or duration <= 0
        or not record.asin_verified
        or not e.asin
        or not re.fullmatch(r"[A-Z0-9]{10}", e.asin)
        or record.reference.record_id != e.asin
        or record.reference.source
        not in {
            f"libex_live_{record.reference.marketplace}",
            f"libex_history_{record.reference.marketplace}",
        }
        or not e.authors
        or not e.narrators
        or not e.publisher
        or not language(e.language)
        or not e.release_date
        or not e.cover_url
        or (e.part_number is None) != (e.part_total is None)
        or fact(record, "bookFormat") not in ("abridged", "unabridged")
    ):
        return None
    sku = fact(record, "sku")
    expected_sku = group + (
        (record.reference.marketplace or "").upper() if record.reference.marketplace != "us" else ""
    )
    if sku is not None and sku != expected_sku:
        return None
    if group.startswith("BK_GRPH_"):
        if (
            not isbn
            or len(e.narrators) < 2
            or record.production != "dramatized"
            or words(e.publisher) not in {"graphic audio llc", "graphicaudio"}
        ):
            return None
    elif sku != expected_sku or (not isbn and not group.startswith("BK_ACX0_")):
        return None
    # Generic SKU equivalence does not attest raw Audible ISBN recording scope.
    return RegionalEvidence(RULE, group, isbn if group.startswith("BK_GRPH_") else None, duration)


def regional_equivalent(first: SourceRecord, second: SourceRecord) -> bool:
    """Require a recording SKU plus all corroborators, across different regions."""
    from shelfmark.metadata_providers.editions.multisource import normalized_isbn

    a, b = first.edition, second.edition
    left, right = first.regional_evidence, second.regional_evidence
    if (
        left is None
        or right is None
        or left.rule != RULE
        or right.rule != RULE
        or not first.reference.marketplace
        or not second.reference.marketplace
        or first.reference.marketplace == second.reference.marketplace
        or left != right
        or libex_evidence(first) != left
        or libex_evidence(second) != right
    ):
        return False
    identifiers = {
        normalized_isbn(value)
        for r in (first, second)
        for value in (*r.recording_isbns, r.edition.isbn_10, r.edition.isbn_13)
        if value
    }
    if None in identifiers or len(identifiers) > 1:
        return False
    if normalized_isbn(a.isbn_13 or a.isbn_10 or "") != normalized_isbn(
        b.isbn_13 or b.isbn_10 or ""
    ):
        return False
    return (
        base_title(a.title) == base_title(b.title)
        and {words(name) for name in a.authors} == {words(name) for name in b.authors}
        and {words(name) for name in recording_cast(a.narrators)}
        == {words(name) for name in recording_cast(b.narrators)}
        and language(a.language) == language(b.language)
        and words(a.publisher or "") == words(b.publisher or "")
        and a.release_date == b.release_date
        and a.cover_url == b.cover_url
        and a.part_number == b.part_number
        and a.part_total == b.part_total
        and first.production == second.production
        and fact(first, "bookFormat") == fact(second, "bookFormat")
        and (
            a.series_position is None
            or b.series_position is None
            or (
                a.series_position == b.series_position
                and words(literary_title(a.series_name or ""))
                == words(literary_title(b.series_name or ""))
            )
        )
    )
