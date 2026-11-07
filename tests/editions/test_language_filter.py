"""Synthetic Primal Hunter discovery; no catalog or tracker connections."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from flask import Flask
from mock_sources import MockCatalogSource
from test_multisource import record

from shelfmark.core.config import config
from shelfmark.core.edition_ownership import EditionBookMetadata
from shelfmark.core.metadata_routes import register_metadata_routes
from shelfmark.metadata_providers import MetadataSearchOptions
from shelfmark.metadata_providers.editions.catalog_types import SourceRecord
from shelfmark.metadata_providers.editions.multisource import AggregationOptions, MultiSourceBackend
from shelfmark.metadata_providers.editions.provider import EditionMetadataProvider
from shelfmark.metadata_providers.editions.search_ranking import language


class PrimalSource(MockCatalogSource):
    def search(
        self, query: str, search_type: str, limit: int, timeout: float
    ) -> tuple[SourceRecord, ...]:
        return super().search(
            query, "general" if search_type == "series" else search_type, limit, timeout
        )


def backend(unknown: bool = False) -> MultiSourceBackend:
    rows = []
    for i, lang in enumerate(("English", "eng", "French", "fr-FR", None, "und")):
        row = record("fixture")
        asin = f"B0LANG{i:04d}"
        rows.append(
            replace(
                row,
                reference=replace(row.reference, record_id=asin),
                edition=replace(
                    row.edition,
                    title=f"The Primal Hunter {i // 2 + 1}",
                    canonical_title=None,
                    asin=asin,
                    isbn_10=None,
                    isbn_13=None,
                    language=lang,
                    narrators=("Example French Narrator" if i in (2, 3) else "Example Narrator",),
                    series_name="The Primal Hunter",
                    series_position=i // 2 + 1,
                    parent_work_id=f"audiobookdb:fixture-work-{i // 2 + 1}",
                ),
            )
        )
    return MultiSourceBackend(
        (PrimalSource("fixture", "libex_live", tuple(rows)),),
        AggregationOptions(include_unknown_language=unknown),
    )


class LanguageTests(unittest.TestCase):
    def test_native_default_and_explicit_languages_before_pagination_and_work_groups(self) -> None:
        provider = EditionMetadataProvider(backend())
        for fields in ({}, {"series": "The Primal Hunter"}):
            with patch.object(config, "get", return_value=["en"]):
                for preference, expected in (
                    (None, ["English", "eng"]),
                    ("fr", ["French", "fr-FR"]),
                    ("en,fr", ["English", "eng", "French", "fr-FR"]),
                ):
                    result = provider.search(
                        MetadataSearchOptions(
                            query="The Primal Hunter", fields=fields, language=preference
                        )
                    )
                    self.assertEqual([b.language for b in result], expected)
                    self.assertTrue(all(isinstance(b, EditionBookMetadata) for b in result))
                    self.assertEqual(
                        len(
                            {
                                b.edition_work_id
                                for b in result
                                if isinstance(b, EditionBookMetadata)
                            }
                        ),
                        len(expected) // 2,
                    )
                    for page in (2, 1):
                        part = provider.search(
                            MetadataSearchOptions(
                                query="The Primal Hunter",
                                fields=fields,
                                language=preference,
                                limit=1,
                                page=page,
                            )
                        )
                        self.assertEqual(
                            [b.provider_id for b in part], [result[page - 1].provider_id]
                        )

    def test_user_defaults_are_resolved_per_search_without_mutating_shared_backend(self) -> None:
        original = backend()
        provider = EditionMetadataProvider(original)

        def preference(key: str, default: object = None, *, user_id: int | None = None) -> object:
            return ["French"] if user_id == 7 else ["English"]

        with patch.object(config, "get", side_effect=preference):
            for user in (7, 8, 7):
                result = provider.search(
                    MetadataSearchOptions(query="The Primal Hunter", user_id=user)
                )
                self.assertEqual(
                    {language(b.language) for b in result}, {"fr" if user == 7 else "en"}
                )
        self.assertIsNone(original.options.languages)

    def test_unknown_preference_and_explicit_all(self) -> None:
        for include in (False, True):
            provider = EditionMetadataProvider(backend(include))
            with patch.object(config, "get", return_value=[]):
                result = provider.search(
                    MetadataSearchOptions(query="The Primal Hunter", language="fr")
                )
                self.assertEqual(len(result), 4 if include else 2)
                all_result = provider.search(
                    MetadataSearchOptions(query="The Primal Hunter", language="all")
                )
                self.assertEqual(len(all_result), 6 if include else 4)
                # An intentionally empty native default preserves the existing extension policy.
                self.assertEqual(
                    len(provider.search(MetadataSearchOptions(query="The Primal Hunter"))),
                    6 if include else 4,
                )

    def test_native_http_forwards_selection_and_series_uses_the_same_filter(self) -> None:
        provider = EditionMetadataProvider(backend())
        app = Flask("language-fixture")
        register_metadata_routes(
            app,
            login_required=lambda view: view,
            resolve_auth_mode=lambda: "none",
            attach_library_ownership=lambda book, data: None,
        )
        with (
            patch("shelfmark.metadata_providers.is_provider_enabled", return_value=True),
            patch(
                "shelfmark.metadata_providers.get_provider_kwargs",
                return_value={"backend": provider.backend},
            ),
            patch("shelfmark.core.metadata_routes.get_session_db_user_id", return_value=7),
            patch(
                "shelfmark.core.utils.transform_cover_url", side_effect=lambda url, cache_id: url
            ),
            patch.object(config, "get", return_value=["en"]),
        ):
            client = app.test_client()
            for search in ("query=The+Primal+Hunter", "series=The+Primal+Hunter&sort=series_order"):
                response = client.get(
                    "/api/metadata/search?provider=edition_metadata&language=fr&" + search
                )
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assertEqual(len(data["books"]), 2)
                self.assertTrue(all(language(b["language"]) == "fr" for b in data["books"]))
                self.assertFalse(data["has_more"])

    def test_each_real_adapter_preserves_explicit_french_metadata(self) -> None:
        from test_libex import FixtureHttp, book
        from test_live_sources import AudHttp, DBHttp, audbook, release, work

        from shelfmark.metadata_providers.editions.audiobookdb import AudiobookDBSource
        from shelfmark.metadata_providers.editions.audnexus import AudnexusSource
        from shelfmark.metadata_providers.editions.libex import LibexSource
        from shelfmark.metadata_providers.editions.source_http import HttpResponse

        lib = LibexSource(FixtureHttp(), "ca")._parse(
            dict(book(), language="French"), HttpResponse(200, {}, {})
        )
        db = AudiobookDBSource(DBHttp())._record(
            dict(release(), language={"name": "French"}), work([release()])
        )
        aud = AudnexusSource(AudHttp(), "ca")._parse(dict(audbook(), language="fr-FR"))
        self.assertEqual([language(row.edition.language) for row in (lib, db, aud)], ["fr"] * 3)

    def test_optional_enrichment_cannot_turn_an_allowed_unknown_into_a_french_result(self) -> None:
        unknown = record("unknown")
        unknown = replace(unknown, edition=replace(unknown.edition, language=None))
        french = record("french")
        french = replace(french, edition=replace(french.edition, language="French"))
        source = MultiSourceBackend(
            (
                MockCatalogSource("unknown", "libex_live", (unknown,)),
                MockCatalogSource("french", "audiobookdb", (french,), searchable=False),
            ),
            AggregationOptions(languages=("en",), include_unknown_language=True),
        )
        self.assertEqual(source.search("Lantern"), [])


if __name__ == "__main__":
    unittest.main()
