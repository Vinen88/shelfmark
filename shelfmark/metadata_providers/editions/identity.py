"""Edition identity is a pair, independent of titles and optional identifiers."""

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EditionIdentity:
    provider: str
    provider_id: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.provider_id.strip():
            raise ValueError("provider and provider_id must be non-blank")

    @property
    def key(self) -> str:
        """Unambiguous encoding, including when either component contains ':'."""
        return json.dumps(
            [self.provider, self.provider_id], ensure_ascii=False, separators=(",", ":")
        )
