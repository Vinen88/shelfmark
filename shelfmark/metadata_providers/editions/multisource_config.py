"""Source choices and user preferences in Shelfmark's existing settings."""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from shelfmark.metadata_providers.editions.audiobookdb import AudiobookDBSource
from shelfmark.metadata_providers.editions.audnexus import MARKETS as AUDNEXUS_MARKETS
from shelfmark.metadata_providers.editions.audnexus import AudnexusSource
from shelfmark.metadata_providers.editions.backend import EditionBackend, EmptyBackend
from shelfmark.metadata_providers.editions.libex import SUPPORTED_MARKETS, LibexSource
from shelfmark.metadata_providers.editions.multisource import AggregationOptions, MultiSourceBackend
from shelfmark.metadata_providers.editions.source_http import HttpClient

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shelfmark.metadata_providers.editions.catalog_types import CatalogSource

DEFAULTS: dict[str, object] = {
    "EDITION_LIBEX_ENABLED": False,
    "EDITION_AUDIOBOOKDB_ENABLED": False,
    "EDITION_AUDIOBOOKDB_API_KEY": "",
    "EDITION_AUDNEXUS_ENABLED": False,
    "EDITION_AUDNEXUS_URL": "",
    "EDITION_MARKETPLACES": "ca,us,uk,au",
    "EDITION_LANGUAGE_MODE": "prefer_english",
    "EDITION_INCLUDE_UNKNOWN_LANGUAGE": False,
}


@lru_cache(maxsize=8)
def _client(base_url: str, interval: float = 0.25, api_key: str = "") -> HttpClient:
    # Only pacing/cooldown state is retained, never catalog results.
    return HttpClient(
        base_url, min_interval=interval, api_key=api_key, user_agent="Shelfmark/editions"
    )


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    raise ValueError("Expected a boolean setting")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a text setting")  # noqa: TRY004 -- Serialized metadata uses ValueError for all invalid fields.
    return value.strip()


@dataclass(frozen=True, slots=True)
class MultiSourceSettings:
    libex_enabled: bool = False
    audiobookdb_enabled: bool = False
    audiobookdb_api_key: str = field(default="", repr=False)
    audnexus_enabled: bool = False
    audnexus_url: str = ""
    options: AggregationOptions = AggregationOptions()

    @classmethod
    def from_mapping(cls, overrides: Mapping[str, object]) -> MultiSourceSettings:
        if set(overrides) - set(DEFAULTS):
            raise ValueError("Unknown metadata source setting")
        values = {**DEFAULTS, **overrides}
        markets = tuple(
            part.strip().lower() for part in _text(values["EDITION_MARKETPLACES"]).split(",")
        )
        if (
            not markets
            or len(markets) != len(set(markets))
            or any(m not in SUPPORTED_MARKETS for m in markets)
        ):
            raise ValueError(
                "Expected unique supported marketplaces; language is a separate setting"
            )
        return cls(
            libex_enabled=_boolean(values["EDITION_LIBEX_ENABLED"]),
            audiobookdb_enabled=_boolean(values["EDITION_AUDIOBOOKDB_ENABLED"]),
            audiobookdb_api_key=_text(values["EDITION_AUDIOBOOKDB_API_KEY"]),
            audnexus_enabled=_boolean(values["EDITION_AUDNEXUS_ENABLED"]),
            audnexus_url=_text(values["EDITION_AUDNEXUS_URL"]),
            options=AggregationOptions(
                marketplaces=markets,
                language_mode=_text(values["EDITION_LANGUAGE_MODE"]),
                include_unknown_language=_boolean(values["EDITION_INCLUDE_UNKNOWN_LANGUAGE"]),
            ),
        )

    def backend(self) -> EditionBackend:
        sources: list[CatalogSource] = []
        if self.libex_enabled:
            sources.extend(
                LibexSource(_client("https://libexdb.com"), market, historical=historical)
                for historical in (False, True)
                for market in self.options.marketplaces
            )
        if self.audiobookdb_enabled:
            sources.append(
                AudiobookDBSource(
                    _client("https://audiobookdb.org/api", api_key=self.audiobookdb_api_key)
                )
            )
        if self.audnexus_enabled:
            if not self.audnexus_url:
                raise ValueError("Audnexus requires a configured authorized endpoint")
            sources.extend(
                AudnexusSource(_client(self.audnexus_url, 0.6), market)
                for market in self.options.marketplaces
                if market in AUDNEXUS_MARKETS
            )
        return MultiSourceBackend(tuple(sources), self.options) if sources else EmptyBackend()


def configured_backend() -> EditionBackend:
    from shelfmark.core.config import config

    values: dict[str, object] = {key: config.get(key, default) for key, default in DEFAULTS.items()}
    return MultiSourceSettings.from_mapping(values).backend()
