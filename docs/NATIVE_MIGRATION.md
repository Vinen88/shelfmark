# Native editions migration

The current book-first UI and post-download identification contract is documented
in [Book-first downloads](BOOK_FIRST.md). The migration baseline below predates it;
selected catalog snapshots alone no longer establish recording metadata for import.

Status: implementation and isolated validation only. Production activation requires
separate review. No production installation, configuration, database, media or client
was modified. No commit, push, PR or image publication was performed.

## Baseline and decision

The actual reference directory is `../extension/` (singular). Both tracked worktrees
were clean before migration:

| Repository | Branch | Baseline commit |
| --- | --- | --- |
| Reference extension | `main`, tracking `origin/main` | `b4e6a681c2091db39c063c6c54c19c934eb73f24` |
| Destination fork | `main`, tracking `origin/main` | `985a54765d4fa6e9c48bd97393c47bf370b77dec` |
| Working upstream base, Shelfmark 1.6.1 | retained in fork history | `ed0777a2e0614dcb3104b123bc13ebdf41caf463` |

The working official base is
`ghcr.io/calibrain/shelfmark@sha256:0e5c9ff5f858a9695dc18bc355d8aa29dfee5c0d9c634919484c2bab9955ad5a`.
The cached `shelfmark-edition:local` Python sources match the reference's canonical
plugin files after newline normalization, including recent collision handling.
No uncommitted tracked extension changes were found. Private ignored deployment/data
files remain private; they were not imported into this fork. No Shelfmark container
was running at the read-only container inventory taken during this task.

The fork is 25 changed files newer than the working base. Provider interfaces, file
transfer/output and models remain compatible. Retain the newer fork rather than
rewinding: it includes safer API error messages, failed-release request handling,
dependency/security updates, a mobile release-modal fix and admin-only debug controls.
The custom frontend's **complete** file delta was compared with the pinned upstream
source: only changed/new files were ported, with a three-way merge for `App.tsx`.
The existing custom release-search hook was ported while the fork's newer modal
and tooltip behavior remains in place.
Windows shell/frontend line endings were normalized and `.gitattributes` prevents
CRLF shell entrypoints in future Windows Docker builds.

`origin` remains `https://github.com/Vinen88/shelfmark.git`; `upstream` is
`https://github.com/calibrain/shelfmark.git`. No Git histories were merged or reset.
The implementation is uncommitted on local branch `migration/native-editions`,
created from the destination baseline; the original `main` branch was not moved.

## Native source map

All old names below are relative to `extension/plugins/edition_plugin/`.

| Existing responsibility/files | Native destination |
| --- | --- |
| `libex.py`, `audiobookdb.py`, `audnexus.py`, `source_http.py`, `catalog_fields.py` | Same business modules under `shelfmark/metadata_providers/editions/` |
| `models.py`, `identity.py`, `catalog_types.py`, `regional.py`, `snapshot.py` | Same native provider package; exact recording identities/provenance and old snapshot schemas retained |
| `backend.py`, `multisource.py`, `multisource_config.py`, `search_ranking.py`, `presentation.py` | Native provider discovery, preferences, ranking and display projections |
| `shelfmark_provider.py` | `metadata_providers/editions/provider.py`; imported normally by the native provider registry |
| `edition_handoff.py` | `core/edition_handoff.py`; called directly by `download/orchestrator.py` and native request/approval code |
| `ownership.py` | `core/edition_ownership.py`; called by `core/library_index.py`; ABS recording facts are populated in the native ABS mapper/LibraryEntry |
| `abs_metadata.py`, `abs_output.py` | `download/outputs/abs_metadata.py` and `edition.py`; called by native folder output, with native transfer functions |
| Modified frontend | Relevant edits in `src/frontend/src`; no separate frontend tree/state framework |
| Offline `scripts/fallback_config.py` | Same narrow offline helper, with an explicit config directory CLI |
| `__init__.py` loader, `sitecustomize.py`, `deployment_health.py` | Eliminated: native registration and startup/health suffice |
| `handoff_runtime.py`, `ownership_runtime.py` | Eliminated: direct queue/matcher/mapper implementations |
| `handoff_overlay.py`, `handoff_profile.py`, `output_overlay.py`, `output_profile.py` and three patch files | Eliminated: direct source edits and regression tests |
| `scripts/image_build.py`, old derived Dockerfile | Eliminated: native Dockerfile and root Compose |
| Old `scripts/manage.py` deployment/test/build manager | Replaced by a small native `scripts/manage.py` foreground CLI; automatic verified official recovery retained |

