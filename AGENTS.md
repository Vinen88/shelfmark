# Shelfmark fork

This private fork keeps audiobook editions in native Shelfmark source. Favor minimal,
maintainable code. Port working behavior; reuse native providers, routes, requests,
queue/history/retries and copy/hardlink/move utilities. Preserve ordinary providers.
Do not add plugin loaders, runtime monkey-patches, source-hash guards or overlays.

Catalogs and identity live in `shelfmark/metadata_providers/editions/`; trusted Get
resolution is in `core/edition_handoff.py`, ownership in `core/edition_ownership.py`,
and recording metadata output in `download/outputs/edition.py` and `abs_metadata.py`.
Never collapse literary works into recording identities or combine GraphicAudio parts.
Keep exact source/ID-bound snapshots. Do not retag audio or change hardlinked bytes.
ABS manages artwork. Optional catalog sources require their existing authorized access.

Book-first presentation groups normalized literary title/full authors, with explicit
series/position conflicts kept separate. Catalog work IDs are evidence, not display
group keys. Keep every original child recording and its exact ownership/selection.
Get book is an explicit work intention in native output_args (frozen context v2);
Get this edition retains exact handoff (v1). Requests/approvals must preserve intent.
Use the existing bounded metadata cache for discovered snapshots; namespace by source
settings. Never perform catalog/tracker lookups during post-download identification.
`download/outputs/audio_identification.py` reads Mutagen headers/tags only. Before
publishing sidecars, return exact recording, unknown recording, or conflict. Never
publish a selected ASIN/ISBN merely because it was selected. Unknown imports use
literary metadata and unresolved provenance; conflicts retain files with a useful error.
Keep existing collision/inventory/partial recovery code; no extra publication ledger.
Ownership comes from the accessible native ABS index, not queue/scan acknowledgement.
Completion triggers bounded batched UI ownership refresh, not a per-book polling service.

## MyAnonamouse — Restricted External Service

- Never contact live MAM, directly or indirectly through production Prowlarr, during
  development or automated testing. Prior successful requests and read-only access
  are not authorization.
- Never automate login, scrape MAM, retrieve or expose tracker credentials, cookies,
  session IDs or passkeys. Do not probe sessions, health or limits.
- Only documented or administrator-approved API operations may be automated. The
  user supplied Rules §1.7 dated October 7, 2026; do not fetch MAM to verify them.
- Preserve native batched narrator/series/bitrate enrichment, caching and backoff.
  No additional MAM client, limiter, queue or cache. Only change native behavior for
  a demonstrated compatibility bug with a focused regression.
- Use synthetic fixtures with network isolation. Count direct MAM and Prowlarr
  requests separately. Mocked counts do not establish actual tracker traffic.
- Never modify production MAM settings without explicit approval, immediately retry
  after throttling, rotate sessions/proxies or introduce automatic login.

## Workspace and validation

`../extension/` is a read-only reference and the working production installation.
Never modify, rename, delete, restart or migrate it as part of fork development.
Never run two Shelfmark processes against a configuration/database. Use disposable
containers with synthetic configuration, downloads and media and isolated names/ports.
Do not access private `.env`, production databases, media or client credentials.
Install test dependencies only in disposable containers. Run upstream Python/frontend
tests and checks plus `tests/editions/`. Network-isolate test execution.

Use the native Dockerfile and startup. Keep official fallback offline and preserve
incompatible pending edition requests/retries. Production activation, commits, pushes,
PRs and image publishing require explicit approval. Preserve upstream history/license.
See `docs/NATIVE_MIGRATION.md` for the baseline, mapping, validation and migration plan.

`python scripts/manage.py deploy|fallback|status` is the foreground deployment CLI
(`python3` on Linux). Deploy builds/verifies against isolated scratch config before
replacing the target, and recovers to the exact official image on failure. Never bypass
its stopped-database compatibility preparation or reuse production mounts in tests.
Keep source/client credentials out of output. Test recovery using
`tests/editions/deployment_checks.py`; every app container there has network disabled.
Read-only availability checks may inspect container state and asset existence only.
