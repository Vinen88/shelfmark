# Book-first audiobook downloads

Search results show one literary book per main card. Normalized literary title and
full author names establish browsing groups; explicit series/position conflicts keep
different books apart. Different catalog work IDs, ASINs, narrators, publishers,
years and marketplaces do not split a parent. All returned catalog records remain
under **View editions**, with their original provider IDs, provenance and ownership.
The grouping is recomputed from the complete accumulated results after pagination.
It never asserts that two recordings are equivalent.

**Get book** searches the native release sources for title/author and useful literary
context. A title identical to its series includes the literary book number in the
release query. No recording ISBN, narrator, publisher or recording year is required.
Choose a release in the normal modal; filters, manual overrides, indexers, native
source IDs, requests, approvals, history and retries retain their existing behavior.

**Get this edition** keeps the selected exact provider ID and trusted frozen
snapshot. Neither intention changes the source release identity. Generic work
requests explicitly carry their intention through native request/fulfilment paths.
Unattended generic requests use work ownership; exact-edition requests keep the
strict recording ownership guard.

## Local identification before import

The native folder output reads audio headers/tags with Mutagen before writing
sidecars. MP3/ID3, M4B/MP4 and other supported Mutagen formats can supply album/title,
author, narrator, publisher, identifiers, language, date, duration, track/disc and
chapter counts. It reads audio files only, never rewrites them, and never decodes the
whole audio stream. Missing, malformed or unsupported tags are unknown evidence.
Filenames and release titles do not establish exact recording identity.

Previously discovered trusted catalog snapshots are retained in Shelfmark's existing
bounded metadata cache, scoped to source settings. A work task freezes its available
compatible candidates in native output_args, so restart/retry does not need a new
catalog search. Expired additional candidates are omitted rather than fetched merely
to improve identification. The ordinary selected-book lookup remains available when
needed to queue a request. There is no new identification service or database.

The output distinguishes three outcomes:

- **Exact recording:** validated recording ASIN or attested recording ISBN, compatible
  recording facts and a unique trusted candidate. The existing strict ownership
  matcher checks identifier, narrator, publisher, language, production and part
  contradictions. Identifier-only tags do not suffice. Export the identified
  snapshot with its provenance, not an unrelated selected recording.
  When the primary catalog supplies a duration and every audio header is readable,
  a total duration outside 80–120% vetoes exact identification. Duration alone never
  identifies a recording.
- **Work known, recording unknown:** use the requested literary title, authors and
  series/position. Do not publish a catalog ASIN, ISBN, narrator, publisher, production
  or recording year. Explicit observed part information may qualify the title.
  `.shelfmark-edition.json` records unresolved identity without a fabricated snapshot.
- **Conflict:** embedded work/author/position or selected-part evidence contradicts
  the request. An explicit edition request with contradictory recording identifiers
  stops with an explanation to review the files or use **Get book** for a work-level
  import. It never silently substitutes another edition for an explicit selection.

An exact selection with no usable identifying evidence imports the literary work
without claiming the selected recording. Inspection is best effort; no matching
catalog edition is fabricated. Existing source bytes, hardlinks, distinct formats,
collision suffixes, user files and verified partial-import recovery are preserved.
Artwork remains Audiobookshelf's responsibility.

## Ownership and completion

Completion means the native transfer succeeded. A scan acknowledgement does not
mean ABS owns a book yet. Successful imports request the existing ABS scan and expire
its native index. Existing activity completion events schedule a bounded batch of
visible-result ownership refreshes (immediate, 2, 10 and 30 seconds). Those requests
read the same accessible ABS index; they do not search catalogs or poll per book.
If ingestion takes longer, the next ordinary library refresh/search recognizes it.
Existing later ABS metadata can establish exact recording ownership without a new
background identification loop.

## Validation and deployment

Validated November 8, 2026: 6,087 upstream/fork Python tests and 127 subtests;
277 frontend tests plus lint, formatting, types and build; backend Ruff,
BasedPyright and Vulture; Chromium grid/list/compact/mobile scenarios for The Last
Battle, series results, catalog-only rows and Primal Hunter. Generated MP3/M4B/WAV
imports passed real scratch ABS checks for exact identification, explicit conflicts,
unknown recording metadata, ownership, byte preservation and partial recovery.
The Docker recovery matrix passed all 16 checks. Native/official startup additionally
preserved and restored work intent and frozen candidate snapshots. No production
deployment or live tracker requests were used.

Use the existing upstream/fork Python suites, frontend checks and unit tests, isolated
Chromium harnesses, `tests/editions/run_e2e.py` and
`tests/editions/deployment_checks.py`. The latter exercise only disposable scratch
resources. Audio fixture tagging occurs only in tests. Live MAM and production
Prowlarr are never contacted during development.

After reviewing the diff and validation, back up the intended configuration and
review Compose paths. To activate explicitly, use the existing foreground CLI:

```text
python scripts/manage.py deploy --compose <reviewed-compose-file>
python scripts/manage.py status --compose <reviewed-compose-file>
python scripts/manage.py fallback --compose <reviewed-compose-file>
```

Use `python3` on Linux. The CLI builds and verifies a scratch candidate before replacing
the selected target, and automatically recovers to the pinned official image on
failure. Official fallback holds edition/work requests and retries in the existing
offline compatibility files, restoring them on return to the fork. Never share a
live configuration/database between instances. No production activation, restart,
commit, push or image publication is performed as part of this implementation.
