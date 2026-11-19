# HD²Lab Document Intelligence Service (AGPilot0006)

> **Standalone document parsing, hierarchical structuring and query API**
> *Version 2.0 • PDF • DOCX • PPTX • hierarchy preservation • full-slide rendering • original asset extraction • AGCore (AGPilot0005) integration*

---

## 1. Overview

**HD²Lab Document Intelligence Service** is an independent backend that parses, structures, stores and serves unstructured documents so that an LLM or agent (AGPilot0005) can understand them and query them on demand through tool calls.

Instead of flattening a document into one blob of text or rough Markdown, it preserves:

- **Document tree** — chapters, sections, paragraphs, tables and figures with their hierarchy and reading order
- **Visual composition** — original embedded images plus high-resolution renders of whole pages and slides
- **Grounding** — the mapping between body text and the figures or charts it refers to
- **Token-aware chunking** — target 1,000 tokens, hard ceiling 1,500, 100-token overlap, so Korean and English context survives

Documents are parsed asynchronously: an upload returns a job id, and the client polls the status or reads the outline, sections, chunks, figures and manifest once parsing completes.

---

## 2. Features

- **Parsers** — PyMuPDF / `pymupdf4llm` for PDF, `python-docx` for DOCX and `python-pptx` for PPTX, dispatched by file type (`app/parsers/`). Page and slide rendering uses PyMuPDF, with an optional LibreOffice renderer (`LIBREOFFICE_PATH`) for formats that need it.
- **Hierarchy engine** — builds the section tree and assigns each section its source location (page or slide) and connected figure ids.
- **Hybrid retrieval** — `POST /api/v1/documents/{id}/search` combines SQLite FTS5 / BM25 keyword search with a SQLite vector index by cosine similarity, fused with reciprocal rank fusion, and returns snippets with source locations and figure ids. If either index is unavailable, the other path still answers.
- **Asset preservation** — original figure binaries and page/slide renders are streamed directly, and every document has a manifest with its asset catalogue and checksums.
- **Job management** — bounded concurrency (`MAX_CONCURRENT_JOBS`, default 4), a 300 s parsing timeout, and startup recovery for jobs interrupted by a restart.
- **Security limits** — 100 MB upload ceiling, 500-page limit, ZIP entry/expansion ratio limits, id validation, and tenant/owner isolation.

---

## 3. Brand and visual specification

