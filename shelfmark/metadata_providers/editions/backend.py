"""Release-specific metadata lookup contract."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shelfmark.metadata_providers.editions.models import AudiobookEdition


class EditionBackend(ABC):
    """Implemented by the source aggregator and test fixtures."""

    @abstractmethod
    def is_available(self) -> bool:
        """Whether the actual upstream source is ready to search."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        search_type: str = "general",
        limit: int = 40,
        page: int = 1,
        fields: Mapping[str, object] | None = None,
    ) -> list[AudiobookEdition]:
        """Return one AudiobookEdition per *release*, not per written work."""

    @abstractmethod
    def get(self, catalog: str, edition_id: str) -> AudiobookEdition | None:
        """Retrieve one edition using the ID from a prior search."""

    @abstractmethod
    def by_isbn(self, isbn: str) -> AudiobookEdition | None:
        """Find one release by ISBN, when supported by the chosen catalog."""


class EmptyBackend(EditionBackend):
    """Safe placeholder: no network calls, no results, never available."""

    def is_available(self) -> bool:
        return False

    def search(
        self,
        query: str,
        *,
        search_type: str = "general",
        limit: int = 40,
        page: int = 1,
        fields: Mapping[str, object] | None = None,
    ) -> list[AudiobookEdition]:
        return []

    def get(self, catalog: str, edition_id: str) -> AudiobookEdition | None:
        return None

    def by_isbn(self, isbn: str) -> AudiobookEdition | None:
        return None
