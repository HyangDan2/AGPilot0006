# HD2Lab Document Intelligence Service (AGPilot0006) — API Contract

> **버전**: v2.0.0  
> **기준 일자**: 2026-11-13  
> **프로토콜**: HTTP/1.1 REST (JSON & Binary Stream)  
> **기본 접근 경로**: `/api/v1` (Nginx 관리자 프록시: `/admin/agtools/api/v1`)  
> **연동 시스템**: AGCore (AGPilot0005), HD2Lab AI Gateway v3, 관리자 Web UI

---

## 1. 기본 규약 (General Protocol & Conventions)

- **기본 엔드포인트**: `http://127.0.0.1:8006/api/v1`
- **관리자 프록시 엔드포인트**: `https://hd2lab.duckdns.org/admin/agtools/api/v1`
- **문자셋 / 인코딩**: `UTF-8`
- **데이터 포맷**: 
  - 기본 API 응답: `application/json`
  - 에셋 스트리밍: `image/png`, `image/jpeg` 등 바이너리 스트림
  - 업로드 요청: `multipart/form-data`

---

## 2. 인증 및 접근 통제 헤더 (Authentication & Access Control)

모든 API 호출은 서비스 토큰 인증과 테넌트/소유자 분리 헤더를 거칩니다.

### 2.1 인증 헤더
- `Authorization: Bearer <SERVICE_TOKEN>` 또는 `X-Service-Token: <SERVICE_TOKEN>`
- 기본 토큰 환경변수: `DOCUMENT_SERVICE_TOKEN` (설정 기본값: `hd2-doc-secret-token-key-2026`)

### 2.2 테넌트 및 소유자 컨텍스트 헤더
| 헤더 | 기본값 | 설명 |
| :--- | :--- | :--- |
| `X-Tenant-Id` | `default_tenant` | 다중 테넌트 격리를 위한 조직/테넌트 식별자 |
| `X-Owner-Id` | `default_user` | 문서를 업로드한 사용자 식별자 |
| `X-Access-Scope` | `private` | 접근 권한 범위 (`private`: 본인 및 관리자만 조회, `read_write`: 테넌트 내 공유) |

### 2.3 접근 제어 실패 정책
- **토큰 누락 / 불일치**: `401 Unauthorized`
- **타 테넌트 문서 접근**: 문서의 존재 자체를 숨기기 위해 `404 Not Found` 반환
- **동일 테넌트 내 타인 소유 비공개(`private`) 문서 접근**: `403 Forbidden` 반환

---

## 3. API 엔드포인트 명세 매트릭스 (API Contract Matrix)

