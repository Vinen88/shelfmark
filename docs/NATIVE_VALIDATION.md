# Native editions validation and maintenance footprint

Validated November 7, 2026. Production was not activated. Results use the fork's
native build and application code with synthetic data and externally isolated tests.

## Results

| Check | Result |
| --- | --- |
| Native full-image Docker build, upstream Dockerfile/entrypoint | Passed |
| Upstream + edition Python unit suite | **6,063 passed**, plus 116 subtests |
| Native frontend unit suite | **275 passed**, 45 files |
| Python Ruff lint and formatting | Passed; 582 Python files formatted correctly at final check |
| BasedPyright application and `tests --skipunannotated` | Application, tests and deploy/recovery scripts passed, zero errors/warnings |
| Vulture production dead-code check | Passed |
| Frontend Oxlint, Oxfmt, TypeScript and Vite build | Passed |
| Chromium series/catalog browser regressions | Passed: grid/list/compact/mobile, keyboard disclosures, exact child Get, Details, series navigation and ownership badges |
| Chromium synthetic release modal/MAM regression | Passed: no release calls from grouping/view changes, correct source search counts and exact child Get |
| Normal native startup | Passed through the real entrypoint: health, protected API, native database, built index and asset bytes |
| Exact pinned official startup/fallback | Passed with real synthetic users.db schema, held edition requests/retries and preserved account |
| Held task restoration after official startup | Passed; requests and retry payloads restored without overwriting newer rows |
| Scratch ABS 2.37.2 ingestion | Passed with watchers disabled and actual automatic scan notifications |
| Real automatic recovery fault matrix | Passed: 16 CLI calls; 10 exit 0, 6 expected exit 2, no unexpected exits |
| Focused deploy/persistence regressions | 15 passed, including total failure exit 1, locks and Python 3.10 syntax |
| Root Compose validation / Git whitespace check | Passed |

The scratch workflow exercised provider search, exact recording selection, native
Prowlarr normalization, authenticated Get HTTP, the actual native queue, frozen
context, native folder planning/preparation/transfer, exact metadata/provenance,
real ABS scans and recording ownership. It covered GraphicAudio parts 1/2,
conventional M4B, a separate same-title recording with multiple MP3 files, the same
recording in MP3, already-completed output, failure/partial recovery and a French
series search. Hardlink inode equality, copy inode inequality and unchanged audio
digests were asserted. No real download transport was run.

The native application was also started separately through its normal entrypoint
with an empty synthetic configuration. The output workflow fixture simulates the
download client's completed-file handoff; it does not submit a real queue download
to a torrent/usenet client. Stock fallback was started with no source-code overlay.

The edition suite preserves recording ASIN/ISBN integrity, no-ASIN primary identity,
regional aliases, parts, both additional sources, bounded failures, language filtering,
series/ranking, exact detail resolution, requests/approvals, auth/user scope,
history/restart/retries, ownership and native output behavior. Obsolete wrapper/hash/
bootstrap tests were removed rather than carried forward as inactive infrastructure.

MAM production code has no diff. Tests used synthetic transports and isolated
network namespaces. Direct MAM and Prowlarr counts were measured separately:
native/edition/manual-context searches each made two synthetic Prowlarr API calls
and one Torznab call; direct synthetic MAM enrichment made one cold request and
zero warm/manual/expanded requests. Browser grouping/views made zero release API
calls; opening made one, and expand/manual/another recording each made one
additional call. Late metadata/rerenders/reopen did not add searches. These counts
describe fixtures, not actual tracker traffic. Zero live MAM requests were made and
no tracker credentials were retrieved.

Vite still reports its existing config-loader/theme-init/chunk-size advisories.
The optional `knip --production` check reports unused exports, one dependency and
standalone browser fixture entry points; it is not clean. The native required
`Makefile` checks (lint/format/types) and Vulture are clean. Production module
changes were not broadened to remove unrelated upstream exports/dependencies.

## Current custom production code comparison

This measurement includes the restored automatic deployment/recovery behavior.
No unchanged upstream code, complete copied frontend tree, tests, fixtures,
documentation, generated output, dependency lockfiles or deployment YAML/Dockerfile
syntax is counted as new custom production code.

Both old and new Python use Ruff 0.17.3 formatting; old source was formatted in
memory without modifying the reference. Python counts exclude blank/comment lines
and standalone docstrings, while retaining real `*args`/`**kwargs` source lines.
The earlier broad comment filter excluded 12 such historical Python lines; the
corrected reference total is 5,813. Frontend deltas use the same Oxfmt as before.
Native edits are gross additions against this fork's `985a5476…` baseline; the old
frontend/patch additions use its known `ed0777a2…` base. Removed upstream lines are
not subtracted to inflate savings.

