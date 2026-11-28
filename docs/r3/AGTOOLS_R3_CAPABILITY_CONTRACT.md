# AGTools ↔ R3 Workflow Capability 계약 (AGPilot0006)

- 문서 ID: `r3-capability/v1` (구현: `app/r3_capabilities.py`)
- 대상 저장소: `HyangDan2/AGPilot0006` (HD²Lab Document Intelligence Service, v2.0.0)
- 브랜치: `feat/r3-workflow-extensibility`
- 짝 문서: AGCore R3 `AGPilot0005 app/workflow/registry.py`(WP-07 `UnifiedToolSpec`), `office_adapter.py`(WP-03 공통 Capability Interface), `AGPilot0010 docs/r3/GATEWAY_R3_CONTRACT.md`, `AGPilot0011 docs/r3/HD2AI_R3_WORKFLOW_CONTRACT.md`
- 기존 문서: `docs/API_CONTRACT.md`, `docs/ARCHITECTURE.md`, AGPilot0005 `docs/TOOL_ADAPTER_CONTRACT_AGPILOT0006.md`

---

## 1. 목적과 범위

R3 Workflow(Planner → Step 실행 → Human Approval)가 AGTools의 기존 R2.1 기능을 **실행 코드 수정 없이** 도구로 인식하고 호출할 수 있도록, 이미 존재하는 HTTP 표면을 WP-07 `UnifiedToolSpec` 어휘로 기술한 **읽기 전용 Capability Manifest**를 정의한다.

| 포함 | 제외 |
| --- | --- |
| `app/api/v1/*.py`에 선언된 공개 서비스 라우트 19개(+별칭 1개) | `/api/v1/settings/**` 관리자 콘솔 라우트 16개 |
| 각 라우트의 입력/출력 JSON Schema, 권한 등급, 부작용 등급, 타임아웃, 오류 계약 | 워크플로 상태 저장·스케줄링·승인 보관 (AGCore R3 소관) |
| R2.1 파서/라이터/RAG를 R3가 소비하는 공통 Capability Interface 매핑 | 파서·라이터·RAG 내부 구현 변경 (금지) |
| Mock/Runtime 어댑터 분리 계약 | 제품 UI (AGPilot0011 소관) |

**불변 조건 (강제됨, 테스트로 검증)**

1. `app/parsers/*`, `app/services/*`, `app/storage/*`, `app/core/*` 는 **일절 수정하지 않는다.** `app/r3_capabilities.py`는 표준 라이브러리(`ast`, `copy`, `dataclasses`, `pathlib`, `typing`)만 임포트하며, 라우트는 *임포트가 아니라 소스 데코레이터 파싱*으로 확인한다. (`tests/test_r3_capabilities.py::test_read_only_module_never_imports_r21_code`)
2. 매니페스트는 서비스 상태를 바꾸지 않는다. 문서 생성·파일 쓰기·DB 접근·네트워크 호출이 없고, 호출마다 깊은 복사본을 반환한다. (`test_list_returns_independent_copies`)
3. 매니페스트는 **라우트 진실(route-true)** 을 유지한다. 존재하지 않는 경로를 주장하면 테스트가 실패한다. (`test_manifest_matches_declared_routes_exactly`)

---

## 2. 두 매니페스트의 관계

AGTools는 이미 AGCore v1용 선언적 툴 매니페스트(`GET /api/v1/tools/manifest`, ADR-0017)를 제공한다. R3 매니페스트는 이를 **대체하지 않고 상위 어휘로 확장**한다.

| 항목 | AGCore v1 매니페스트 (`tool_manifest.py`) | R3 Capability Manifest (`r3_capabilities.py`) |
| --- | --- | --- |
| 목적 | LLM function calling용 도구 7종 + 전역 ON/OFF | Planner/Execution 엔진용 capability 서술 19종 |
| 스키마 | `{schema, provider, version, tools[]}` | `{schema, contract, provider, service, spec_fields, capabilities[], bindings[], …}` |
| 항목 단위 | `name, category, kind, default_enabled, is_read_only, description, parameters, endpoint` | WP-07 `UnifiedToolSpec` 13필드 (+ 별도 `bindings[]` 블록) |
| ON/OFF | 파일 상태(`tool_manifest_state.json`) 반영 | 없음 (정적·읽기 전용). 실행 가능 여부는 v1 매니페스트가 결정 |
| 역할 | **실행 게이트** | **계획 근거(무엇을 할 수 있는가)** |