The remaining catalog/backend/snapshot code performs active recording business
logic. It has no bootstrap, source hashing, function replacement, version gating,
external code mount or runtime dependency on the reference workspace.

## Configuration and output behavior

Select **Audiobook Editions** as the audiobook metadata provider. Core handoff,
ownership, metadata export and scan notification are always part of the application.
Sources retain exactly the existing minimal configuration:

- Libex: `EDITION_LIBEX_ENABLED`.
- AudiobookDB: `EDITION_AUDIOBOOKDB_ENABLED`, `EDITION_AUDIOBOOKDB_API_KEY` (server-side PasswordField).
- Audnexus: `EDITION_AUDNEXUS_ENABLED`, `EDITION_AUDNEXUS_URL` for an authorized endpoint.
- Existing marketplace/language preferences: `EDITION_MARKETPLACES`,
  `EDITION_LANGUAGE_MODE`, `EDITION_INCLUDE_UNKNOWN_LANGUAGE`; native per-user search
  language preferences and explicit language selections remain supported.

Sources default off. Existing credentials, API access and catalog authorization
requirements remain. URLs, priorities, User-Agent and budgets are internal. No MAM
client/cache/limiter was added or changed; see the permanent root AGENTS restrictions.

Edition output retains the existing one-book **organize** folder requirement. Use a
dedicated book directory, e.g. `{Author}/{Title}/{Title} - {PartNumber}`. Multi-book
packs, unsafe layouts and custom output scripts still fail closed for edition output.
Ordinary downloads keep native behavior. Native copy/hardlink/move handles audio;
audio bytes and tags are unchanged. ABS handles artwork. Configure the existing ABS
URL, token and destination library selection. If multiple libraries are ambiguous,
the existing `SHELFMARK_EDITION_ABS_LIBRARY_ID` override remains available.

Collision reuse requires the same recording **and** exact requested output paths,
sizes and audio digests. M4B cannot satisfy MP3. Partial imports resume only verified
source bytes under unchanged sidecars; changed recordings, user files and conflicting
layouts receive a separate safe directory or an error. No new job/storage ledger exists.
Scan notification is best effort after successful publication; scan failure never
changes a completed transfer. Periodic/manual ABS scans can recover missed notifications.

## Isolated build and verification

PowerShell, from `N:\docker_images\shelfmark-workspace\shelfmark`:

```powershell
docker build --target shelfmark -t shelfmark-fork-validation:local .
python tests/editions/run_e2e.py --image shelfmark-fork-validation:local
```

Linux Bash, from the fork checkout:

```bash
docker build --target shelfmark -t shelfmark-fork-validation:local .
python3 tests/editions/run_e2e.py --image shelfmark-fork-validation:local
```

The runner uses fresh UUID-named volumes/containers, an internal network for scratch
ABS 2.37.2 and network-disabled native/official startup tests. No host media/config
is mounted. Only synthetic catalogs, release transports and generated audio are used.
ABS watchers are disabled. Cleanup targets only resources created by that run.
See `NATIVE_VALIDATION.md` for unit, quality and browser commands.

For a standalone new scratch instance (not the production migration):

```powershell
Copy-Item .env.example .env
python scripts/manage.py deploy --project shelfmark-fork-lab
```

```bash
cp .env.example .env
chmod 600 .env
python3 scripts/manage.py deploy --project shelfmark-fork-lab
```

Do not overwrite an existing private `.env`. The defaults use port 18084 and `.local/run/`.
The native image inherits Shelfmark's normal entrypoint, Gunicorn and healthcheck.

## Future production migration — commands not executed

Review this plan separately. First drain the native queue or explicitly review/cancel
unfinished application tasks; leave client downloads/seeding files intact. Preserve
retryable native history and requests. Never copy an active SQLite database with a
plain filesystem copy and never run two applications against one configuration.

