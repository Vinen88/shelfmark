"""Pure ABS projection and lossless, metadata-only export. No output hooks or I/O."""

import json
import re
from dataclasses import dataclass
from typing import Required, TypedDict, cast
from urllib.parse import urlsplit

from shelfmark.core.edition_handoff import MAX_CONTEXT_BYTES, PROVIDER
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.multisource import normalized_isbn
from shelfmark.metadata_providers.editions.snapshot import EditionData, EditionSnapshot


class AbsBookMetadata(TypedDict, total=False):
    title: Required[str]
    subtitle: str
    authors: list[str]
    narrators: list[str]
    publisher: str
    publishedDate: str
    publishedYear: str
    language: str
    isbn: str
    asin: str
    series: list[str]
    description: str


_IDENTITY_FIELDS = frozenset(
    {
        "identity.asin_verified",
        "identity.recording_isbns",
        "identity.links",
        "identity.regional_evidence",
        "recording.production",
    }
)
_SOURCE_FIELDS = frozenset(
    "source." + key
    for key in (
        "region",
        "regions",
        "manualEntry",
        "isAvailable",
        "isBuyable",
        "isListenable",
        "productState",
        "updatedAt",
        "lengthMinutes",
        "bookFormat",
        "series",
        "sku",
        "skuGroup",
        "isbn",
        "releaseDate",
        "copyright",
        "bookId",
        "externalIds",
        "duration",
    )
)
_PRIVATE_KEY = re.compile(
    r"password|passkey|credential|secret|token|download.?url|tracker|authorization|cookie",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


class MetadataUrlError(ValueError):
    """A metadata location failed the fixed export policy."""


@dataclass(frozen=True, slots=True)
class ExportPolicy:
    """Fixed public metadata URLs; reject private data without rewriting snapshots."""

    public_url_prefixes: tuple[str, ...] = (
        "https://m.media-amazon.com/images/",
        *(
            f"https://{prefix}audible.{suffix}/pd/"
            for prefix in ("", "www.")
            for suffix in (
                "ca",
                "com",
                "co.uk",
                "com.au",
                "de",
                "fr",
                "it",
                "es",
                "co.jp",
                "in",
                "com.br",
            )
        ),
        "https://audiobookdb.org/api/releases/",
    )

    def __post_init__(self) -> None:
        for prefix in self.public_url_prefixes:
            url = urlsplit(prefix)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or not prefix.endswith("/")
            ):
                raise ValueError(
                    "Public metadata URL prefix must be credential-free HTTPS ending in /"
                )

    def validate_url(self, value: str) -> None:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(part in {".", ".."} for part in url.path.split("/"))
            or re.search(r"%(?:2e|2f|5c)|\\", value, re.IGNORECASE)
            or not any(value.startswith(prefix) for prefix in self.public_url_prefixes)
        ):
            raise MetadataUrlError(
                "Snapshot contains an unapproved or credential-bearing metadata URL"
            )


def _validate_export(value: object, policy: ExportPolicy) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _PRIVATE_KEY.search(key):
                raise ValueError("Private or unsupported metadata export key")
            _validate_export(child, policy)
    elif isinstance(value, list):
        for child in value:
            _validate_export(child, policy)
    elif isinstance(value, str):
        if re.search(r"magnet:|file://|ftp://|smb://|\\\\", value, re.IGNORECASE):
            raise MetadataUrlError("Private resource location in metadata")
        for match in _URL.finditer(value):
            policy.validate_url(match[0])


@dataclass(frozen=True, slots=True)
class AbsMetadataExport:
    metadata_json: str
    provenance_json: str
    warnings: tuple[str, ...]


