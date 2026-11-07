"""Pinned native MAM contracts; synthetic transports only, no tracker access."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from threading import Barrier
from typing import Any
from unittest.mock import MagicMock, patch

import requests
from mock_backend import MOCK_EDITIONS

from shelfmark.core.config import config
from shelfmark.core.search_plan import build_release_search_plan
from shelfmark.metadata_providers import BookMetadata
from shelfmark.metadata_providers.editions.provider import _to_shelfmark
from shelfmark.release_sources.prowlarr import mam, source
from shelfmark.release_sources.prowlarr.settings import _test_mam_connection
from shelfmark.release_sources.prowlarr.utils import build_source_id


class NativeMamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch("socket.socket.connect", side_effect=AssertionError("Live network prohibited"))
        )
        self.stack.enter_context(patch.object(mam, "_cache", {}))
        self.stack.enter_context(patch.object(mam, "_failures", mam._Failures()))
        self.settings: dict[str, Any] = {
            "PROWLARR_MAM_ID": "synthetic-session",
            "PROWLARR_ENABLED": True,
            "SUPPORTED_AUDIOBOOK_FORMATS": ["m4b", "mp3"],
        }
        self.stack.enter_context(
            patch.object(
                config,
                "get",
                side_effect=lambda key, default=None, **kwargs: self.settings.get(key, default),
            )
        )
        self.http = self.stack.enter_context(
            patch("shelfmark.release_sources.prowlarr.mam.requests.get")
        )
        self.http.side_effect = AssertionError("Unexpected network request")
        self.client = MagicMock()
        self.client.indexer_timeout = 90
        self.client.get_enabled_indexers_detailed.return_value = [
            {"id": 7, "name": "MAM fixture", "implementation": "MyAnonamouse", "priority": 1},
            {"id": 8, "name": "Other fixture", "priority": 2},
        ]
        self.client.get_enriched_indexer_ids.return_value = [7]
        self.client.get_disabled_indexers.return_value = {}
        self.source = source.ProwlarrSource()
        self.stack.enter_context(patch.object(self.source, "_get_client", return_value=self.client))
        self.stack.enter_context(
            patch.object(self.source, "_get_selected_indexer_ids", return_value=[7])
        )
        self.book = _to_shelfmark(
            replace(
                MOCK_EDITIONS[0],
                title="Dark Age (1 of 3) [Dramatized Adaptation]",
                search_title=None,
            )
        )
        self.row: dict[str, Any] = {
            "indexerId": 7,
            "indexer": "MAM fixture",
            "guid": "https://www.myanonamouse.net/t/123",
            "infoUrl": "https://www.myanonamouse.net/t/123",
            "title": "Dark Age Part 1 by Pierce Brown [ENG / M4B]",
            "bookTitle": "Dark Age Part 1",
            "author": "Pierce Brown",
            "categories": [{"id": 3030}],
            "protocol": "torrent",
            "seeders": 5,
        }
        self.client.torznab_search.return_value = [self.row]

    def response(self, status: int, data: object) -> MagicMock:
        response = MagicMock(status_code=status, text="synthetic response")
        response.json.return_value = data
        self.http.side_effect = None
        self.http.return_value = response
        return response

    def test_transport_counts_native_and_edition_searches(self) -> None:
        """Run unchanged source/client code, intercepting actual HTTP boundaries."""
        from shelfmark.release_sources.prowlarr.api import ProwlarrClient

        client = ProwlarrClient("https://prowlarr.invalid", "synthetic-key")
        indexers = [
            {
                **self.client.get_enabled_indexers_detailed.return_value[0],
                "enable": True,
                "protocol": "torrent",
            }
        ]

        def api_reply(method: str, url: str, **kwargs: Any) -> MagicMock:
            self.assertEqual(method, "GET")
            self.assertIn(
                url,
                (
                    "https://prowlarr.invalid/api/v1/indexer",
                    "https://prowlarr.invalid/api/v1/indexerstatus",
                ),
            )
            reply = MagicMock()
            reply.json.return_value = [] if url.endswith("indexerstatus") else indexers
            return reply

        xml = MagicMock(ok=True)
        xml.text = """<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
          <title>Dark Age Part 1 [ENG / M4B]</title><guid>https://www.myanonamouse.net/t/123</guid>
          <comments>https://www.myanonamouse.net/t/123</comments>
          <torznab:attr name="category" value="3030"/></item></channel></rss>"""
        counts = {}
        with (
            patch.object(self.source, "_get_client", return_value=client),
            patch.object(client._session, "request", side_effect=api_reply) as api,
            patch.object(client._session, "get", return_value=xml) as torznab,
        ):
            native = BookMetadata(
                provider="openlibrary",
                provider_id="fixture-native",
                title="Dark Age Part 1",
                authors=["Pierce Brown"],
            )
            manual = BookMetadata(
                provider="manual",
                provider_id=self.book.provider_id,
                title=self.book.search_title or self.book.title,
                authors=self.book.authors,
            )
            for label, book in (
                ("native", native),
                ("edition", self.book),
                ("edition_listing", manual),
            ):
                mam._cache.clear()
                mam.reset_failures()
                self.response(
                    200,
                    {
                        "data": [
                            {
                                "id": 123,
                                "narrator_info": '{"1":"Fixture cast"}',
                                "series_info": '{"2":["Red Rising","5"]}',
                                "tags": "128 kbps",
                            }
                        ]
                    },
                )
                for operation, query, expanded, expected in (
                    ("cold", None, False, 1),
                    ("warm", None, False, 0),
                    ("manual", "Dark Age Part 1", False, 0),
                    ("expanded", None, True, 0),
                ):
                    api.reset_mock()
                    torznab.reset_mock()
                    self.http.reset_mock()
                    releases = self.source.search(
                        book,
                        build_release_search_plan(book, manual_query=query),
                        content_type="audiobook",
                        expand_search=expanded,
                    )
                    self.assertEqual(len(releases), 1)
                    self.assertEqual(releases[0].extra["narrator"], "Fixture cast")
                    self.assertEqual(releases[0].extra["series_name"], "Red Rising")
                    self.assertEqual(releases[0].extra["bitrate_value"], 128)
                    self.assertEqual(
                        (api.call_count, torznab.call_count, self.http.call_count), (2, 1, expected)
                    )
                    self.assertEqual(torznab.call_args.kwargs["params"]["q"], "Dark Age Part 1")
                    counts[f"{label}/{operation}"] = [
                        api.call_count,
                        torznab.call_count,
                        self.http.call_count,
                    ]
        print("MAM_COUNTS " + json.dumps(counts, sort_keys=True))

    def test_id_cache_expiry_query_normalization_and_no_negative_cache(self) -> None:
        self.response(200, {"data": [{"id": 123}, {"id": 456}]})
        with patch("shelfmark.release_sources.prowlarr.mam.time.time", return_value=1000):
            self.assertEqual(
                set(mam.lookup_torrent_details("synthetic-session", {123}, ["Dark Age"])), {123}
            )
        self.http.reset_mock()
        with patch("shelfmark.release_sources.prowlarr.mam.time.time", return_value=4600):
            self.assertEqual(
                set(mam.lookup_torrent_details("synthetic-session", {456}, ["different query"])),
                {456},
            )
        self.http.assert_not_called()  # Every returned ID is cached; TTL uses >, not >=.
        with patch("shelfmark.release_sources.prowlarr.mam.time.time", return_value=4601):
            mam.lookup_torrent_details("synthetic-session", {456}, ["different query"])
        self.assertEqual(self.http.call_count, 1)
        self.response(200, {"data": []})
        self.http.reset_mock()
        for _ in range(2):
            mam.lookup_torrent_details("synthetic-session", {999}, ["Dark-Age", "Dark Age"])
        self.assertEqual(
            self.http.call_count, 2
        )  # No negative cache; equivalent queries dedup within one lookup.
        self.assertEqual(self.http.call_args.kwargs["params"]["tor[text]"], "Dark Age")

    def test_pagination_stop_conditions_and_four_request_cap(self) -> None:
        page = [{"id": n} for n in range(100)]
        for label, pages, wanted, queries, starts in (
            ("found", [page], 3, ["A"], ["0"]),
            ("short", [[{"id": 1}]], 999, ["A"], ["0"]),
            ("second page", [page, [{"id": 999}]], 999, ["A"], ["0", "100"]),
            ("cap", [page] * 4, 999, ["A"], ["0", "100", "200", "300"]),
            ("variants first", [page] * 4, 999, ["A", "B"], ["0", "0", "100", "100"]),
        ):
            with self.subTest(label=label):
                mam._cache.clear()
                self.http.reset_mock()
                self.http.side_effect = [
                    MagicMock(status_code=200, json=MagicMock(return_value={"data": p}))
                    for p in pages
                ]
                mam.lookup_torrent_details("synthetic-session", {wanted}, queries)
                self.assertEqual(
                    [c.kwargs["params"]["tor[startNumber]"] for c in self.http.call_args_list],
                    starts,
                )

    def test_concurrent_cold_lookups_are_not_coalesced_by_native_cache(self) -> None:
        barrier = Barrier(2)

        def reply(*args: Any, **kwargs: Any) -> MagicMock:
            barrier.wait(timeout=5)
            return MagicMock(status_code=200, json=MagicMock(return_value={"data": [{"id": 123}]}))

        self.http.side_effect = reply
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    mam.lookup_torrent_details, "synthetic-session", {123}, ["Dark Age"]
                )
                for _ in range(2)
            ]
            self.assertEqual([set(f.result()) for f in futures], [{123}, {123}])
        self.assertEqual(self.http.call_count, 2)

    def test_no_mam_without_eligible_release_or_session(self) -> None:
        for rows, session in (
            ([], "synthetic-session"),
            ([self.row], ""),
            (
                [{**self.row, "indexerId": 8, "infoUrl": "https://example.invalid/other"}],
                "synthetic-session",
            ),
        ):
            self.client.torznab_search.return_value = rows
            self.settings["PROWLARR_MAM_ID"] = session
            self.source.search(
                self.book, build_release_search_plan(self.book), content_type="audiobook"
            )
        self.http.assert_not_called()

    def test_http_throttle_uses_native_backoff_and_retains_release(self) -> None:
        self.response(429, {}).headers = {"Retry-After": "3600"}
        for _ in range(2):
            releases = self.source.search(
                self.book, build_release_search_plan(self.book), content_type="audiobook"
            )
            self.assertEqual([r.source_id for r in releases], [build_source_id(self.row)])
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.client.torznab_search.call_count, 2)

    def test_catalog_series_and_presentation_have_zero_release_http_calls(self) -> None:
        from test_series_search import SeriesSearchTests

        from shelfmark.metadata_providers.editions.presentation import work_presentation
        from shelfmark.release_sources.prowlarr.api import ProwlarrClient

        backend = SeriesSearchTests().backend()
        with patch.object(
            ProwlarrClient, "_request", side_effect=AssertionError("Unexpected Prowlarr call")
        ) as api:
            self.assertTrue(backend.search("Harry Potter"))
            self.assertTrue(backend.search("", fields={"series": "Harry Potter"}))
            for edition in backend.search("Harry Potter"):
                work_presentation(edition)
            api.assert_not_called()
        self.client.torznab_search.assert_not_called()
        self.http.assert_not_called()

    def test_session_success_expiry_timeout_and_reset(self) -> None:
        mam._record_failure("synthetic-session", mam.MamError("synthetic"))
        self.response(200, {"username": "fixture-user"})
        self.assertTrue(_test_mam_connection()["success"])
        self.assertIsNone(mam._blocked_reason("synthetic-session"))
        call = self.http.call_args
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["headers"]["Cookie"], "mam_id=synthetic-session")
        for status in (403, 500):
            self.response(status, {})
            self.assertFalse(_test_mam_connection()["success"])
        self.http.side_effect = requests.Timeout("synthetic timeout")
        self.assertFalse(_test_mam_connection()["success"])

    def test_matching_ids_enrich_without_changing_release_identity(self) -> None:
        self.response(
            200,
            {
                "data": [
                    {"id": 999, "narrator_info": '{"1":"Wrong narrator"}'},
                    {
                        "id": 123,
                        "narrator_info": '{"1":"Fixture cast"}',
                        "series_info": '{"2":["Red Rising","5"]}',
                        "tags": "128 kbps",
                    },
                ]
            },
        )
        releases = self.source.search(
            self.book, build_release_search_plan(self.book), content_type="audiobook"
        )
        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(
            (release.source, release.source_id), ("prowlarr", build_source_id(self.row))
        )
        self.assertEqual(release.content_type, "audiobook")
        self.assertEqual(release.format, "m4b")
        self.assertEqual(release.extra["narrator"], "Fixture cast")
        self.assertEqual(release.extra["series_name"], "Red Rising")
        self.assertEqual(release.extra["series_position"], 5)
        self.assertEqual(release.extra["bitrate_value"], 128)
        self.assertEqual(
            self.http.call_args.kwargs["params"]["tor[main_cat][]"], ["13", "15", "16"]
        )

    def test_failed_enrichment_keeps_release_and_backs_off(self) -> None:
        for failure in (
            requests.Timeout("synthetic"),
            mam.MamAuthError("synthetic"),
            requests.HTTPError("synthetic"),
            mam.MamError("synthetic"),
        ):
            with self.subTest(failure=type(failure).__name__):
                mam.reset_failures()
                self.http.reset_mock()
                self.http.side_effect = failure
                first = self.source.search(
                    self.book, build_release_search_plan(self.book), content_type="audiobook"
                )
                second = self.source.search(
                    self.book, build_release_search_plan(self.book), content_type="audiobook"
                )
                self.assertEqual([r.source_id for r in first], [build_source_id(self.row)])
                self.assertEqual([r.source_id for r in second], [build_source_id(self.row)])
                self.assertEqual(self.http.call_count, 1)
                self.assertIsNotNone(mam._blocked_reason("synthetic-session"))
        for _ in range(9):
            mam._record_failure("synthetic-session", mam.MamError("synthetic"))
        self.assertIn("stopped", mam._blocked_reason("synthetic-session") or "")
        self.assertIsNone(mam._blocked_reason("different-synthetic-session"))

    def test_nonmatching_ids_and_spent_deadline_do_not_invent_metadata(self) -> None:
        self.response(200, {"data": [{"id": 999, "narrator_info": '{"1":"Other"}'}]})
        releases = self.source.search(
            self.book, build_release_search_plan(self.book), content_type="audiobook"
        )
        self.assertEqual(len(releases), 1)
        self.assertNotIn("narrator", releases[0].extra)
        self.http.reset_mock()
        self.assertEqual(
            mam.lookup_torrent_details("synthetic-session", {123}, ["Dark Age"], deadline=0), {}
        )
        self.http.assert_not_called()

    def test_native_manual_expanded_other_indexer_and_unsupported_format(self) -> None:
        self.settings["PROWLARR_MAM_ID"] = ""
        self.stack.enter_context(
            patch.object(self.source, "_get_selected_indexer_ids", return_value=[7, 8])
        )
        other = {
            **self.row,
            "indexerId": 8,
            "guid": "https://example.invalid/other",
            "infoUrl": "https://example.invalid/other",
            "indexer": "Other fixture",
        }
        unsupported = {
            **other,
            "guid": "https://example.invalid/unsupported",
            "title": "Unsupported.avi",
        }
        self.client.torznab_search.side_effect = lambda **kw: (
            [self.row] if kw["indexer_id"] == 7 else [other, unsupported]
        )
        native = BookMetadata(
            provider="openlibrary", provider_id="fixture-native", title="Dark Age Part 1"
        )
        for book in (self.book, native):
            for manual, expanded in (
                (None, False),
                (None, True),
                ("Dark Age Part 1", False),
                ("Dark Age Part 1", True),
            ):
                self.client.torznab_search.reset_mock()
                releases = self.source.search(
                    book,
                    build_release_search_plan(book, manual_query=manual),
                    content_type="audiobook",
                    expand_search=expanded,
                )
                self.assertEqual(
                    [r.source_id for r in releases],
                    [
                        build_source_id(self.row),
                        build_source_id(other),
                        build_source_id(unsupported),
                    ],
                )
                # Native audiobook searches retain unknown formats; the modal filters known formats.
                for call in self.client.torznab_search.call_args_list:
                    self.assertEqual(call.kwargs["query"], "Dark Age Part 1")
                    self.assertEqual(call.kwargs["categories"], None if expanded else [3030])
        self.http.assert_not_called()

    def test_native_http_selected_edition_keeps_hint_and_release(self) -> None:
        from app_harness import AppHarness

        from shelfmark.core.release_routes import register_release_routes

        harness = AppHarness()
        for endpoint in ("api_download_release", "api_inspect_release"):
            harness.app.view_functions.pop(endpoint)
        register_release_routes(
            harness.app,
            harness.login,
            resolve_auth_mode=lambda: harness.mode,
            get_user_db=lambda: harness.db,
            load_policy_settings=lambda: harness.settings,
            resolve_status_scope=lambda **kw: (True, None, False),
            attach_library_ownership=lambda b, d: None,
        )
        provider = MagicMock()
        provider.get_book.return_value = self.book
        self.response(200, {"data": []})
        with (
            patch("shelfmark.metadata_providers.get_provider", return_value=provider),
            patch("shelfmark.metadata_providers.get_provider_kwargs", return_value={}),
            patch("shelfmark.metadata_providers.is_provider_registered", return_value=True),
            patch("shelfmark.release_sources.get_source", return_value=self.source),
        ):
            client = harness.app.test_client()
            params = {
                "provider": self.book.provider,
                "book_id": self.book.provider_id,
                "title": self.book.title,
                "content_type": "audiobook",
                "source": "prowlarr",
            }
            self.assertEqual(client.get("/api/releases", query_string=params).status_code, 401)
            with client.session_transaction() as session:
                session.update(user_id="fixture-user", db_user_id=2)
            reply = client.get("/api/releases", query_string=params)
        self.assertEqual(reply.status_code, 200)
        data = reply.get_json()
        self.assertEqual(len(data["releases"]), 1)
        self.assertEqual(data["releases"][0]["source_id"], build_source_id(self.row))
        self.assertEqual(data["book"]["provider_id"], self.book.provider_id)
        self.assertEqual(self.client.torznab_search.call_args.kwargs["query"], "Dark Age Part 1")
        # Observed production failure: selected metadata cooldown aborts before Prowlarr.
        from shelfmark.metadata_providers.editions.catalog_types import RateLimited

        provider.get_book.side_effect = RateLimited(18)
        self.client.torznab_search.reset_mock()
        with (
            patch("shelfmark.metadata_providers.get_provider", return_value=provider),
            patch("shelfmark.metadata_providers.get_provider_kwargs", return_value={}),
            patch("shelfmark.metadata_providers.is_provider_registered", return_value=True),
        ):
            failed = client.get("/api/releases", query_string=params)
        self.assertGreaterEqual(failed.status_code, 500)
        self.assertEqual(failed.get_json()["error"], "Could not search for releases")
        self.client.torznab_search.assert_not_called()
        # Listing already has the selected server-generated hint. Native manual
        # search context avoids a second catalog request; Get still uses its locator.
        with patch("shelfmark.release_sources.get_source", return_value=self.source):
            recovered = client.get(
                "/api/releases",
                query_string={**params, "provider": "manual", "title": self.book.search_title},
            )
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(len(recovered.get_json()["releases"]), 1)
        self.assertEqual(self.client.torznab_search.call_args.kwargs["query"], "Dark Age Part 1")
        self.assertEqual(
            recovered.get_json()["releases"][0]["source_id"], build_source_id(self.row)
        )

    def test_native_torznab_category_response_parses_without_identity_loss(self) -> None:
        from shelfmark.release_sources.prowlarr.api import ProwlarrClient

        client = ProwlarrClient("https://example.invalid", "synthetic-key")
        response = MagicMock(ok=True)
        response.text = """<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
            <title>Dark Age Part 1 [ENG / M4B]</title><guid>https://www.myanonamouse.net/t/123</guid>
            <torznab:attr name="category" value="3030"/><torznab:attr name="seeders" value="5"/>
            </item></channel></rss>"""
        with patch.object(client._session, "get", return_value=response) as get:
            rows = client.torznab_search(indexer_id=7, query="Dark Age Part 1", categories=[3030])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["indexerId"], 7)
        release = source._prowlarr_result_to_release(
            rows[0], "audiobook", enable_format_detection=True
        )
        self.assertEqual(release.source_id, build_source_id(self.row))
        self.assertEqual((release.content_type, release.format), ("audiobook", "m4b"))
        self.assertEqual(get.call_args.kwargs["params"]["cat"], "3030")
