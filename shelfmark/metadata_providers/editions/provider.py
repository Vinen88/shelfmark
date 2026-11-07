"""Native audiobook recording metadata provider."""

from dataclasses import replace
from typing import TYPE_CHECKING

from shelfmark.core.edition_ownership import EditionBookMetadata, ownership_evidence
from shelfmark.core.settings_registry import (
    CheckboxField,
    HeadingField,
    PasswordField,
    SelectField,
    SettingsField,
    TextField,
    register_settings,
)
from shelfmark.metadata_providers import (
    BookMetadata,
    DisplayField,
    MetadataCapability,
    MetadataProvider,
    MetadataSearchOptions,
    SearchType,
    SortOrder,
    TextSearchField,
    register_provider,
)
from shelfmark.metadata_providers.editions.catalog_types import AggregatedEdition
from shelfmark.metadata_providers.editions.identity import EditionIdentity
from shelfmark.metadata_providers.editions.models import AudiobookEdition, split_provider_id
from shelfmark.metadata_providers.editions.presentation import (
    display_title,
    release_search_query,
    work_presentation,
)
from shelfmark.metadata_providers.editions.snapshot import (
    EditionSnapshot,
    validate_unique_identities,
)

if TYPE_CHECKING:
    from shelfmark.metadata_providers.editions.backend import EditionBackend

PROVIDER_NAME = "edition_metadata"


def _to_shelfmark(edition: AudiobookEdition) -> BookMetadata:
    """Map a *release* to one Shelfmark search result without merging parts."""
    display_fields: list[DisplayField] = []
    if edition.asin:
        display_fields.append(DisplayField(label="ASIN", value=edition.asin))
    if edition.narrators:
        display_fields.append(
            DisplayField(label="Narrators", value=", ".join(edition.narrators), icon="microphone")
        )
    if edition.part_number is not None:
        part = str(edition.part_number)
        if edition.part_total is not None:
            part += f" of {edition.part_total}"
        display_fields.append(DisplayField(label="Part", value=part))
    if edition.edition_label:
        display_fields.append(
            DisplayField(
                label="Production",
                value=(
                    "GraphicAudio"
                    if "graphic" in (edition.publisher or "").lower()
                    else edition.edition_label
                ),
            )
        )
    if edition.release_date:
        display_fields.append(
            DisplayField(label="Release date", value=edition.release_date.isoformat())
        )
    if isinstance(edition, AggregatedEdition):
        families = {ref.source.split("_")[0] for ref in edition.provenance.references}
        source_names = [
            name
            for key, name in (("audiobookdb", "AudiobookDB"), ("audnexus", "Audnexus"))
            if key in families
        ]
        if source_names:
            display_fields.append(
                DisplayField(label="Metadata sources", value=", ".join(source_names))
            )
        listings = sorted(
            {
                f"{ref.marketplace.upper()}: {ref.record_id}"
                for ref in edition.provenance.references
                if ref.marketplace
            }
        )
        if listings:
            display_fields.append(
                DisplayField(label="Regional listings", value="; ".join(listings))
            )
        # Discovery diagnostics remain in provenance and existing backend logs.
        # They are not catalog descriptions or ordinary result-card content.

    work_id, work_title, series_title = work_presentation(edition)
    result = EditionBookMetadata(
        edition_work_id=work_id,
        edition_work_title=work_title,
        edition_series_name=series_title,
        provider=PROVIDER_NAME,
        provider_id=edition.provider_id,
        title=display_title(edition),
        authors=list(edition.authors),
        subtitle=edition.subtitle,
        isbn_10=edition.isbn_10,
        isbn_13=edition.isbn_13,
        cover_url=edition.cover_url,
        cover_aspect="square",
        description=edition.description,
        publisher=edition.publisher,
        publish_year=(edition.release_date.year if edition.release_date else edition.publish_year),
        language=edition.language,
        series_name=edition.series_name,
        series_position=edition.series_position,
        source_url=edition.source_url,
        search_title=release_search_query(edition),
        display_fields=display_fields,
        provider_display_name="Audiobook Editions",
    )
    object.__setattr__(result, "_edition_ownership", ownership_evidence(edition))
    return result