The existing deployment uses service/container `shelfmark`, port `8084:8084`, and
the four private path variables `SHELFMARK_CONFIG_ROOT`, `SHELFMARK_BOOKS_ROOT`,
`SHELFMARK_MEDIA_ROOT`, `SHELFMARK_DOWNLOADS_ROOT` mapping to `/config`, `/books`,
`/media`, `/downloads`. Their private host values were not retrieved. Keep these
container targets and client path mappings. Keep PUID/PGID and existing permissions.

1. Build and validate the fork before any outage. Retain the exact working custom
   image and pinned official image locally.
2. With approval, stop the old application. Leave ABS/download clients alone.
3. Copy the complete stopped config into a **new** fork config directory. This keeps
   accounts, password hashes, permissions, native settings, users.db history/requests
   and frozen retry payloads. Keep the original config untouched for rollback.
4. Copy the private `.env` locally, then review it without logging secrets. Convert
   old relative host paths to absolute paths: `.env` moved between directories changes
   how relative paths resolve. Point `SHELFMARK_CONFIG_ROOT` to the new config copy.
5. Remove legacy `PYTHONPATH`, `GUNICORN_CMD_ARGS`, code mounts and obsolete feature
   switches if present. Keep native source/client/ABS settings, credentials and all
   meaningful authorization flags. `.env` is passed to the service without publishing it.
6. Preserve `edition_metadata`, provider IDs, catalog settings, snapshot schemas and
   per-user selections. No provider ID/database conversion is needed. Native schema
   migration may add the newer fork's ordinary columns on the copied database.
7. To preserve service identity, set `SHELFMARK_PROJECT=shelfmark`,
   `SHELFMARK_CONTAINER_NAME=shelfmark`, `SHELFMARK_PORT=8084` and the original bind
   address (`0.0.0.0` in the inspected Compose port mapping). Set
   `SHELFMARK_IMAGE=shelfmark-fork:local`, `SHELFMARK_ENV_FILE=.env`.
8. Only after the old service is stopped/removed and the copied config is reviewed,
   start the fork and review health, login/roles, queue/history/requests and paths.
   Do not resume pending downloads until their exact snapshots/paths are reviewed.

PowerShell future sequence (replace the config path with its private absolute value):

```powershell
Set-Location N:\docker_images\shelfmark-workspace\shelfmark
docker compose --project-directory ..\extension -f ..\extension\compose.yaml stop shelfmark
$OriginalConfig = 'ABSOLUTE_ORIGINAL_CONFIG_DIRECTORY'
New-Item -ItemType Directory -Force .local\migration | Out-Null
Copy-Item -LiteralPath $OriginalConfig -Destination .local\migration\config -Recurse
Copy-Item -LiteralPath ..\extension\.env -Destination .env
# Review/edit .env as described above; do not print it or overwrite an existing file.
docker compose --project-directory ..\extension -f ..\extension\compose.yaml rm -f shelfmark
python scripts/manage.py deploy --project shelfmark
docker compose -p shelfmark ps
```

Linux Bash future sequence (equivalent private config path):

```bash
cd /path/to/shelfmark
docker compose --project-directory ../extension -f ../extension/compose.yaml stop shelfmark
original_config='/absolute/original/config'
mkdir -p .local/migration
cp -a "$original_config" .local/migration/config
cp ../extension/.env .env
chmod 600 .env
# Review/edit .env as described above; do not print it or overwrite an existing file.
docker compose --project-directory ../extension -f ../extension/compose.yaml rm -f shelfmark
python3 scripts/manage.py deploy --project shelfmark
docker compose -p shelfmark ps
```

These examples assume a new `.local/migration/config` and no existing fork `.env`.
Choose a new destination instead of merging/overwriting an earlier migration attempt.

## Deployment and automatic official recovery

Use the foreground helper from this fork (host Python 3.10+ and Docker Compose):

```powershell
python scripts/manage.py deploy
python scripts/manage.py fallback
python scripts/manage.py status
```

```bash
python3 scripts/manage.py deploy
python3 scripts/manage.py fallback
python3 scripts/manage.py status
```

The root Compose is the one source of paths, port, identity and private environment.
`--compose PATH` selects another Compose file, `--project NAME` selects the target
project, and `--timeout 90` sets each bounded readiness window (5–600 seconds).
The default paths/project remain the isolated lab; production is selected only after
the separately approved copy/migration steps above. Plain `docker compose up` builds
normally but bypasses automatic recovery; use the helper for deployments.