두 매니페스트는 이름 공간을 공유한다. v1이 광고하는 7개 이름(`web.search`, `web.fetch`, `document.get_outline`, `document.search`, `document.read_chunk`, `docx.to_markdown`, `docx.from_markdown`)은 R3 매니페스트에 모두 존재해야 하며, 테스트가 이를 강제한다. (`test_agcore_v1_tool_manifest_names_are_a_subset`)

### 2.1 확인된 스키마 드리프트 (R3 매니페스트가 정본)

| 도구 | v1 매니페스트 광고 | 실제 라우트 수용 | R3 판정 |
| --- | --- | --- | --- |
| `document.search` | `top_k`(1–50), `max_snippet_chars`(100–4000) | `limit`(1–50), `token_budget`(64–262144) | 라우트 기준(`limit`/`token_budget`)으로 기술. v1 항목은 어댑터가 매핑하거나 후속 개정에서 교정 필요 |
| `web.search` | `limit` 1–25 | `WebSearchRequest.limit` 1–10 | 라우트 기준(1–10)으로 기술 |
| `docx.to_markdown` | `doc_id`, `table_mode`, `max_chars` | `+ image_mode`(assets/base64/none) | 라우트 기준으로 `image_mode` 추가 |
| `docx.from_markdown` | `markdown`, `filename`, `title` | `+ return_format`(json/docx) | 라우트 기준으로 `return_format` 추가 |
| `web.fetch` | timeout 25s | 제공자 10s + CSR 렌더 15s | R3는 상한 30s로 기술(재시도 여유 포함) |

---

## 3. Capability Descriptor 규격

WP-07 `UnifiedToolSpec`은 `AGPilot0005 app/workflow/registry.py`에 `model_config = ConfigDict(frozen=True, extra="forbid")`로 구현되어 있다. 즉 **키 하나만 달라도 등록이 거부된다.** 본 절은 그 구현에 맞춘 값 규칙이다.

### 3.1 필드 (13개, 그 외 키 금지)

| 필드 | 타입 | AGTools 값 규칙 |
| --- | --- | --- |
| `name` | string | `document.search`처럼 도구 이름. v1 매니페스트와 동일 이름을 쓰며, 이름 앞부분이 계열(`document`·`docx`·`web`·`tools`)을 나타낸다 |
| `namespace` | string | **전부 `agtools`** (아래 이유) |
| `version` | string | 도구 계약 SemVer. 현재 전부 `1.0.0` (`^\d+(\.\d+){0,3}([-+]\w+)?$`) |
| `description` | string | LLM function schema에 그대로 실리는 설명. 1~2문장 |
| `input_schema` | object | JSON Schema. `type`은 반드시 `object`. 인자명은 **서비스 파라미터명**(`doc_id`, `section_id`, …) |
| `output_schema` | object | 응답 JSON Schema. 바이너리는 `{"type":"string","format":"binary"}` |
| `capability` | string | capability 식별자(§4). **WP-07은 capability 하나당 라이브 구현 하나만 허용**한다 |
| `permission` | `read` \| `write` \| `admin` | §3.3 |
| `side_effect` | `none` \| `read` \| `write` \| `external` \| `destructive` | §3.4 |
| `timeout_seconds` | number | `0 < x ≤ 600` (WP-07 검증 범위) |
| `error_contract` | object | **문자열 id가 아니라 매핑**이다. §3.5의 계약 dict를 그대로 넣는다(식별자는 `id` 키) |
| `approval_required` | boolean | `true`면 Human Approval 없이 실행 금지 |
| `source` | string | 항상 `"agtools"` |

