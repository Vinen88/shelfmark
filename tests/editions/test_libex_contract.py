"""Reviewed patch versions retain the same required API structure."""

import unittest
from copy import deepcopy

from shelfmark.metadata_providers.editions.catalog_types import SourceError
from shelfmark.metadata_providers.editions.libex import validate_contract


def specification(version: str) -> dict[str, object]:
    paths: dict[str, object] = {}
    for path, names in {
        "/search": ["title", "author", "region", "limit", "page"],
        "/db/book": ["title", "author_name", "isbn", "series_name", "region", "limit", "page"],
        "/book/{asin}": ["asin", "cache", "region"],
        "/db/book/{asin}": ["asin", "region"],
        "/db/book/sku/{sku}": ["sku"],
    }.items():
        schema: dict[str, object] = {"$ref": "#/components/schemas/BookResponse"}
        if path in {"/search", "/db/book", "/db/book/sku/{sku}"}:
            schema = {"type": "array", "items": schema}
        parameters = [
            {
                "name": name,
                "schema": {
                    "default": 0 if path == "/search" else 1,
                    "maximum": 50 if path == "/search" else 100,
                },
            }
            for name in names
        ]
        paths[path] = {
            "get": {
                "parameters": parameters,
                "responses": {"200": {"content": {"application/json": {"schema": schema}}}},
            }
        }
    fields = [
        "asin",
        "region",
        "title",
        "isbn",
        "publisher",
        "releaseDate",
        "imageUrl",
        "language",
        "authors",
        "narrators",
        "series",
        "manualEntry",
    ]
    return {
        "info": {"version": version},
        "paths": paths,
        "components": {
            "schemas": {
                "BookResponse": {
                    "required": ["asin", "region"],
                    "properties": {key: {"type": "string"} for key in fields},
                }
            }
        },
    }


class ContractTests(unittest.TestCase):
    def test_reviewed_versions_and_unknown_version(self) -> None:
        for version in ("2.4.0", "2.4.2"):
            validate_contract(specification(version))
        with self.assertRaises(SourceError):
            validate_contract(specification("2.4.3"))

    def test_reviewed_version_still_rejects_structural_change(self) -> None:
        spec = deepcopy(specification("2.4.2"))
        spec["paths"] = {}
        with self.assertRaises(SourceError):
            validate_contract(spec)