| Maintained production source | Code LOC |
| --- | ---: |
| Previous custom Python production modules/tools, same formatting | 5,278 |
| Previous custom frontend additions | 462 |
| Previous pinned native patch additions | 73 |
| **Previous total** | **5,813** |
| Retained native metadata/recording business modules | 4,033 |
| Added calls/fields in existing native Shelfmark Python | 91 |
| Native frontend additions and two new presentation modules | 466 |
| Foreground deployment helper (`scripts/manage.py`) | 328 |
| Offline task/settings compatibility helper | 196 |
| **Current total, including automatic recovery** | **5,114** |

Reduction: **699 production source lines (12.0%)** with automatic official recovery
included. The initial 18.1% migration-only figure omitted the requested deployment
helper and is superseded by this complete measurement. Additional native application
and frontend code is 4,590 LOC; the two operational helpers add 524 LOC.

The old deployment manager measured 595 lines in the same formatting, compared with
328 in the new manager. Native registration, direct queue/matcher/mapper calls and
ordinary Docker startup eliminate ten old executable modules/tools: loader
`__init__.py`, `sitecustomize.py`, `deployment_health.py`, `handoff_runtime.py`,
`ownership_runtime.py`, `handoff_profile.py`, `handoff_overlay.py`, `output_profile.py`,
`output_overlay.py`, and `scripts/image_build.py`. These total 586 same-format code
lines (504 historical source lines). Three pinned patch artifacts and the old
derived-image pipeline are also absent. `scripts/manage.py` is replaced, not counted
as eliminated now that its required recovery feature is restored.

The remaining native provider package is 2,855 LOC: Libex/AudiobookDB/Audnexus
contracts, source HTTP budgets, identity, provenance, regional equivalence, ranking,
language/series logic and provider presentation. The other 1,178 business lines
handle trusted selection/snapshots, recording ownership and metadata-first output.
Their module-to-native-location map is in `NATIVE_MIGRATION.md`. These perform
active recording work; none loads an extension, patches a function or hashes source.
Native Shelfmark continues to supply requests, history/retries and file transfer.

Runtime dependencies added or eliminated: **0**. The app still uses upstream's
dependencies, and the host deployment helper uses Python stdlib plus the Docker CLI.
No deployment database, image history, service, background monitor or metadata crawler
was added. Active source/preference settings remain the same eight canonical fields;
no handoff, ownership, export or scan enable switch was introduced.

## Reproduce checks in disposable containers

Commands work in PowerShell and Bash from the fork root (use `python3` for the
host-side runner on Linux). Build/dependency installation needs registry access;
all test execution below disables external networking.

```text
docker build --target shelfmark -t shelfmark-fork-validation:local .
docker build -f tests/editions/Dockerfile.checks -t shelfmark-fork-checks:local .
docker build --target frontend-builder -t shelfmark-fork-frontend-validation:local .
docker build -f tests/editions/Dockerfile.browser -t shelfmark-fork-browser-checks:local .
```

PowerShell backend:

```powershell
$ForkSource = (Get-Location).Path
docker run --rm --network none --user 1000:1000 --workdir /tmp --env PYTHONPATH=/workspace --mount "type=bind,source=$ForkSource,target=/workspace,readonly" shelfmark-fork-checks:local python -m pytest -c /workspace/pyproject.toml -o addopts= -o cache_dir=/tmp/pytest-cache -n 4 -m "not integration and not e2e" /workspace/tests -q
docker run --rm --network none --mount "type=bind,source=$ForkSource,target=/workspace,readonly" shelfmark-fork-checks:local ruff check --no-cache shelfmark tests scripts/fallback_config.py
docker run --rm --network none --mount "type=bind,source=$ForkSource,target=/workspace,readonly" shelfmark-fork-checks:local ruff format --no-cache --check shelfmark tests scripts/fallback_config.py
docker run --rm --network none --mount "type=bind,source=$ForkSource,target=/workspace,readonly" shelfmark-fork-checks:local basedpyright
docker run --rm --network none --mount "type=bind,source=$ForkSource,target=/workspace,readonly" shelfmark-fork-checks:local basedpyright tests --skipunannotated
docker run --rm --network none --mount "type=bind,source=$ForkSource,target=/workspace,readonly" shelfmark-fork-checks:local vulture shelfmark
python tests/editions/run_e2e.py
```