**`namespace`를 `agtools` 하나로 두는 이유**: WP-07의 `qualified_name`은 `f"{namespace}.{name}"`이다. 계열별 namespace(`agtools.document`)와 계열 접두 이름(`document.search`)을 함께 쓰면 `agtools.document.document.search`로 중복된다. 이름에 계열을 남기고 namespace를 `agtools`로 두면 `agtools.document.search` — v1 이름과 정확히 맞는 정규 이름이 된다. (계열 필터는 `name` 접두사 또는 `capability`로 한다.)

### 3.2 AGTools 바인딩 블록 (WP-07 외부)

WP-07 스펙에는 전송 정보가 없다. AGTools는 이를 끼워 넣지 않고 매니페스트의 **별도 `bindings[]` 블록**에 담는다. 두 블록은 `name`으로 1:1 대응한다.

```json
{
  "name": "document.search",
  "method": "POST",
  "path": "/api/v1/documents/{id}/search",
  "request_body": "json",
  "response_body": "json",
  "response_content_type": "application/json",
  "auth": "caller",
  "document_scoped": true,
  "aliases": [],
  "path_params": {"doc_id": "id"},
  "description": "Hybrid retrieval inside one parsed document …"
}
```

- `path_params`는 인자명 → 실제 경로 변수 매핑이다. Runtime 어댑터는 이것만 보고 치환하며, 인자 이름을 임의로 바꾸지 않는다.
- `aliases`는 같은 구현을 공유하는 별칭 라우트다(`/documents/{id}/assets/{asset_id}` → figure asset 스트림).
- `auth`는 전송 인증(`caller`), `document_scoped`는 문서별 테넌트/소유자 검사 여부다. WP-07 `permission`(읽기/쓰기 등급)과는 다른 축이므로 바인딩에 둔다.

### 3.3 권한 등급 (`permission`)

| 값 | 의미 | AGTools 적용 |
| --- | --- | --- |
| `read` | 서비스 상태를 읽기만 함 | 문서·청크·도면·매니페스트·변환 조회, 생성파일 다운로드 |
| `write` | 상태를 생성/변경하거나 외부로 나감 | `document.parse`, `docx.from_markdown`, `web.search`, `web.fetch` |
| `admin` | 관리자 콘솔 | 사용 안 함 (`/settings/` 표면은 매니페스트에서 제외) |

전송 인증은 모든 capability가 서비스 토큰(`Authorization: Bearer` 또는 `X-Service-Token`)을 요구하고, `document_scoped: true`인 도구는 추가로 문서 소유권을 검사한다(교차 테넌트 404, 타 소유자 private 403). `X-Access-Scope: admin`은 테넌트 검사를 우회하므로 R3 Planner가 스스로 요청해서는 안 되고, 호출 주체의 스코프를 그대로 전달해야 한다.

> **WP-07에 대한 발견·권고**: `side_effect="external"`은 WP-07 교차 검증에서 `permission != "read"`를 요구한다. 그래서 `web.search`/`web.fetch`(상태를 바꾸지 않는 조회)도 `permission="write"`로 표기해야 등록된다. 이대로면 Planner가 "검색에도 쓰기 권한이 필요하다"고 해석하고, 승인 게이트 정책을 잘못 걸 수 있다. `read + external` 조합을 허용하도록 WP-07 검증 규칙을 개정할 것을 권고한다(AGPilot0005 WP-07 후속 WO).

### 3.4 부작용 등급 (`side_effect`)

| 값 | 의미 | 재시도 | 병렬 실행 |
| --- | --- | --- | --- |
| `read` | AGTools 상태 읽기 | 자유 | 허용 |
| `write` | 문서·생성파일 저장 | `step_id + idempotency_key` 필요 | 같은 대상에는 금지 |
| `external` | 경계 밖(검색 제공자·원격 웹) 호출 | 지수 백오프 + 상한 | 레이트리밋 준수 |
| `none` | 요청 자체 외 상호작용 없음 | 자유 | 허용 |
| `destructive` | 삭제·비가역 덮어쓰기 | — | **AGTools는 제공하지 않음** |

`write`·`external`·`destructive`는 WP-07에서 `approval_required`와 별개로 `permission != "read"`를 요구한다(§3.3). `destructive`는 `approval_required=True`가 필수지만 AGTools에는 해당 도구가 없다.