Exit codes:

| Exit | Meaning |
| --- | --- |
| 0 | Native deployment succeeded, or `status` verified the current service healthy |
| 2 | Pinned official recovery verified healthy, including explicit `fallback` |
| 1 | Deployment/recovery/status failure; never a claim of successful recovery |

`deploy` first checks target ownership and configuration sharing, then ensures the
exact pinned official image is cached locally. If that prerequisite fails, it leaves
the existing service alone. It builds the candidate before stopping the target,
then starts a disposable, network-disabled candidate using empty configuration.
It verifies the actual image ID, Docker health, local HTTP health and native edition
registration. No production data or credentials enter the candidate check.

Only after that succeeds does it stop the target, confirm no running container is
using its config, prepare persisted compatibility state offline, and start the fork
with the existing mounts. It checks image identity and health again against the
real selected configuration. Foreground commands from the checkout are serialized
with an OS file lock. A failed stop prevents compatibility writes or another start.

Build, candidate, startup or readiness failure invokes official recovery. Recovery
stops the same target, holds incompatible edition requests/retries in the existing
users.db, then starts the **unmodified pinned official image** with its own normal
entrypoint and healthcheck. Ports, configuration, media and download-client mounts
are retained. Native-only environment values are removed from the in-memory Compose
input, and stock metadata defaults to Open Library. No `.env` rewrite or generated
recovery Compose file is needed. Credentials and resolved Docker output are not printed.

The manager uses `fallback_config.py --keep-config-files`: existing global JSON
settings are not rewritten or replaced with backups. Per-user edition selections
are temporarily converted, with only the changed fields retained in the existing
hold table. A later `deploy` restores held requests/retry snapshots and unchanged
provider selections, preserving unrelated/newer settings and account password hashes.
Newer conflicting requests or retries are refused for manual review, never overwritten.
This is compatibility preservation, not a deployment database or a release history.

Official recovery is reported only after verifying the pinned image ID, its health
and HTTP endpoint, and absence of the native edition provider. A second failure exits
1 with data retained. Readiness defaults to 90 seconds per phase; individual Docker
operations also have finite timeouts (build 30 minutes, image pull 10 minutes, normal
operations two minutes). No background monitor, automatic later failover or zero-downtime
guarantee is provided. Queues still need the pre-migration drain/review described above.

The official image remains:

```text
ghcr.io/calibrain/shelfmark@sha256:0e5c9ff5f858a9695dc18bc355d8aa29dfee5c0d9c634919484c2bab9955ad5a
```

Operator logs (read privately; they may contain application/client details):

```text
docker compose -p shelfmark logs --tail 100 shelfmark
docker compose -p shelfmark logs -f shelfmark
```

`status` is read-only and reports the current image mode, state and health. It exits
1 if the service is absent or fails verification. `fallback` always uses the same
stopped-database preparation and verified official startup as automatic recovery.
No migration, restart or recovery against production has been executed in this task.

## Rollback to the working extension

Read-only inspection found the old `shelfmark` container absent (neither running
nor stopped). Its cached `shelfmark-edition:local` image, Compose, private `.env`,
`config/users.db`, plugin settings directory, backups and old manage/fallback scripts
are still available. Their contents were not opened for this availability check.
The original `extension/` config, Compose and `.env` remain untouched. After stopping
the rejected fork and retaining its separate config, remove only its stopped service
container (no `-v`). Start the original service using its own Compose project/env:

```powershell
docker compose -p shelfmark stop shelfmark
docker compose -p shelfmark rm -f shelfmark
docker compose --project-directory ..\extension -f ..\extension\compose.yaml up -d --no-build shelfmark
```

```bash
docker compose -p shelfmark stop shelfmark
docker compose -p shelfmark rm -f shelfmark
docker compose --project-directory ../extension -f ../extension/compose.yaml up -d --no-build shelfmark
```

Review any requests/history created while the fork was active against the retained
fork config before resuming them; do not blindly merge live databases. Existing media
is preserved, including new completed imports. No media cleanup, retagging, schema
downgrade, custom rollback-image history or release-management database is needed.
