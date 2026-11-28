# R21 migration and rollback — AGTools / AGCore / Gateway

## 1. Migration: none required

R21 is additive in every repository:

| Repository | Change | Data migration |
| --- | --- | --- |
| AGPilot0006 (AGTools) | new `/api/v1/knowledge/*` routes, new `app/knowledge/*` modules, new `data/knowledge/` store | **none** — a new store directory is created on first use; legacy `documents/` and `metadata.sqlite3` are untouched |
| AGPilot0005 (AGCore) | new `/api/v1/knowledge/*` routes and `app/knowledge/*`, feature flag `AGCORE_KNOWLEDGE_ENABLED` (default off) | **none** — workspace DB schema unchanged; artifacts are workspace files with ordinary revisions |
| AGPilot0010 (Gateway) | new capability snapshot/embeddings routes, new `capabilities.py`, `embeddings.py`, `capability_cli.py` | **none** — policy schema untouched; `model_capabilities` already exists in schema 7 |
| AGPilot0011 (HD2AI) | new `src/services/knowledge.ts` client | **none** — not wired into a screen yet, so no bundle behaviour changes |
| Server-HQ | release records and R3 interface docs | **none** |

Because the feature flags default to off, deploying the code before enabling it is safe:
the only visible difference is additional OpenAPI paths.

## 2. Enabling order

1. AGTools: `KNOWLEDGE_ENABLED=true` (default true; set false to hide the routes). Optional:
   `KNOWLEDGE_VISION_CAPABILITY_FILE` pointing at the Gateway export.
2. Gateway: export capabilities (`python3 capability_cli.py export --policy … --out …`) and
   place the file where AGTools reads it. This is what turns vision on — there is no model name
   in config on the AGTools side.
3. AGCore: `AGCORE_KNOWLEDGE_ENABLED=true` and `AGCORE_KNOWLEDGE_ALLOWED_SERVICES=hd2ai-beta,…`.
   Keep the list explicit; an empty list means "no service restriction".
4. HD2AI: only after the client is wired to a screen; the client itself is inert.

## 3. Rollback

Order matters: stop writers first, then the reader layer.

1. AGCore: set `AGCORE_KNOWLEDGE_ENABLED=false` (or remove AGCore from the routes). No schema
   change means no database restore is needed. Workspace artifacts created through the
   knowledge API remain ordinary workspace files; leave them, or delete them through the
   workspace API.
2. AGTools: set `KNOWLEDGE_ENABLED=false`; the legacy document endpoints keep serving the R2
   flow. The `data/knowledge/` directory can stay — it is only read by the disabled routes.
   If a full removal is required, move it aside first (never `rm -rf` while a request may be
   in flight): `mv data/knowledge data/knowledge.disabled-$(date +%s)`.
3. Gateway: the capability routes are read-only except `/embeddings`. Disabling them is a
   deployment removal; no policy schema change was made, so **do not** restore an older policy
   to "roll back" capabilities. WO-R2-CLOSE warns explicitly against restoring schema 6 without
   a compatibility review.
4. HD2AI: revert the commit; no persisted client state.

## 4. Data introduced by R21 (and how to read it)

```
AGTools  {HD2_DATA_DIR}/knowledge/
         ├─ {document_id}/meta.json                  revisions + hashes (atomic replace)
         ├─ {document_id}/revisions/{n:04d}/{name}   immutable payloads
         └─ index.sqlite3                            knowledge_chunks + optional FTS5 table
```

* `meta.json` is the commit point; a payload without meta is an orphan and is never served.
* Revisions are pruned to `KNOWLEDGE_MAX_REVISIONS` (default 20) oldest-first; rollback writes a
  **new** revision rather than rewriting history.
* To rebuild the search index from payloads: delete `index.sqlite3`; it is recreated and
  repopulated on the next index call (`index_document`), with no dependency on the legacy DB.

## 5. Gateway contract differences (WP-09)

Recorded from WO-R2-CLOSE (§운영 핀과 소스 증빙, Gateway row) and re-checked in
`capabilities.contract_differences()`:

| id | status | what it means for R21 |
| --- | --- | --- |
| `readiness_stub` | open | the Gateway readiness probe answers differently for non-chat routes than the AGCore expectation; R21 does not depend on readiness, so it does not block the knowledge engine |
| `auth_fixture` | open | the Gateway test suite and downstream suites use different auth fixtures; R21 tests use the Gateway's own fixture and do not assert on the shared one |
| `rate_limit_fallback` | open | embedded provider rate-limit fallback differs from the documented 429; `embeddings.py` maps a provider 429 to `busy` (429) and never silently retries |
| `capability_snapshot_added` | additive | new in R21; nothing removed |

These are **recorded, not silently fixed**: they belong to the R2→R3 promotion decision, and
changing them in a pre-development branch would hide the difference the close document asked to
confirm.

## 6. Recorded test runs (R21 pre-development)

| Repository | Command | Result |
| --- | --- | --- |
| AGPilot0006 | `python3 -m pytest -q` | see §6.1 |
| AGPilot0005 | `python3 -m pytest -q` | see §6.2 |
| AGPilot0010 | `python3 -m pytest tests/test_capabilities.py tests/test_gateway.py -q` | 14 + gateway suite pass; readiness/release/socket tests fail on the snapshot itself (see §6.3) |
| AGPilot0011 | `node --test tests/knowledge-client.test.mjs` and `npx tsc -b` | 6 pass, typecheck clean |

### 6.1 AGTools

```
python3 -m pytest -q
2 failed, 98 passed in 10.04s
FAILED tests/test_api.py::test_root_page            (base 6d0f57f, copy claim)
FAILED tests/test_search_engine.py::test_track_a_web_search_fallback_cache_and_admin_api
                                                    (external web-search dependency)
```

R21 knowledge modules: 57 tests, all passing (`test_knowledge_contracts/vision/pdf/docx/xlsx/
pptx/service/api`).

### 6.2 AGCore

```
python3 -m pytest -q
1 failed, 445 passed in 34.60s
FAILED tests/test_r1_2_final_addendum.py::test_add_02_durable_analysis_real_orchestrator_create_resume_and_no_legacy_fallback
                                                    ('_DummyNoop' has no 'create' — base d7a03f5)
```

R21: 21 tests (`test_r21_contract_conformance.py` 13, `test_r21_knowledge_federation.py` 8),
all passing.

### 6.3 Cross-service wire smoke

Because the repositories cannot import each other, a sandbox script started AGTools with real
uvicorn and drove it through AGCore's `AgtoolsProvider`: manifest, DOCX and PDF `read-inline`,
upload, hybrid search (degraded keyword + citation), validate — **12/12 checks passed**
(`/workspace/artifacts/r21/verify_cross_service.py`).

### 6.4 Gateway snapshot baseline

`tests/test_gateway.py` on the R21 branch: **9 failed, 37 passed**, and the same nine test ids
fail on a clean worktree at base `df382df` (verified by diffing the failure lists). They are the
route-registry, dynamic-key and embedded rate-limit cases that WO-R2-CLOSE already lists as
contract differences — not R21 regressions. `tests/test_readiness.py` fails in the snapshot with
`AttributeError: 'types.SimpleNamespace' object has no attribute 'execution_policy'` (the test
fixture predates the `execution_policy()` API), and `tests/test_release.py`,
`tests/test_socket_mode.py`, `tests/test_startup_repair.py` import `scripts/*` that are not part
of the snapshot. Both are pre-existing conditions of `df382df`, not R21 regressions; they match
the "레거시 준비도 스텁·인증 fixture 차이" already listed in WO-R2-CLOSE.