### 3.5 오류 계약 (`error_contract`, 매핑)

| 계약 `id` | 형태 | 사용처 |
| --- | --- | --- |
| `agtools.http_error/v1` | `{"detail": "<사람용 문자열>"}` + HTTP 상태, `retryable`/`non_retryable` 목록 포함 | 동기 라우트 전부 |
| `agtools.parse_job/v1` | `http_error/v1` 상속 + `document.status`의 `status="failed"`, `error_message` | 비동기 파싱(`POST /documents` → 202) |

- 상태 코드: 400 입력 오류, 401 토큰 없음/불일치, 403 소유권 위반, 404 대상 없음(교차 테넌트 포함), 409 스냅샷 변경/파싱 미완료/커서 불일치, 413 용량 초과, 422 파싱 불가·파라미터 범위 위반, 429 레이트리밋, 502/503 상위 제공자 장애.
- 재시도 가능: `429`, `502`, `503`만. `403`·`404`·`409`·`422`는 재시도 금지(같은 결과가 반복된다).
- 오류 문자열은 사용자에게 그대로 노출하지 않는다. Planner는 상태 코드와 `capability`로 분기하고, 사용자 문구는 제품(HD2AI)이 만든다.

---

## 4. 공통 Capability Interface (R2.1 ↔ R3 Workflow)

AGCore R3 `OFFICE_CAPABILITIES`(`app/workflow/office_adapter.py`)의 5개 capability에 대한 AGTools 충족도다. **충족되지 않은 capability는 Mock 어댑터가 담당하며, Runtime 어댑터가 조용히 대체 구현을 만들지 않는다.**

| Capability | AGTools 상태 | 제공 도구 | 비고 |
| --- | --- | --- | --- |
| `workspace.rag.search` | ✅ provided | `document.search` | R2.1 하이브리드 검색(FTS5/BM25 + 벡터, RRF). **단일 문서 범위**이며 워크스페이스 횡단 검색은 미제공 |
| `office.docx.compare` | ❌ not_provided | — | 비교 기능 없음. Mock 필요 |
| `office.xlsx.analyze` | ❌ not_provided | — | 업로드 허용 형식이 PDF/DOCX/PPTX뿐 |
| `office.pptx.generate` | ❌ not_provided | — | PPTX는 파싱 전용(렌더는 래스터 이미지) |
| `workspace.file.write` | ❌ not_provided | — | `docx.from_markdown`은 AGTools 생성파일만 쓰고 관리형 워크스페이스에 쓰지 못함 |

AGTools가 R3에 추가로 기여하는 capability(§6): `document.ingest.parse`, `document.metadata.read`, `document.job.status`, `document.structure.outline`, `document.structure.section`, `document.text.read`, `document.chunk.list`, `document.chunk.read`, `document.figure.read`, `document.asset.read`, `document.manifest.read`, `document.convert.docx_to_markdown`, `document.convert.docx_to_markdown_inline`, `document.convert.markdown_to_docx`, `document.generated_file.read`, `web.search`, `web.fetch`, `registry.tool_manifest.read`.

> **capability 1:1 규칙**: WP-07은 capability 하나에 라이브 구현 하나만 허용한다(`DuplicateCapabilityError`). 그래서 "이미 저장된 문서를 변환"(`docx.to_markdown`)과 "업로드 파일을 즉시 변환"(`docx.to_markdown_upload`)은 **다른 capability**(`document.convert.docx_to_markdown` / `document.convert.docx_to_markdown_inline`)로 분리했다. 후자는 문서 id가 없어 결과의 청크·도면을 나중에 참조할 수 없으므로, 계획 단계에서 같은 capability로 취급하면 안 된다.

---

## 5. Mock / Runtime 분리 계약

### 5.1 공통 인터페이스

```
OfficeCapabilityAdapter (ABC)                 # AGPilot0005 app/workflow/office_adapter.py
  ├── MockOfficeAdapter      : R3 선행개발·Acceptance Scenario A. 결정론적, 외부 호출 0
  └── RuntimeOfficeAdapter   : AGTools HTTP 호출. 본 문서의 매니페스트만 근거로 동작
```