Bash backend:

```bash
fork_source="$PWD"
docker run --rm --network none --user 1000:1000 --workdir /tmp --env PYTHONPATH=/workspace --mount "type=bind,source=$fork_source,target=/workspace,readonly" shelfmark-fork-checks:local python -m pytest -c /workspace/pyproject.toml -o addopts= -o cache_dir=/tmp/pytest-cache -n 4 -m 'not integration and not e2e' /workspace/tests -q
docker run --rm --network none --mount "type=bind,source=$fork_source,target=/workspace,readonly" shelfmark-fork-checks:local ruff check --no-cache shelfmark tests scripts/fallback_config.py
docker run --rm --network none --mount "type=bind,source=$fork_source,target=/workspace,readonly" shelfmark-fork-checks:local ruff format --no-cache --check shelfmark tests scripts/fallback_config.py
docker run --rm --network none --mount "type=bind,source=$fork_source,target=/workspace,readonly" shelfmark-fork-checks:local basedpyright
docker run --rm --network none --mount "type=bind,source=$fork_source,target=/workspace,readonly" shelfmark-fork-checks:local basedpyright tests --skipunannotated
docker run --rm --network none --mount "type=bind,source=$fork_source,target=/workspace,readonly" shelfmark-fork-checks:local vulture shelfmark
python3 tests/editions/run_e2e.py
```

Frontend checks are identical in either shell; each uses the native frontend stage:

```text
docker run --rm --network none shelfmark-fork-frontend-validation:local npm run lint
docker run --rm --network none shelfmark-fork-frontend-validation:local npm run format:check
docker run --rm --network none shelfmark-fork-frontend-validation:local npm run typecheck
docker run --rm --network none shelfmark-fork-frontend-validation:local npm run test:unit
docker run --rm --network none shelfmark-fork-frontend-validation:local npm run build
```

Browser checks, PowerShell (screenshots remain under ignored `.local/`):

```powershell
New-Item -ItemType Directory -Force .local/browser-artifacts | Out-Null
$ForkSource = (Get-Location).Path
docker run --rm --network none --mount "type=bind,source=$ForkSource/tests/editions/series_browser.mjs,target=/frontend/check.mjs,readonly" --mount "type=bind,source=$ForkSource/.local/browser-artifacts,target=/artifacts" shelfmark-fork-browser-checks:local
docker run --rm --network none --mount "type=bind,source=$ForkSource/tests/editions/mam_browser.mjs,target=/frontend/check.mjs,readonly" --mount "type=bind,source=$ForkSource/.local/browser-artifacts,target=/artifacts" shelfmark-fork-browser-checks:local
```

Bash:

```bash
mkdir -p .local/browser-artifacts
docker run --rm --network none --mount "type=bind,source=$PWD/tests/editions/series_browser.mjs,target=/frontend/check.mjs,readonly" --mount "type=bind,source=$PWD/.local/browser-artifacts,target=/artifacts" shelfmark-fork-browser-checks:local
docker run --rm --network none --mount "type=bind,source=$PWD/tests/editions/mam_browser.mjs,target=/frontend/check.mjs,readonly" --mount "type=bind,source=$PWD/.local/browser-artifacts,target=/artifacts" shelfmark-fork-browser-checks:local
```

## Review artifacts and Git

Implementation branch: `migration/native-editions`, based on
`985a54765d4fa6e9c48bd97393c47bf370b77dec`. `main` and upstream history were retained;
the working extension remains clean. All migration changes are unstaged/uncommitted.
The tracked diff contains the native migration, deployment documentation and a
Python-target override keeping the standalone CLI compatible with host Python 3.10.
New provider/core/output files, deployment helpers, tests, Compose, AGENTS and docs
remain unstaged/uncommitted alongside it.
Git may additionally report newline-only working-file changes from normalization;
`git diff --stat` shows the actual content edits. No frontend dependency upgrade or
native MAM source change is part of the diff.

```text
git status --short --branch
git diff --stat
git diff --check
git diff --cached --stat
git ls-files --others --exclude-standard
```

Local ignored evidence from this session: `.local/all-tests-verified.txt`,
`.local/frontend-checks.txt`, `.local/python-quality-final.txt`,
`.local/native-build-final.txt`, `.local/acceptance-final.txt`,
`.local/browser-series.txt`, `.local/browser-release-modal.txt`,
`.local/production-size.json` and `.local/browser-artifacts/`.
These are scratch validation artifacts, not runtime dependencies or release history.


