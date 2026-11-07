"""Bounded discovery and stateless exact-identifier detail reconstruction."""

import base64
import json
import logging
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from time import monotonic
from typing import TYPE_CHECKING, TypeVar

from shelfmark.metadata_providers.editions.backend import EditionBackend
from shelfmark.metadata_providers.editions.catalog_types import (
    AggregatedEdition,
    CatalogSource,
    FieldValue,
    Provenance,
    SourceError,
    SourceRecord,
    SourceRef,
    metadata_values,
)
from shelfmark.metadata_providers.editions.regional import recording_cast, regional_equivalent
from shelfmark.metadata_providers.editions.search_ranking import language as language  # noqa: PLC0414 -- Language normalization is an existing public helper.
from shelfmark.metadata_providers.editions.search_ranking import literary_title, relevance, words

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from shelfmark.metadata_providers.editions.models import AudiobookEdition

logger = logging.getLogger(__name__)
T = TypeVar("T")


def normalized_isbn(value: str) -> str | None:
    isbn = re.sub(r"[-\s]", "", value).upper()
    if len(isbn) == 10 and re.fullmatch(r"\d{9}[\dX]", isbn):
        digits = [10 if char == "X" else int(char) for char in isbn]
        if sum((10 - i) * digit for i, digit in enumerate(digits)) % 11:
            return None
        prefix = "978" + isbn[:9]
        return prefix + str(
            (-sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(prefix))) % 10
        )
    if (
        len(isbn) == 13
        and isbn.isdigit()
        and isbn.startswith(("978", "979"))
        and sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn)) % 10 == 0
    ):
        return isbn
    return None


def _asins(record: SourceRecord) -> str | None:
    value = (record.edition.asin or "").strip().upper()
    return value if record.asin_verified and re.fullmatch(r"[A-Z0-9]{10}", value) else None


def _pick[T](first: T | None, second: T | None) -> T | None:
    return second if first is None or first == "" else first


def _fill(first: AudiobookEdition, second: AudiobookEdition) -> AudiobookEdition:
    """Typed, explicit field overlay; never pass an untyped dictionary to a model."""
    return replace(
        first,
        title=first.title or second.title,
        authors=first.authors or second.authors,
        subtitle=_pick(first.subtitle, second.subtitle),
        isbn_10=_pick(first.isbn_10, second.isbn_10),
        isbn_13=_pick(first.isbn_13, second.isbn_13),
        asin=_pick(first.asin, second.asin),
        cover_url=_pick(first.cover_url, second.cover_url),
        description=_pick(first.description, second.description),
        publisher=_pick(first.publisher, second.publisher),
        publish_year=_pick(first.publish_year, second.publish_year),
        language=_pick(first.language, second.language),
        narrators=first.narrators or second.narrators,
        series_name=_pick(first.series_name, second.series_name),
        series_position=_pick(first.series_position, second.series_position),
        source_url=_pick(first.source_url, second.source_url),
        part_number=_pick(first.part_number, second.part_number),
        part_total=_pick(first.part_total, second.part_total),
        parent_work_id=_pick(first.parent_work_id, second.parent_work_id),
        search_title=_pick(first.search_title, second.search_title),
        release_date=_pick(first.release_date, second.release_date),
        canonical_title=_pick(first.canonical_title, second.canonical_title),
        edition_label=_pick(first.edition_label, second.edition_label),
    )


def equivalent(first: SourceRecord, second: SourceRecord) -> bool:
    a, b = first.edition, second.edition
    # Hard contradictions veto even a matching identifier. Never single-link clusters.
    for left, right in (
        (a.part_number, b.part_number),
        (a.part_total, b.part_total),
        (language(a.language), language(b.language)),
        (first.production, second.production),
    ):
        if left is not None and right is not None and left != right:
            return False
    if a.narrators and b.narrators and recording_cast(a.narrators) != recording_cast(b.narrators):
        return False
    aa, ba = _asins(first), _asins(second)
    if aa and ba and aa != ba:
        return False
    ai = {v for raw in first.recording_isbns if (v := normalized_isbn(raw))}
    bi = {v for raw in second.recording_isbns if (v := normalized_isbn(raw))}
    if ai and bi and not ai & bi:
        return False
    direct = (
        first.reference.key == second.reference.key
        or first.reference.key in {ref.key for ref in second.links}
        or second.reference.key in {ref.key for ref in first.links}
    )
    return (
        direct
        or bool(aa and aa == ba)
        or bool(
            ai & bi
            and a.narrators
            and b.narrators
            and first.production
            and second.production
            and language(a.language)
            and language(b.language)
        )
    )