두 구현은 같은 입력/출력 형태(`OfficeResult`)를 반환하고, 차이는 **실패의 성격**뿐이다.

### 5.2 선택 규칙

| 상황 | 선택 |
| --- | --- |
| AGTools base URL 미설정 / R3 단위테스트 | Mock |
| `capability_coverage()`에서 `not_provided` | Mock (Runtime 호출 금지) |
| AGTools 응답 5xx·타임아웃이 **계획 단계**에서 1회 발생 | 해당 step을 Mock으로 대체하지 않고 `degraded`로 표시 후 사용자/운영에 보고 |
| 실행 단계 실패 | 재시도 정책 적용 후 step 실패로 종결. 조용한 Mock 폴백 금지 |

**금지**: Runtime 어댑터가 결과가 비었을 때 Mock 결과를 섞어 반환하는 것. "계약을 지키는 척하는" 가장 흔한 실패 모드이며, R3 안전 불변식(외부 텍스트 untrusted 태깅, 예산 계측)을 무력화한다.

### 5.3 Runtime 어댑터 매핑 규칙

1. `bindings[]`의 `method`/`path`를 그대로 사용하고, `path_params`로 경로 변수를 채운다. 그 외 인자는 `input_schema`에 정의된 위치(본문/쿼리)로 보낸다.
2. `timeout_seconds`를 클라이언트 타임아웃으로 강제한다(서버 상한보다 짧게 잡지 않는다).
3. `side_effect == "write"`는 `approval_required`가 `true`일 때 Human Approval 토큰(`ApprovalToken`, `approved_by_user=True`, `approval_source="user"`, `scope_hash` 일치) 없이는 호출하지 않는다.
4. `external`은 호출 전에 예산(WP-08)에서 1회분을 예약하고, 응답 크기를 `max_chars`/`limit`로 제한한다.
5. 오류는 §3.5 계약으로만 해석하고, 원문 문자열을 프롬프트에 넣지 않는다(외부 텍스트는 untrusted 데이터로 태깅).
6. 대용량 응답(`document.get_text`, `document.get_manifest`, asset 바이너리)은 도구 결과로 직접 넣지 않고 요약·청크 참조로 변환한 뒤 예산에 반영한다.

### 5.4 승인 게이트가 필요한 도구

| 도구 | 이유 |
| --- | --- |
| `docx.from_markdown` | 24시간 보관되는 파일을 생성(사용자에게 링크가 노출됨) — 유일한 `approval_required: true` |
| `document.parse` | 승인 불필요: 사용자가 직접 올린 문서의 수집이며 테넌트 내부에만 남는다 |

`document.parse`가 `write` 등급인데도 승인을 요구하지 않는 것은 의도적이다. 승인 게이트는 **되돌릴 수 없거나 외부로 나가는 결과**에만 건다. 반대로 파일 생성은 사용자가 받게 되는 산출물이므로 게이트를 건다.

---

## 6. Capability 인벤토리 (19종)