## Follow-up validation: explicit exits and feature coverage

The actual upstream unit recipe is pytest excluding service-dependent `integration`
and `e2e` markers; it ran through the native `uv run --no-sync` toolchain in the
locked check image. It passed **5,803 upstream + 260 edition/deployment tests = 6,063**,
plus 116 subtests, exit **0**. The focused 15 deployment/persistence tests were also
rerun after strengthening the total-failure assertion; these overlap the full count.
Frontend lint, format, TypeScript, 275 unit tests and build each exited **0**.
Python lint/format, application/test/helper type checks and Vulture exited **0**.
Native full Docker build and the synthetic acceptance runner exited **0**.

The optional `knip --production` check was rerun and exited **1**: five flagged files,
one dependency and 15 exports. These include standalone test-harness entries and
existing upstream exports; this optional check is not part of Makefile `checks`.
It remains the only nonzero optional quality result, and was not hidden or reported
as passing. Vite's existing advisories remain warnings. No required test/build/check
has an unexpected failure.

| Feature group | Collected tests, all passed | Source |
| --- | ---: | --- |
| Libex live/historical | 17 | `test_libex.py`, `test_libex_contract.py` |
| AudiobookDB/Audnexus source contracts | 24 | `test_live_sources.py` |
| Recording identity, regional deduplication and multipart snapshots | 51 | `test_identity.py`, `test_multisource.py`, `test_handoff_completeness.py` |
| Language filtering | 6 | `test_language_filter.py` |
| Series navigation/ranking and relevant presentation | 26 | `test_series_search.py`, `test_search_refinement.py` |
| Native Prowlarr/MAM synthetic listing and enrichment counts | 19 | `test_native_mam.py`, `test_ui_search.py` |
| Trusted Get, auth, requests/approvals, persisted/in-memory retry | 36 | `test_edition_handoff.py` |
| M4B/MP3 collisions, safe native hardlink/copy and ABS scans | 25 | `test_abs_output.py` |
| Exact metadata/provenance export | 19 | `test_abs_metadata.py` |
| Edition ownership and ordinary-provider matching | 22 | `test_ownership.py` |
| Deployment, concurrency and settings/request/retry recovery | 15 | `test_manage.py`, `test_official_fallback.py` |

Browser series/catalog and fully intercepted release-modal regressions also exited
0. Synthetic acceptance used the built native image: exact recording selection,
Prowlarr fixtures, native Get/queue, byte-preserving transfer, exact sidecars, real
scratch ABS scanning/ownership, multipart and same-recording different formats,
completed and partial output, and French series searching. Native and exact official
startup used disposable configuration; all external networking was disabled.

The real recovery matrix ran seven deployment cases (successful native, failed build,
invalid image entrypoint, failed container command, unhealthy container, missing
edition registration, native reattempt), seven healthy status checks, manual fallback,
and a final native redeploy: **16 CLI invocations**. Expected exits were 0 for three
native deploys and seven statuses, and 2 for five automatic recoveries and manual
fallback. Each application image/health was verified. All four original scratch
mounts stayed identical and exactly one running container used the configuration.
Accounts/password hashes, global settings, settings edited during recovery, exact
request data and full frozen retry payloads were asserted. Every scratch app used
`network_mode: none`. Total recovery failure exit 1 is a mocked unit case, not a
claim that an intentionally broken official image was deployed.

Reproduce the fault matrix (safe default; creates only UUID-named scratch resources):

```powershell
python tests/editions/deployment_checks.py --image shelfmark-fork-validation:local
```

```bash
python3 tests/editions/deployment_checks.py --image shelfmark-fork-validation:local
```

The host-side matrix ran on Windows/Python 3.15 against Linux containers. Linux
fcntl locking and helper behavior ran in the native check image; host scripts were
checked for Python 3.10 syntax. Production deployment on either host was not run.

Current follow-up evidence lives in ignored `.local/followup-backend-final.txt`,
`followup-frontend.txt`, `followup-python-quality-final.txt`, `followup-native-build.txt`,
`followup-e2e-final.txt`, `recovery-matrix-final.txt`, `recovery-results.json`,
`followup-deploy-unit-final.txt`, `followup-browser-*.txt`, `followup-knip.txt`,
and `followup-production-size.json`. The reference installation is still intact;
availability inspection found its container absent, image/config/recovery assets
present and tracked Git worktree clean. No production operation, commit or push ran.
