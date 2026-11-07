# HD2Lab Document Intelligence Service (AGPilot0006)

> **독립 문서 파싱, 구조화, 시각 정보 보존 및 REST Query API 서비스**  
> *PDF • DOCX • PPTX • 계층 구조 보존 • 전체 슬라이드 렌더링 • 원본 에셋 추출 • AGPilot0005 연동*

---

## 1. 개요 (Overview)

**HD2Lab Document Intelligence Service**는 비정형 문서(PDF, DOCX, PPTX)를 LLM 및 AI 에이전트(AGPilot0005)가 효율적으로 이해하고 Tool Calling을 통해 온디맨드로 조회할 수 있도록 파싱·구조화·보관·검색하는 독립 백엔드 서비스입니다.

문서 전체를 거대한 단일 텍스트나 조잡한 Markdown으로 단순 변환하지 않고, 다음 핵심 정보를 엄격히 보존합니다:
- **문서 계층 구조 (Document Tree)**: 장/절/단락/표/그림의 위계 관계
- **시각적 구성 보존**: 원본 삽입 이미지 파일 및 슬라이드/페이지 전체 고해상도 렌더링
- **근거 연결 (Grounding)**: 본문 단락과 시각 정보(Figure/Chart)의 유기적 매핑

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
- [상세 아키텍처 (ARCHITECTURE.md)](ARCHITECTURE.md): 시스템 컴포넌트, 파이프라인 및 저장소 구조 (작성 중)

---

## 4. API 엔드포인트 명세 (API Contract)

| 메서드 | 엔드포인트 | 설명 |
| :--- | :--- | :--- |
| `POST` | `/api/v1/documents` | 신규 문서 업로드 및 비동기 파싱 Job 등록 |
| `GET`  | `/api/v1/documents/{id}` | 문서 메타데이터 조회 |
| `GET`  | `/api/v1/documents/{id}/status` | 파싱 진행 상태 (queued, processing, completed, failed) |
| `GET`  | `/api/v1/documents/{id}/outline` | 계층형 목차 및 섹션 목록 |
| `GET`  | `/api/v1/documents/{id}/sections/{section_id}` | 특정 섹션 본문 및 관련 Figure ID |
| `GET`  | `/api/v1/documents/{id}/figures/{figure_id}` | 시각 객체 메타데이터 및 캡션 |
| `GET`  | `/api/v1/documents/{id}/figures/{figure_id}/asset` | 원본 이미지 / 슬라이드 렌더링 이미지 바이너리 |
| `POST` | `/api/v1/documents/{id}/search` | 문서 내 FTS 키워드 검색 |