| name (정규 이름 `agtools.*`) | capability | method · path | permission | side_effect | approval | timeout |
| --- | --- | --- | --- | --- | --- | --- |
| `document.parse` | `document.ingest.parse` | POST `/api/v1/documents` | write | write | – | 120s |
| `document.get` | `document.metadata.read` | GET `/api/v1/documents/{id}` | read | read | – | 10s |
| `document.status` | `document.job.status` | GET `/api/v1/documents/{id}/status` | read | read | – | 10s |
| `document.get_outline` | `document.structure.outline` | GET `/api/v1/documents/{id}/outline` | read | read | – | 10s |
| `document.get_text` | `document.text.read` | GET `/api/v1/documents/{id}/text` | read | read | – | 20s |
| `document.read_section` | `document.structure.section` | GET `/api/v1/documents/{id}/sections/{section_id}` | read | read | – | 10s |
| `document.list_chunks` | `document.chunk.list` | GET `/api/v1/documents/{id}/chunks` | read | read | – | 15s |
| `document.read_chunk` | `document.chunk.read` | GET `/api/v1/documents/{id}/chunks/{chunk_id}` | read | read | – | 10s |
| `document.get_figure` | `document.figure.read` | GET `/api/v1/documents/{id}/figures/{figure_id}` | read | read | – | 10s |
| `document.get_figure_asset` | `document.asset.read` | GET `/api/v1/documents/{id}/figures/{figure_id}/asset` (+ alias `/assets/{asset_id}`) | read | read | – | 30s |
| `document.get_manifest` | `document.manifest.read` | GET `/api/v1/documents/{id}/manifest` | read | read | – | 15s |
| `document.search` | `workspace.rag.search` | POST `/api/v1/documents/{id}/search` | read | read | – | 30s |
| `docx.to_markdown` | `document.convert.docx_to_markdown` | POST `/api/v1/tools/docx-to-md/by-doc` | read | read | – | 25s |
| `docx.to_markdown_upload` | `document.convert.docx_to_markdown_inline` | POST `/api/v1/tools/docx-to-md` | read | read | – | 60s |
| `docx.from_markdown` | `document.convert.markdown_to_docx` | POST `/api/v1/tools/md-to-docx` | write | write | **필요** | 30s |
| `docx.download` | `document.generated_file.read` | GET `/api/v1/tools/md-to-docx/{file_id}` | read | read | – | 15s |
| `web.search` | `web.search` | POST `/api/v1/web/search` | write | external | – | 20s |
| `web.fetch` | `web.fetch` | POST `/api/v1/web/fetch` | write | external | – | 30s |
| `tools.manifest` | `registry.tool_manifest.read` | GET `/api/v1/tools/manifest` | read | read | – | 10s |

**제외된 표면**: `/api/v1/settings/**` 16개 라우트(임베딩 설정, 검색 제공자 설정, v1 툴 전역 스위치, 관리자용 변환 다운로드). 관리자 SSO에 묶여 있고 Planner가 계획할 대상이 아니므로 매니페스트에서 의도적으로 제외하며, 테스트가 "제외된 라우트는 전부 `/settings/`" 임을 검증한다.

---

## 7. AGTools가 하지 않는 일 (경계)

- 워크플로우 상태(plan, step, checkpoint)를 저장하지 않는다. AGTools는 **무상태 도구 제공자**다.
- Human Approval을 판정·보관하지 않는다. 승인 토큰 검증은 AGCore WP-10(`safety.ApprovalVerifier`) 소관이며, AGTools는 `approval_required` 표시만 제공한다.
- 재시도·백오프·예산 계산을 하지 않는다(클라이언트 예산은 `token_budget` 힌트로만 반영).
- 대화 세션, 사용자 응답 생성, 모델 호출을 하지 않는다(LLM-Free baseline).
- 파서/라이터/RAG 동작을 R3 요구에 맞춰 바꾸지 않는다. R3에 필요한 새 동작은 **새 라우트**로 추가하고 매니페스트를 갱신한다.

---

## 8. 검증과 드리프트 대응

### 8.1 이 저장소 (단위 테스트 20개)

```bash
cd worktrees/r3/AGPilot0006
/workspace/.venv-r2canvas/bin/python -m pytest tests/test_r3_capabilities.py -v
```

| 항목 | 테스트 |
| --- | --- |
| 13필드 규격 준수, 타입·어휘·타임아웃·버전 유효 | `test_every_capability_carries_exactly_the_wp07_field_set` |
| WP-07 교차 필드 정책(권한-부작용-승인) 재현 | `test_side_effect_and_approval_invariants` |
| capability 1:1 (중복 capability 금지) | `test_capability_ids_are_unique_per_tool` |
| JSON 직렬화·재호출 안정성 | `test_manifest_is_json_serializable_and_stable` |
| 반환값 독립성(읽기 전용) | `test_list_returns_independent_copies` |
| R2.1 코드 미임포트(표준 라이브러리만) | `test_read_only_module_never_imports_r21_code` |
| 라우트 진실(누락·미기술·중복 0) | `test_manifest_matches_declared_routes_exactly` |
| 드리프트 탐지기 자체 검증 | `test_route_coverage_detects_drift` |
| 관리자 표면 제외 규칙 | `test_admin_surfaces_are_excluded_on_purpose` |
| 공통 Capability Interface 정합 | `test_shared_capability_interface_is_consistent` |
| v1 매니페스트 이름 포섭 | `test_agcore_v1_tool_manifest_names_are_a_subset` |