def encode_identity(ref: SourceRef) -> str:
    raw = json.dumps(
        [1, ref.source, ref.marketplace, ref.record_id], ensure_ascii=False, separators=(",", ":")
    ).encode()
    if len(raw) > 1024:
        raise ValueError("Catalog identity is too long")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_identity(value: str) -> SourceRef:
    if len(value) > 1400 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid aggregate identity")
    data: object = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    if (
        not isinstance(data, list)
        or len(data) != 4
        or type(data[0]) is not int
        or data[0] != 1
        or not isinstance(data[1], str)
        or not isinstance(data[3], str)
        or (data[2] is not None and not isinstance(data[2], str))
    ):
        raise ValueError("Invalid aggregate identity")
    result = SourceRef(data[1], data[3], data[2])
    if encode_identity(result) != value:
        raise ValueError("Noncanonical aggregate identity")
    return result


@dataclass(frozen=True, slots=True)
class AggregationOptions:
    language_mode: str = "prefer_english"
    include_unknown_language: bool = False
    languages: tuple[str, ...] | None = None  # Request/user preference, not another setting.
    source_priority: tuple[str, ...] = ("libex_live", "libex_history", "audiobookdb", "audnexus")
    marketplaces: tuple[str, ...] = ("ca", "us", "uk", "au")
    timeout: float = 8.0
    workers: int = 4
    window: int = 40
    max_pages: int = 2
    max_search_calls: int = 16

    def __post_init__(self) -> None:
        if self.language_mode not in {"english_only", "prefer_english", "all"}:
            raise ValueError("Unsupported language mode")
        if (
            not 0.05 <= self.timeout <= 30
            or not 1 <= self.workers <= 8
            or not 1 <= self.window <= 100
        ):
            raise ValueError("Invalid aggregation bounds")
        if (
            type(self.max_pages) is not int
            or not 1 <= self.max_pages <= 3
            or type(self.max_search_calls) is not int
            or not 1 <= self.max_search_calls <= 48
        ):
            raise ValueError("Invalid discovery page/request bounds")


def _parallel[T](
    calls: tuple[tuple[str, Callable[[], T]], ...], options: AggregationOptions, timeout: float
) -> tuple[dict[str, T], tuple[str, ...]]:
    if not calls:
        return {}, ()
    executor = ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="edition-catalog")
    futures = {executor.submit(call): name for name, call in calls}
    done, pending = wait(futures, timeout=max(0, timeout))
    results: dict[str, T] = {}
    errors = [f"{futures[future]}: timeout" for future in pending]
    for future in done:
        try:
            results[futures[future]] = future.result()
        except Exception as exc:  # noqa: BLE001 -- Optional source/scan failures must not invalidate valid results.
            # Partial outages never masquerade as successful empty searches.
            errors.append(f"{futures[future]}: {type(exc).__name__}")
    executor.shutdown(wait=False, cancel_futures=True)
    return results, tuple(sorted(errors))