def convert_snapshot(
    snapshot: EditionSnapshot,
    policy: ExportPolicy = ExportPolicy(),  # noqa: B008 -- Immutable frozen dataclass default.
) -> AbsMetadataExport:
    """Convert only trusted frozen metadata, never a task/release/output_args dict.

    ABS supports one ISBN and one ASIN. A raw novel ISBN is not recording proof.
    The complete safe snapshot, including unsupported fields/aliases, is retained
    byte-for-byte through its canonical serializer in a separate versioned file.
    """
    raw = snapshot.to_json()
    if len(raw.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ValueError("Metadata export exceeds frozen-context limit")
    snapshot = EditionSnapshot.from_json(raw)
    edition = snapshot.edition
    if (
        snapshot.identity.provider != PROVIDER
        or snapshot.identity.provider_id != edition.provider_id
    ):
        raise ValueError("Unsupported or inconsistent frozen edition identity")
    data = snapshot.to_dict()
    for url in (edition.cover_url, edition.source_url):
        if url is not None:
            policy.validate_url(url)
    _validate_export(data, policy)
    if isinstance(edition, AggregatedEdition):
        for entry in edition.provenance.values:
            if (
                entry.field
                not in frozenset(EditionData.__annotations__) | _IDENTITY_FIELDS | _SOURCE_FIELDS
            ):
                raise ValueError("Unreviewed provenance field; lossless export refused")
            _validate_export(json.loads(entry.value_json), policy)

    result: AbsBookMetadata = {"title": snapshot.edition_title}
    if edition.authors:
        result["authors"] = list(edition.authors)
    if edition.narrators:
        result["narrators"] = list(edition.narrators)
    for key, value in (
        ("subtitle", edition.subtitle),
        ("publisher", edition.publisher),
        ("language", edition.language),
        ("description", edition.description),
    ):
        if value is not None:
            # Explicit keys above are the supported ABS string-valued fields.
            cast("dict[str, object]", result)[key] = value
    if edition.release_date is not None:
        result["publishedDate"] = edition.release_date.isoformat()
        result["publishedYear"] = str(edition.release_date.year)
    elif edition.publish_year is not None:
        result["publishedYear"] = str(edition.publish_year)
    if edition.series_name is not None:
        position = edition.series_position
        if not edition.series_name.strip() or (
            position is None and re.search(r" #[^#\s]+$", edition.series_name)
        ):
            raise ValueError("Literary series cannot be represented unambiguously by ABS")
        suffix = (
            ""
            if position is None
            else " #" + (str(int(position)) if position == int(position) else str(position))
        )
        result["series"] = [edition.series_name + suffix]

    warnings: list[str] = []
    verified_isbns: set[str] = set()
    verified_asin = False
    if isinstance(edition, AggregatedEdition):
        for entry in edition.provenance.values:
            decoded: object = json.loads(entry.value_json)
            if entry.field == "identity.recording_isbns" and isinstance(decoded, list):
                verified_isbns.update(
                    isbn
                    for item in decoded
                    if isinstance(item, str) and (isbn := normalized_isbn(item)) is not None
                )
        primary = {
            entry.field: json.loads(entry.value_json)
            for entry in edition.provenance.values
            if entry.source == edition.provenance.primary
        }
        verified_asin = (
            primary.get("identity.asin_verified") is True and primary.get("asin") == edition.asin
        )
    else:
        # Existing base EditionBackend guarantees release-specific ASINs, but
        # has no recording-scope ISBN attestation. Never upgrade raw ISBNs.
        verified_asin = True
    raw_isbns = tuple(value for value in (edition.isbn_13, edition.isbn_10) if value)
    for candidate in raw_isbns:
        isbn = normalized_isbn(candidate)
        if isbn is not None and isbn in verified_isbns:
            result["isbn"] = isbn
            break
    if raw_isbns and "isbn" not in result:
        warnings.append("ISBN omitted: recording scope is not verified")
    if edition.asin:
        if verified_asin and re.fullmatch(r"[A-Z0-9]{10}", edition.asin):
            result["asin"] = edition.asin
        else:
            warnings.append("ASIN omitted: selected primary ASIN is not verified")

    def encode(value: object) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    return AbsMetadataExport(
        encode(result), encode({"schema_version": 1, "snapshot": data}), tuple(warnings)
    )
