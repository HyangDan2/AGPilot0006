# HD2Lab Document Intelligence Service (AGPilot0006)

> **독립 문서 파싱, 계층 구조화, 시각 정보 보존 및 REST Query API 서비스**  
> *Version 2.0 — Phase 1.5 + Phase 2 완료*  
> *PDF • DOCX • PPTX • 계층 구조 보존 • 전체 슬라이드 렌더링 • 원본 에셋 추출 • AGPilot0005 연동*

---

## 1. 개요 (Overview)

**HD2Lab Document Intelligence Service**는 비정형 문서(PDF, DOCX, PPTX)를 LLM 및 AI 에이전트(AGPilot0005)가 효율적으로 이해하고 Tool Calling을 통해 온디맨드로 조회할 수 있도록 파싱·구조화·보관·검색하는 독립 백엔드 서비스입니다.

문서 전체를 거대한 단일 텍스트나 조잡한 Markdown으로 단순 변환하지 않고, 다음 핵심 정보를 엄격히 보존합니다:
- **문서 계층 구조 (Document Tree)**: 장/절/단락/표/그림의 위계 관계 및 읽기 순서
- **시각적 구성 보존**: 원본 삽입 이미지 파일 및 슬라이드/페이지 전체 고해상도 렌더링
- **근거 연결 (Grounding)**: 본문 단락과 시각 정보(Figure/Chart)의 유기적 매핑
- **토큰 기반 청킹 (Token-Aware Chunking)**: 한국어/영어 문맥을 보존하며 최대 1500 토큰 엄격 준수

---

## 2. HD2Lab 브랜드 및 비주얼 규격

본 프로젝트는 [AGPilot0002](https://github.com/HyangDan2/AGPilot0002) 및 [HD2Lab HQ](https://hd2lab.duckdns.org)의 시각 디자인 및 브랜드 규격을 준수합니다.
- **Brand Colors**:
  - Dark BG: `#101110`, Light BG: `#f4f5f0`
  - Accent Primary: `#ff824f`
  - Muted: `#a2a59c`, Lines: `#30322d`
- **Brand Mark**: HD2Lab Geometric Skew Mark & Favicon 세트 내장 (`favicon.svg`, `favicon.ico`, `LOGO.png` 등)
- **Header Eyebrow**: `06 / SERVE · DOCUMENT INTELLIGENCE SERVICE`

---

## 3. 핵심 규칙 및 문서

- [작업 규칙 (WORK_RULES.md)](WORK_RULES.md): 프로젝트 개발 원칙, 보안 지침, 브랜드 규칙, API 제약사항
- [라이선스 감사 (THIRD_PARTY_LICENSES.md)](THIRD_PARTY_LICENSES.md): PyMuPDF(AGPL v3) 및 서드파티 라이선스 점검 보고서

---

## 4. 인증 및 접근 통제 (Authentication & Access Control)

모든 API 호출은 서비스 토큰 인증과 Tenant/Owner 접근 제어를 거칩니다.

- **인증 헤더**:
  - `Authorization: Bearer <SERVICE_TOKEN>` 또는 `X-Service-Token: <SERVICE_TOKEN>`
  - `X-Tenant-Id`: 테넌트 분리 (기본: `default_tenant`)
  - `X-Owner-Id`: 소유자 분리 (기본: `default_user`)
  - `X-Access-Scope`: 접근 범위 (`private`, `read_write`, `admin`)
- **실패 응답**:
  - 토큰 누락 또는 불일치 시 `401 Unauthorized`
  - 타 테넌트 문서 접근 시 `404 Not Found` (문서 존재 유출 방지)
  - 동일 테넌트 내 타인 소유 비공개 문서 접근 시 `403 Forbidden`

---

## 5. API 엔드포인트 명세 (API Contract)

| 메서드 | 엔드포인트 | 설명 |
| :--- | :--- | :--- |
| `POST` | `/api/v1/documents` | 신규 문서 업로드(100MB 스트리밍 제한) 및 비동기 파싱 Job 등록 |
| `GET`  | `/api/v1/documents/{id}` | 문서 메타데이터 조회 |
| `GET`  | `/api/v1/documents/{id}/status` | 파싱 진행 상태 (queued, processing, completed, failed) |
| `GET`  | `/api/v1/documents/{id}/outline` | 계층형 목차 및 섹션 목록 (limit, offset 페이지네이션 지원) |
| `GET`  | `/api/v1/documents/{id}/sections/{section_id}` | 특정 섹션 본문 및 관련 Figure ID |
| `GET`  | `/api/v1/documents/{id}/chunks/{chunk_id}` | 토큰 한도 준수 단일 청크 상세 조회 |
| `GET`  | `/api/v1/documents/{id}/figures/{figure_id}` | 시각 객체 메타데이터 및 캡션 |
| `GET`  | `/api/v1/documents/{id}/figures/{figure_id}/asset` | 원본 이미지 / 슬라이드 렌더링 이미지 바이너리 스트리밍 |
| `GET`  | `/api/v1/documents/{id}/assets/{asset_id}` | 시각 에셋 직접 조회 엔드포인트 |
| `GET`  | `/api/v1/documents/{id}/manifest` | 에셋 카탈로그 및 문서 전체 Manifest 조회 |
| `POST` | `/api/v1/documents/{id}/search` | SQLite FTS5 기반 키워드 검색 및 스니펫 하이라이팅 |

---

## 6. 실행 및 테스트

### 개발 서버 실행
```bash
python server.py
# 서비스가 http://127.0.0.1:8006 에서 실행됩니다.
```

### 테스트 실행 (QA-01 ~ QA-16 전체 32종 테스트)
```bash
.venv/bin/pytest -v
```