The service follows the HD2Lab design system used by [AGPilot0002](https://github.com/HyangDan2/AGPilot0002) and [HD2Lab HQ](https://hd2lab.duckdns.org):

- **Soft Iris light / Navy & Lavender dark** tokens driven by the shared `hd2-theme` key, applied before first paint
- HD2Lab geometric skew mark and favicon set (`favicon.svg`, `favicon.ico`, `LOGO.png`)
- Header eyebrow `06 / SERVE · DOCUMENT INTELLIGENCE SERVICE`
- A branded status/dashboard landing page at `/`, and Swagger UI at `/docs` themed for the HD2Lab palette; both work behind the `/admin/agtools` reverse-proxy prefix

---

## 4. Running and testing

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 server.py          # http://127.0.0.1:8006
.venv/bin/pytest -v        # 12 test modules
```

- Landing page: http://127.0.0.1:8006/
- Swagger UI: http://127.0.0.1:8006/docs
- OpenAPI schema: http://127.0.0.1:8006/openapi.json

The test suite covers the API, auth and security, the parsers, hierarchy, chunking, rendering, resource limits, job recovery, cache integrity, search/timeouts and hybrid RAG search.

---

## 5. Authentication and access control

Every API call is checked against the service token and a tenant/owner scope.

| Header | Purpose |
| --- | --- |
| `Authorization: Bearer <token>` or `X-Service-Token: <token>` | Service authentication (`DOCUMENT_SERVICE_TOKEN`) |
| `X-Tenant-Id` | Tenant isolation (default `default_tenant`) |
| `X-Owner-Id` | Owner isolation (default `default_user`) |
| `X-Access-Scope` | `private`, `read_write` or `admin`; `admin` bypasses document-level checks |

Failure behaviour: a missing or wrong token returns `401 Unauthorized`; a document from another tenant returns `404 Not Found` (so its existence is not disclosed); another owner's private document inside the same tenant returns `403 Forbidden`.

---

## 6. API

Base path `/api/v1`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/documents` | Upload a document (100 MB streaming limit) and queue an asynchronous parse job (`202`) |
| `GET` | `/documents/{id}` | Document metadata and current status |
| `GET` | `/documents/{id}/status` | Parsing status: `queued`, `processing`, `completed`, `failed` |
| `GET` | `/documents/{id}/outline` | Hierarchical outline and section list (`limit`, `offset`, max 500 per page) |
| `GET` | `/documents/{id}/text` | Complete parsed document as Markdown |
| `GET` | `/documents/{id}/sections/{section_id}` | Section body, connected figure ids and table counts |
| `GET` | `/documents/{id}/chunks/{chunk_id}` | Single chunk body, token count and source location |
| `GET` | `/documents/{id}/figures/{figure_id}` | Figure metadata and caption |
| `GET` | `/documents/{id}/figures/{figure_id}/asset` | Original image or page/slide render (binary stream) |
| `GET` | `/documents/{id}/assets/{asset_id}` | Direct named-asset access |
| `GET` | `/documents/{id}/manifest` | Asset catalogue and document manifest with checksums |
| `POST` | `/documents/{id}/search` | Hybrid keyword + vector search with snippets and source grounding (`query`, `limit` 1–50) |

Request/response schemas, error codes and the AGPilot0005 mapping are in [docs/API_CONTRACT.md](docs/API_CONTRACT.md); the generated spec is [docs/openapi.json](docs/openapi.json).

### AGCore mapping

AGPilot0005 exposes these calls as the `document.*` tools: `get_outline`, `read_section`, `search`, `read_chunk` and `get_figure_info`. The adapter contract is documented in AGPilot0005's `docs/TOOL_ADAPTER_CONTRACT_AGPILOT0006.md`.

---

## 7. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ENV` | `development` | `development`, `test` or `production` |
| `HD2_DATA_DIR` | `./data` | Root of `documents/` and `metadata.sqlite3` |
| `DOCUMENT_SERVICE_TOKEN` | — | Service token (set this in production) |
| `AUTH_REQUIRED` | `true` | Enforce token authentication |
| `BYPASS_AUTH_IN_DEV` | `false` | Skip auth in development only |
| `PARSING_TIMEOUT_SECONDS`, `MAX_CONCURRENT_JOBS` | `300`, `4` | Parse budget and concurrency |
| `LIBREOFFICE_PATH` | — | Optional LibreOffice binary for render fallback |

Chunking defaults (`CHUNK_TARGET_TOKENS` 1,000, `CHUNK_MAX_TOKENS` 1,500, `CHUNK_OVERLAP_TOKENS` 100) and the file/page/ZIP limits live in `app/config.py`.

---

## 8. Documentation

- [Architecture (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md) — parser engines, AST hierarchy, chunking, search and system design
- [API contract (docs/API_CONTRACT.md)](docs/API_CONTRACT.md) — endpoint schemas, error codes and AGCore mapping
- [Operations (docs/OPERATIONS.md)](docs/OPERATIONS.md) — systemd, Nginx, environment variables and backup
- [OpenAPI schema (docs/openapi.json)](docs/openapi.json)
- [Work rules (WORK_RULES.md)](WORK_RULES.md) — development principles, security and API constraints
- [Third-party licence audit (THIRD_PARTY_LICENSES.md)](THIRD_PARTY_LICENSES.md) — PyMuPDF (AGPL v3) and other licences

`hd2lab.manifest.json` registers the service with the Server-HQ admin hub at `/admin/agtools/`.