| 메서드 | 엔드포인트 | 태그 | 설명 |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/documents` | Documents | 비정형 문서(PDF, DOCX, PPTX) 업로드 및 비동기 파싱 Job 생성 |
| `GET`  | `/api/v1/documents/{id}` | Documents | 문서 메타데이터, 파서 정보 및 전체 현황 요약 조회 |
| `GET`  | `/api/v1/documents/{id}/status` | Documents | 문서 파싱 실시간 진행 상태 조회 (`queued`, `processing`, `completed`, `failed`) |
| `GET`  | `/api/v1/documents/{id}/outline` | Documents | 문서 계층 구조 목차 및 섹션 목록 (limit/offset 페이지네이션 지원) |
| `GET`  | `/api/v1/documents/{id}/sections/{section_id}` | Documents | 특정 섹션 본문 텍스트, 연결된 Figure ID, 표 개수 및 소스 위치 조회 |
| `GET`  | `/api/v1/documents/{id}/chunks/{chunk_id}` | Documents | 1,500토큰 이내의 정밀 단일 청크 상세 내용 및 토큰 수 조회 |
| `GET`  | `/api/v1/documents/{id}/figures/{figure_id}` | Documents | 시각 객체(이미지/도표/슬라이드) 메타데이터 및 캡션 정보 조회 |
| `GET`  | `/api/v1/documents/{id}/figures/{figure_id}/asset` | Documents | 원본 추출 이미지 또는 고해상도 슬라이드 렌더링 바이너리 스트리밍 |
| `GET`  | `/api/v1/documents/{id}/assets/{asset_id}` | Documents | 시각 에셋 직접 조회 엔드포인트 (Asset ID 별칭) |
| `GET`  | `/api/v1/documents/{id}/manifest` | Documents | 문서 전체 에셋 카탈로그 및 SHA-256 체크섬 매니페스트 조회 |
| `POST` | `/api/v1/documents/{id}/search` | Documents | SQLite FTS5 기반 키워드 전문 검색 및 문맥 스니펫 하이라이팅 |

---

## 4. 엔드포인트별 상세 규격

### 4.1 문서 업로드 및 파싱 Job 등록
- **엔드포인트**: `POST /api/v1/documents`
- **Content-Type**: `multipart/form-data`
- **제약 사항**: 최대 파일 크기 100MB 스트리밍 제한, 비정상 Zip Bomb 차단

#### Request (Form-Data)
- `file`: 바이너리 파일 (`.pdf`, `.docx`, `.pptx`)

#### Response (Status: `202 Accepted`)
```json
{
  "document_id": "doc-550e8400-e29b-41d4-a716-446655440000",
  "filename": "HD2Lab_Platform_Architecture_Guide.docx",
  "status": "queued",
  "message": "Document uploaded and parsing job enqueued."
}
```
> 동일 파일이 이미 파싱 완료되어 캐시에 존재하는 경우 즉시 `status: "completed"`와 함께 캐시 적중 메시지가 반환됩니다.

---

### 4.2 문서 상태 및 메타데이터 조회
- **엔드포인트**: `GET /api/v1/documents/{id}`
- **Response (Status: `200 OK`)**:
```json
{
  "document_id": "doc-550e8400-e29b-41d4-a716-446655440000",
  "filename": "HD2Lab_Platform_Architecture_Guide.docx",
  "file_hash": "a1b2c3d4e5f67890123456789abcdef0...",
  "doc_type": "docx",
  "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "file_size": 2458120,
  "status": "completed",
  "parser_version": "2.0.0",
  "page_count": 12,
  "section_count": 8,
  "figure_count": 4,
  "error_message": null,
  "owner_id": "default_user",
  "tenant_id": "default_tenant",
  "access_scope": "private",
  "created_at": "2026-11-13T01:00:00Z",
  "updated_at": "2026-11-13T01:00:03Z"
}
```

---

### 4.3 실시간 파싱 상태 확인
- **엔드포인트**: `GET /api/v1/documents/{id}/status`
- **Response (Status: `200 OK`)**:
```json
{
  "document_id": "doc-550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "error_message": null,
  "created_at": "2026-11-13T01:00:00Z",
  "updated_at": "2026-11-13T01:00:01Z"
}
```

---

### 4.4 계층형 목차 조회 (Outline)
- **엔드포인트**: `GET /api/v1/documents/{id}/outline?limit=100&offset=0`
- **Query Parameters**:
  - `limit`: 조회할 섹션 수 (기본: 100, 최소: 1, 최대: 500)
  - `offset`: 시작 오프셋 (기본: 0)
- **Response (Status: `200 OK`)**:
```json
{
  "document_id": "doc-550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "sections": [
    {
      "section_id": "sec-1",
      "parent_id": null,
      "title": "1. Overview & Vision",
      "level": 1,
      "source": {
        "type": "docx",
        "page": 1,
        "slide": null
      }
    },
    {
      "section_id": "sec-1-1",
      "parent_id": "sec-1",
      "title": "1.1 Architecture Pillars",
      "level": 2,
      "source": {
        "type": "docx",
        "page": 2,
        "slide": null
      }
    }
  ]
}
```

---

### 4.5 특정 섹션 본문 조회
- **엔드포인트**: `GET /api/v1/documents/{id}/sections/{section_id}`
- **Response (Status: `200 OK`)**:
```json
{
  "section_id": "sec-1",
  "parent_id": null,
  "title": "1. Overview & Vision",
  "level": 1,
  "breadcrumbs": ["1. Overview & Vision"],
  "content": "HD2Lab은 생성형 AI 서비스를 효율적으로 통합 관리하는 독립 플랫폼입니다...",
  "figure_ids": ["fig-1-1"],
  "table_count": 0,
  "source": {
    "type": "docx",
    "page": 1,
    "slide": null
  }
}
```

---

### 4.6 단일 청크 상세 조회
- **엔드포인트**: `GET /api/v1/documents/{id}/chunks/{chunk_id}`
- **설명**: 1,500토큰 이내로 보장된 단일 텍스트 청크 및 위치 근거 정보 조회
- **Response (Status: `200 OK`)**:
```json
{
  "chunk_id": "chk-1-1",
  "parent_section_id": "sec-1",
  "content": "HD2Lab은 다양한 생성형 AI 애플리케이션(HD2AI, Lingo 등)을 통합 관리합니다.",
  "token_count": 48,
  "figure_ids": ["fig-1-1"],
  "source": {
    "type": "docx",
    "page": 1,
    "slide": null
  }
}
```

---

### 4.7 시각 객체 메타데이터 조회
- **엔드포인트**: `GET /api/v1/documents/{id}/figures/{figure_id}`
- **Response (Status: `200 OK`)**:
```json
{
  "figure_id": "fig-1-1",
  "caption": "HD2Lab 통합 아키텍처 개요 다이어그램",
  "page_or_slide": 1,
  "asset_id": "asset-fig-1-1",
  "source_type": "embedded_image",
  "visual_description": null
}
```

---

### 4.8 시각 에셋 바이너리 스트리밍
- **엔드포인트**: `GET /api/v1/documents/{id}/figures/{figure_id}/asset` (또는 `/assets/{asset_id}`)
- **Response (Status: `200 OK`)**:
  - `Content-Type`: `image/png` (또는 `image/jpeg`)
  - Body: 이미지 바이너리 데이터

---

### 4.9 문서 매니페스트 조회
- **엔드포인트**: `GET /api/v1/documents/{id}/manifest`
- **Response (Status: `200 OK`)**:
```json
{
  "document_id": "doc-550e8400-e29b-41d4-a716-446655440000",
  "manifest_version": "1.0",
  "assets": [
    {
      "asset_id": "asset-fig-1-1",
      "type": "figure",
      "filename": "fig-1-1.png",
      "sha256": "8f3b...",
      "size_bytes": 104850
    }
  ]
}
```

---

### 4.10 FTS5 키워드 전문 검색
- **엔드포인트**: `POST /api/v1/documents/{id}/search`
- **Request Body**:
```json
{
  "query": "LangGraph Agent Runtime",
  "limit": 5
}
```
- **Response (Status: `200 OK`)**:
```json
{
  "document_id": "doc-550e8400-e29b-41d4-a716-446655440000",
  "query": "LangGraph Agent Runtime",
  "total_matches": 2,
  "results": [
    {
      "chunk_id": "chk-3-1",
      "section_id": "sec-3",
      "snippet": "AGCore(AGPilot0005)는 <mark>LangGraph</mark> 기반의 독립 <mark>Agent</mark> <mark>Runtime</mark> 플랫폼으로...",
      "score": -2.48,
      "token_count": 65,
      "figure_ids": ["fig-agent-loop"],
      "source": {
        "type": "docx",
        "page": 7,
        "slide": null
      }
    }
  ]
}
```

---

## 5. 업스트림 AGPilot0005 (AGCore) 연동 매핑

AGPilot0005의 어댑터(`AGToolsHttpAdapter`)는 아래 매핑에 따라 본 API를 호출합니다:

```text
[AGPilot0005 내장 도구]                           [AGPilot0006 REST 엔드포인트]
document.get_outline(doc_id)         ======>   GET  /api/v1/documents/{doc_id}/outline
document.read_section(doc_id, sec_id) ======>   GET  /api/v1/documents/{doc_id}/sections/{sec_id}
document.search(doc_id, query)       ======>   POST /api/v1/documents/{doc_id}/search
document.read_chunk(doc_id, chk_id)  ======>   GET  /api/v1/documents/{doc_id}/chunks/{chk_id}
document.get_figure_info(doc_id, fig)======>   GET  /api/v1/documents/{doc_id}/figures/{fig_id}
```

---

## 6. HTTP 상태 코드 및 오류 규격

| HTTP 상태 코드 | 상황 | 오류 응답 규격 |
| :--- | :--- | :--- |
| `200 OK` | 조회, 검색, 스트리밍 성공 | JSON 객체 또는 이미지 바이너리 |
| `202 Accepted` | 신규 문서 업로드 및 비동기 파싱 등록 성공 | `{"document_id": "...", "status": "queued"}` |
| `400 Bad Request` | 빈 파일 업로드, 잘못된 검색 인자, 파싱 불가 형식 | `{"detail": "Empty file uploaded."}` |
| `401 Unauthorized`| 서비스 토큰 누락 또는 유효하지 않은 토큰 | `{"detail": "Unauthorized: Invalid service token"}` |
| `403 Forbidden` | 타인 소유 비공개 문서 접근 | `{"detail": "Forbidden: Insufficient access rights"}` |
| `404 Not Found` | 존재하지 않는 문서/섹션/청크/에셋 또는 타 테넌트 문서 | `{"detail": "Document not found"}` |
| `413 Payload Too Large` | 업로드 파일 100MB 초과 | `{"detail": "File size exceeds 100MB limit"}` |
| `500 Server Error`| 파싱 내부 예외 또는 프로세스 장애 | `{"detail": "Internal processing failure"}` |