class MultiSourceBackend(EditionBackend):
    def __init__(
        self,
        sources: tuple[CatalogSource, ...],
        options: AggregationOptions = AggregationOptions(),  # noqa: B008 -- Immutable frozen dataclass default.
    ) -> None:
        if len(sources) > 24 or len({source.key for source in sources}) != len(sources):
            raise ValueError("Too many sources or duplicate source keys")
        if any(
            type(cost) is not int or not 1 <= cost <= 16
            for s in sources
            for cost in (s.discovery_cost, s.lookup_cost)
        ):
            raise ValueError("Invalid source request costs")
        self.sources = sources
        self.options = options
        self._sources = {source.key: source for source in self.sources}

    def is_available(self) -> bool:
        return any(source.searchable for source in self.sources)

    def _rank(self, record: SourceRecord) -> tuple[int, int, str, str]:
        source = self._sources[record.reference.source]
        return (*self._source_rank(source), record.reference.record_id)

    def _source_rank(self, source: CatalogSource) -> tuple[int, int, str]:
        priority = self.options.source_priority
        regions = self.options.marketplaces
        return (
            priority.index(source.family) if source.family in priority else len(priority),
            regions.index(source.marketplace) if source.marketplace in regions else len(regions),
            source.key,
        )

    def _same_recording(self, first: SourceRecord, second: SourceRecord) -> bool:
        return equivalent(first, second) or regional_equivalent(first, second)

    def _discover(
        self, query: str, search_type: str, deadline: float, exact_asin: bool
    ) -> tuple[dict[str, tuple[SourceRecord, ...]], tuple[str, ...], tuple[str, ...], int]:
        """One executor, bounded pages/calls, with live capacity protected from history."""
        queue = [(source, 1) for source in self.sources if source.searchable]
        found: dict[str, tuple[SourceRecord, ...]] = {}
        errors: list[str] = []
        warnings: list[str] = []
        calls = 0
        preferred = self.options.marketplaces[:2]

        def priority(job: tuple[CatalogSource, int]) -> tuple[int, int, tuple[int, int, str]]:
            source, page = job
            historical = source.family == "libex_history"
            if exact_asin or search_type == "isbn":
                tier = 0 if historical else 1
            else:
                tier = (1 if historical else 0) + (
                    0 if source.marketplace is None or source.marketplace in preferred else 2
                )
                if historical and page > 1:
                    tier += 2
            return tier, page, self._source_rank(source)

        def run(source: CatalogSource, page: int) -> tuple[SourceRecord, ...]:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SourceError("Request deadline reached before discovery")
            records: tuple[SourceRecord, ...]
            if exact_asin:
                record = source.get(query.strip().upper(), remaining)
                records = (record,) if record is not None else ()
            else:
                records = source.search_page(
                    query,
                    search_type,
                    min(self.options.window, source.page_limit),
                    remaining,
                    page=page,
                )
            return self._checked(source, records)

        executor = ThreadPoolExecutor(
            max_workers=self.options.workers, thread_name_prefix="edition-discovery"
        )
        active: dict[Future[tuple[SourceRecord, ...]], tuple[CatalogSource, int]] = {}
        historical_limit = (
            max(1, self.options.workers // 2)
            if any(s.family != "libex_history" for s, _ in queue)
            else self.options.workers
        )
        try:
            while (queue or active) and monotonic() < deadline:
                queue.sort(key=priority)
                while (
                    queue
                    and len(active) < self.options.workers
                    and calls < self.options.max_search_calls
                ):
                    history_count = sum(s.family == "libex_history" for s, _ in active.values())
                    index = next(
                        (
                            i
                            for i, (s, _) in enumerate(queue)
                            if calls + s.discovery_cost <= self.options.max_search_calls
                            and (s.family != "libex_history" or history_count < historical_limit)
                        ),
                        None,
                    )
                    if index is None or monotonic() >= deadline:
                        break
                    source, page = queue.pop(index)
                    active[executor.submit(run, source, page)] = source, page
                    calls += source.discovery_cost
                if not active:
                    break
                done, _ = wait(
                    active, timeout=max(0, deadline - monotonic()), return_when=FIRST_COMPLETED
                )
                if not done:
                    break
                for future in sorted(done, key=lambda f: (active[f][0].key, active[f][1])):
                    source, page = active.pop(future)
                    try:
                        records = future.result()
                    except Exception as exc:  # noqa: BLE001 -- Optional source/scan failures must not invalidate valid results.
                        errors.append(f"{source.key} page {page}: {type(exc).__name__}")
                        continue  # No retry, and no next page after failure.
                    previous = found.get(source.key, ())
                    keys = {r.reference.key for r in previous}
                    overlap = any(r.reference.key in keys for r in records)
                    found[source.key] = previous + tuple(
                        r for r in records if r.reference.key not in keys
                    )
                    if overlap:
                        errors.append(
                            f"{source.key} page {page}: repeated page identifiers; pagination incomplete"
                        )
                    elif not exact_asin and len(records) == min(
                        self.options.window, source.page_limit
                    ):
                        if source.supports_paging and page < self.options.max_pages:
                            queue.append((source, page + 1))
                        else:
                            warnings.append(
                                f"{source.key}: discovery window reached; page {page} saturated, additional pages not read"
                            )
            errors.extend(f"{s.key} page {p}: timeout" for s, p in active.values())
            errors.extend(f"{s.key} page {p}: skipped (deadline or request cap)" for s, p in queue)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return found, tuple(sorted(errors)), tuple(sorted(warnings)), calls

    def _accept_language(self, value: str | None) -> bool:
        code = language(value)
        if code in {None, "unknown", "und", "unk", "n/a", "na", "none", "null", "unspecified"}:
            return self.options.include_unknown_language
        if self.options.languages is not None:
            return code in self.options.languages
        return self.options.language_mode != "english_only" or code == "en"

    def _checked(
        self, source: CatalogSource, records: tuple[SourceRecord, ...]
    ) -> tuple[SourceRecord, ...]:
        if len(records) > self.options.window:
            raise SourceError("Source exceeded discovery bound")
        seen: set[tuple[str, str, str]] = set()
        for record in records:
            if (
                record.reference.source != source.key
                or record.reference.marketplace != source.marketplace
                or record.reference.key in seen
            ):
                raise SourceError("Source returned a mismatched or duplicate identity")
            seen.add(record.reference.key)
        return records

    def _merge(
        self, primary: SourceRecord, members: tuple[SourceRecord, ...], warnings: tuple[str, ...]
    ) -> AggregatedEdition:
        ordered = (
            primary,
            *tuple(
                sorted(
                    (r for r in members if r.reference.key != primary.reference.key), key=self._rank
                )
            ),
        )
        data = metadata_values(primary.edition)
        values: list[FieldValue] = []
        conflicts: set[str] = set()
        for record in ordered:
            values.extend(
                FieldValue("source." + name, record.reference, value)
                for name, value in record.facts
            )
            evidence: dict[str, object] = {
                "identity.asin_verified": record.asin_verified,
                "identity.recording_isbns": record.recording_isbns,
                "identity.links": [list(ref.key) for ref in record.links],
                "recording.production": record.production,
            }
            if record.regional_evidence is not None:
                evidence["identity.regional_evidence"] = asdict(record.regional_evidence)
            values.extend(
                FieldValue(
                    name,
                    record.reference,
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
                for name, value in evidence.items()
            )
            for name, value in metadata_values(record.edition).items():
                if value is None or value in ((), ""):
                    continue
                encoded = json.dumps(
                    value,
                    default=str,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                values.append(FieldValue(name, record.reference, encoded))
                if data[name] is None or data[name] == () or data[name] == "":
                    data[name] = value
                elif data[name] != value:
                    conflicts.add(name)
        edition = primary.edition
        for record in ordered[1:]:
            edition = _fill(edition, record.edition)
        attested = {
            isbn
            for record in ordered
            for value in record.recording_isbns
            if (isbn := normalized_isbn(value)) is not None
        }
        # Raw source ISBNs stay in provenance; only attested recording ISBNs
        # become selected identifiers, including when the primary has a work ISBN.
        isbn10 = next(
            (
                r.edition.isbn_10
                for r in ordered
                if normalized_isbn(r.edition.isbn_10 or "") in attested
            ),
            None,
        )
        isbn13 = next(
            (
                r.edition.isbn_13
                for r in ordered
                if normalized_isbn(r.edition.isbn_13 or "") in attested
            ),
            None,
        )
        # Alias ASIN evidence stays in provenance/ownership, never becomes an
        # unverified primary identifier on a no-ASIN independent recording.
        edition = replace(
            edition,
            catalog="aggregate",
            edition_id=encode_identity(primary.reference),
            asin=primary.edition.asin,
            isbn_10=isbn10,
            isbn_13=isbn13,
        )
        provenance = Provenance(
            primary.reference,
            tuple(r.reference for r in ordered),
            tuple(values),
            tuple(sorted(conflicts)),
            tuple(sorted(set(warnings + tuple(w for r in ordered for w in r.warnings)))),
        )
        return AggregatedEdition.create(edition, provenance)

    def _resolve(
        self,
        primary: SourceRecord,
        timeout: float,
        warnings: tuple[str, ...] = (),
        known: tuple[SourceRecord, ...] = (),
        targets: tuple[CatalogSource, ...] | None = None,
    ) -> AggregatedEdition:
        if timeout <= 0:
            return self._merge(
                primary,
                known or (primary,),
                (*warnings, "Enrichment skipped: request deadline reached"),
            )
        deadline = monotonic() + timeout

        def lookup(source: CatalogSource) -> tuple[SourceRecord, ...]:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SourceError("Request deadline reached before lookup")
            return self._checked(source, source.lookup(primary, remaining))

        members = list(known or (primary,))
        known_sources = {member.reference.source for member in members}
        # Reuse this request's verified search records. Fresh detail reconstructs
        # them by exact identifiers; no persistent or process-local result cache.
        sources = (
            targets
            if targets is not None
            else tuple(
                source
                for source in self.sources
                if source.key not in known_sources or not source.unique_recording_ids
            )
        )
        calls = tuple((source.key, lambda source=source: lookup(source)) for source in sources)
        found, errors = _parallel(calls, self.options, timeout)
        candidates = sorted((r for records in found.values() for r in records), key=self._rank)
        for record in candidates:
            if record.reference.key not in {member.reference.key for member in members} and all(
                self._same_recording(record, member) for member in members
            ):
                members.append(record)
                if len(members) > 32:
                    raise SourceError("Too many aliases for one recording")
        return self._merge(primary, tuple(members), warnings + errors)

    def search(
        self,
        query: str,
        *,
        search_type: str = "general",
        limit: int = 40,
        page: int = 1,
        fields: Mapping[str, object] | None = None,
    ) -> list[AudiobookEdition]:
        if fields:
            if (
                set(fields) != {"series"}
                or not isinstance(fields["series"], str)
                or not fields["series"].strip()
            ):
                return []
            query, search_type = fields["series"].strip(), "series"
        if limit < 1 or page < 1 or (page - 1) * limit >= self.options.window:
            return []
        deadline = monotonic() + self.options.timeout
        exact_asin = search_type in {"general", "title"} and bool(
            re.fullmatch(r"B[0-9][A-Z0-9]{8}|[0-9]{9}[0-9X]", query.strip().upper())
        )
        found, errors, bounds, used_calls = self._discover(query, search_type, deadline, exact_asin)
        for error in errors:
            logger.warning("Edition discovery incomplete: %s", error)
        if any(s.searchable for s in self.sources) and not found:
            raise SourceError("All enabled discovery sources failed")
        warnings = errors + bounds
        positions = {
            record.reference.key: index
            for entries in found.values()
            for index, record in enumerate(entries)
        }
        records = sorted(
            (
                r
                for entries in found.values()
                for r in entries
                if self._accept_language(r.edition.language)
                and (
                    search_type != "series"
                    or words(literary_title(r.edition.series_name or ""))
                    == words(literary_title(query))
                )
            ),
            key=self._rank,
        )
        filtered = sum(
            not self._accept_language(r.edition.language)
            for entries in found.values()
            for r in entries
        )
        if filtered:
            warnings += (
                f"Language policy excluded {filtered} source records (including unknown if disabled)",
            )
        if not records and errors:
            raise SourceError("No accepted results; discovery incomplete: " + "; ".join(errors))
        groups: list[list[SourceRecord]] = []
        for record in records:
            group = next(
                (
                    g
                    for g in groups
                    if len(g) < 32 and all(self._same_recording(record, member) for member in g)
                ),
                None,
            )
            if group is None:
                groups.append([record])
            else:
                group.append(record)
        # Rank before the final cap. Similarity never participates in identity.
        groups.sort(
            key=lambda g: (
                self.options.language_mode == "prefer_english"
                and language(g[0].edition.language) != "en",
                min(relevance(member, query, search_type) for member in g),
                self._rank(g[0])[:3],
                positions[g[0].reference.key],
            )
        )
        if len(groups) > self.options.window:
            warnings += (
                f"Aggregate discovery window reached ({self.options.window} of {len(groups)} observed groups)",
            )
        for warning in warnings:
            logger.warning("Edition search bounded or filtered: %s", warning)
        selected = groups[: self.options.window][(page - 1) * limit : page * limit]
        # Search merges observed discovery batches, avoiding one network fan-out
        # per result. Only lookup-only sources and nonunique local aliases need
        # enrichment here; all such calls share the same search request cap.
        enrichment: list[tuple[str, Callable[[], AggregatedEdition]]] = []

        def resolver(
            members: tuple[SourceRecord, ...],
            targets: tuple[CatalogSource, ...],
            notes: tuple[str, ...],
        ) -> Callable[[], AggregatedEdition]:
            def resolve() -> AggregatedEdition:
                return self._resolve(
                    members[0], max(0, (deadline - monotonic()) * 0.8), notes, members, targets
                )

            return resolve

        for group in selected:
            known_sources = {member.reference.source for member in group}
            wanted = tuple(
                s
                for s in self.sources
                if not s.searchable or (s.key in known_sources and not s.unique_recording_ids)
            )
            targets_list: list[CatalogSource] = []
            for source in wanted:
                if used_calls + source.lookup_cost <= self.options.max_search_calls:
                    targets_list.append(source)
                    used_calls += source.lookup_cost
            targets = tuple(targets_list)
            notes = warnings + (
                ("Enrichment skipped: search request cap reached",)
                if len(targets) < len(wanted)
                else ()
            )
            enrichment.append(
                (encode_identity(group[0].reference), resolver(tuple(group), targets, notes))
            )
        calls2 = tuple(enrichment)
        resolved, failures = _parallel(calls2, self.options, max(0, deadline - monotonic()))
        if failures:
            logger.warning("Edition resolution incomplete: %s", ", ".join(failures))
        # A slow alias lookup must not erase an already verified edition. Fall
        # back to its observed search group and mark enrichment as incomplete.
        results: list[AudiobookEdition] = []
        for group in selected:
            result = resolved.get(encode_identity(group[0].reference))
            if result is None:
                result = self._merge(
                    group[0],
                    tuple(group),
                    warnings + failures + ("Enrichment incomplete: using verified search records",),
                )
            elif failures:
                result = replace(
                    result,
                    provenance=replace(
                        result.provenance,
                        warnings=tuple(sorted(set(result.provenance.warnings + failures))),
                    ),
                )
            # Optional enrichment can supply a previously unknown language.
            if self._accept_language(result.language):
                results.append(result)
        return results

    def get(self, catalog: str, edition_id: str) -> AudiobookEdition | None:
        if catalog != "aggregate":
            return None
        try:
            ref = decode_identity(edition_id)
        except ValueError, UnicodeError:
            return None
        source = self._sources.get(ref.source)
        if source is None or source.marketplace != ref.marketplace:
            raise SourceError("Selected source is disabled or unavailable")
        deadline = monotonic() + self.options.timeout
        record = source.get(ref.record_id, self.options.timeout)
        if record is None:
            return None
        self._checked(source, (record,))
        if record.reference.key != ref.key:
            raise SourceError("Detail lookup returned a different recording")
        return self._resolve(record, max(0, deadline - monotonic()))

    def by_isbn(self, isbn: str) -> AudiobookEdition | None:
        normalized = normalized_isbn(isbn)
        if normalized is None:
            return None
        found = self.search(isbn, search_type="isbn", limit=self.options.window)
        matches = [
            edition
            for edition in found
            if normalized
            in {normalized_isbn(edition.isbn_10 or ""), normalized_isbn(edition.isbn_13 or "")}
        ]
        return matches[0] if len(matches) == 1 else None