### 8.2 교차 저장소 검증 (WP-07 실제 구현 대조)

`AGPilot0005 app/workflow/registry.py`의 실제 `UnifiedToolSpec`(`extra="forbid"`)으로 19개 항목을 생성하고 `UnifiedToolRegistry`에 등록해 충돌·중복 capability가 없음을 확인했다.

```
validated 19 AGTools specs against AGPilot0005 UnifiedToolSpec; registered without collision
qualified: ['agtools.document.get', 'agtools.document.get_figure', 'agtools.document.get_figure_asset',
            'agtools.document.get_manifest', 'agtools.document.get_outline', 'agtools.document.get_text',
            'agtools.document.list_chunks', 'agtools.document.parse', 'agtools.document.read_chunk',
            'agtools.document.read_section', 'agtools.document.search', 'agtools.document.status',
            'agtools.docx.download', 'agtools.docx.from_markdown', 'agtools.docx.to_markdown',
            'agtools.docx.to_markdown_upload', 'agtools.tools.manifest', 'agtools.web.fetch',
            'agtools.web.search']
approval gates: ['agtools.docx.from_markdown']
by capability workspace.rag.search: ['agtools.document.search']
```

(검증 시점의 AGPilot0005 `app/workflow/`는 같은 WO의 형제 작업에서 작성 중이었다. WP-07 필드·어휘가 바뀌면 이 절과 §3을 함께 갱신한다.)

### 8.3 드리프트 대응 규칙

`app/api/v1/*.py`의 라우트를 추가·변경·삭제하면 같은 커밋에서 `app/r3_capabilities.py`의 `_CATALOG`를 갱신한다. 갱신을 잊으면 `test_manifest_matches_declared_routes_exactly`가 실패하므로 CI에서 차단된다.

---

## 9. 미해결·권고 사항

1. **`external` + `read` 권한 조합 허용**(§3.3) — 현재 WP-07 규칙 때문에 읽기 전용 웹 검색이 `permission="write"`로 표기된다. Planner의 승인 정책이 과도하게 걸릴 수 있으므로 WP-07 검증 규칙 개정을 권고한다.
2. **v1 매니페스트 드리프트**(§2.1) — `document.search`의 `top_k`/`max_snippet_chars`, `web.search`의 `limit` 1–25는 실제 라우트와 불일치한다. 어댑터 매핑으로 흡수 중이나, 후속 WO에서 `DEFAULT_TOOLS`를 교정해야 한다.
3. **`workspace.rag.search` 범위** — AGTools 검색은 문서 1건 단위다. 워크스페이스 횡단 검색은 AGCore Workspace RAG가 담당하며, R3 Planner는 두 범위를 혼동하지 않아야 한다.
4. **`office.*` 4종 공백** — R3 Acceptance Scenario B(엑셀 분석·PPT 생성·문서 비교)는 Mock 없이는 통과할 수 없다. Runtime 제공 계획은 별도 WO가 필요하다.
5. **WP-07 `description`** — 스펙에 이미 존재하므로 AGTools는 이를 채워 넣는다(§3.1). `bindings[].description`과 같은 문자열을 유지해 두 경로에서 같은 설명이 보이게 한다.

---

## 10. 변경 이력

| 날짜 | 내용 |
| --- | --- |
| 2026-11-28 | 최초 작성. `r3-capability/v1`, 19개 capability, 라우트 진실 검증 포함 (WO-R3-PREDEV) |
| 2026-11-28 | WP-07 실제 구현(`AGPilot0005 app/workflow/registry.py`)에 맞춰 정렬: 13필드, `permission`(read/write/admin), `side_effect`(none/read/write/external/destructive), `error_contract` 매핑, namespace `agtools`, capability 1:1 분리. 교차 저장소 등록 검증 추가 |