@register_provider(PROVIDER_NAME)
class EditionMetadataProvider(MetadataProvider):
    name = PROVIDER_NAME
    display_name = "Audiobook Editions"
    requires_auth = False
    supported_sorts = (SortOrder.RELEVANCE, SortOrder.SERIES_ORDER)
    capabilities = (
        MetadataCapability(key="view_series", field_key="series", sort=SortOrder.SERIES_ORDER),
    )
    search_fields = (
        TextSearchField(key="series", label="Series", placeholder="Search literary series..."),
    )

    def __init__(self, backend: EditionBackend | None = None) -> None:
        from shelfmark.metadata_providers.editions.multisource_config import configured_backend

        self.backend: EditionBackend = backend if backend is not None else configured_backend()

    def is_available(self) -> bool:
        return self.backend.is_available()

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        from shelfmark.core.search_plan import _normalize_languages
        from shelfmark.metadata_providers.editions.multisource import MultiSourceBackend

        backend = self.backend
        if isinstance(backend, MultiSourceBackend):
            requested = options.language.split(",") if options.language else None
            languages = _normalize_languages(requested, options.user_id)
            backend = MultiSourceBackend(
                backend.sources,
                replace(
                    backend.options,
                    languages=tuple(languages) if languages else None,
                    language_mode="all"
                    if requested and any(v.strip().lower() == "all" for v in requested)
                    else backend.options.language_mode,
                ),
            )
        if options.search_type == SearchType.ISBN:
            edition = backend.by_isbn(options.query)
            found = _to_shelfmark(edition) if edition else None
            return [found] if found else []
        editions = backend.search(
            options.query,
            search_type=options.search_type.value,
            limit=options.limit,
            page=options.page,
            fields=dict(options.fields),
        )
        snapshots = tuple(
            EditionSnapshot(EditionIdentity(self.name, edition.provider_id), edition)
            for edition in editions
        )
        validate_unique_identities(snapshots)
        return [_to_shelfmark(snapshot.edition) for snapshot in snapshots]

    def get_book(self, book_id: str) -> BookMetadata | None:
        snapshot = self.get_snapshot(book_id)
        return _to_shelfmark(snapshot.edition) if snapshot else None

    def get_snapshot(self, book_id: str) -> EditionSnapshot | None:
        """Resolve the selected recording for trusted handoff; never saves settings/files."""
        identity = split_provider_id(book_id)
        if identity is None:
            return None
        found = self.backend.get(*identity)
        if found is None:
            return None
        if found.provider_id != book_id:
            raise ValueError("Backend returned a different edition than requested")
        return EditionSnapshot(EditionIdentity(self.name, book_id), found)

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        found = self.backend.by_isbn(isbn)
        return _to_shelfmark(found) if found else None


@register_settings(
    PROVIDER_NAME,
    "Audiobook Editions",
    icon="book-open",
    order=58,
    group="metadata_providers",
)
def edition_metadata_settings() -> list[SettingsField]:
    """Core behavior is provided by the image; only sources and preferences vary."""
    return [
        HeadingField(
            key="edition_metadata_heading",
            title="Audiobook Editions",
            description="Enable only sources you are authorized to use.",
        ),
        CheckboxField(key="EDITION_LIBEX_ENABLED", label="Libex", default=False),
        CheckboxField(
            key="EDITION_AUDIOBOOKDB_ENABLED",
            label="AudiobookDB",
            default=False,
            description="Requires written integration and snapshot/export/retention permission.",
        ),
        PasswordField(key="EDITION_AUDIOBOOKDB_API_KEY", label="AudiobookDB API key", default=""),
        CheckboxField(key="EDITION_AUDNEXUS_ENABLED", label="Audnexus", default=False),
        TextField(key="EDITION_AUDNEXUS_URL", label="Authorized Audnexus endpoint", default=""),
        TextField(
            key="EDITION_MARKETPLACES", label="Marketplace preference order", default="ca,us,uk,au"
        ),
        SelectField(
            key="EDITION_LANGUAGE_MODE",
            label="Language preference",
            default="prefer_english",
            options=[
                {"value": "english_only", "label": "English only"},
                {"value": "prefer_english", "label": "Prefer English, include other languages"},
                {"value": "all", "label": "All languages"},
            ],
        ),
        CheckboxField(
            key="EDITION_INCLUDE_UNKNOWN_LANGUAGE", label="Include unknown languages", default=False
        ),
    ]
