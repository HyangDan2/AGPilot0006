# AGPilot0006 — 작업 규칙 (Operational Rules & Invariants)

본 문서는 **AGPilot0006 (HD2Lab Document Intelligence Service)**의 개발 원칙, 아키텍처 제약사항, 보안 지침 및 HD2Lab 브랜딩 규칙을 규정합니다.

---

## 1. 핵심 개발 원칙 (Core Principles)

1. **독립 서비스 원칙 (Decoupled & Standalone)**
   - AGPilot0006는 AGCore, LangGraph, 또는 특정 LLM Agent 프레임워크에 직접 종속되지 않는 독립 REST API 서비스입니다.
   - 문서 파싱, 구조화, 에셋 추출, 청킹, 검색, 상태 관리는 AGPilot0006이 전담합니다.
   - 대화 세션 관리, Agent 의사결정 및 최종 사용자 응답 생성은 AGPilot0005가 담당합니다.

2. **LLM-Free Baseline 보장**
   - 핵심 기능(업로드, 파싱, 계층 추출, 슬라이드/페이지 렌더링, 청킹, FTS 검색)은 외부 LLM API 연결 없이도 100% 독립 동작해야 합니다.
   - 시각 정보 해석(Vision AI) 등 추가 기능은 별도 `VisionAdapter` 인터페이스와 기능 플래그(`FEATURE_VISION_ANALYSIS`)로 분리하며, 분석 미수행 시 임의 추론 결과를 생성하지 않습니다.

3. **구조 및 시각 정보 무결성 보존 (Information Integrity)**
   - 단순 평문 텍스트나 밋밋한 Markdown으로 축약 변환하지 않습니다.
   - **계층 구조 보존**: Chapter → Section → Subsection → Paragraph / Table / Figure
   - **시각 정보 이중 보존**:
     1. 원본 개별 에셋 (Embedded Image / Vector Chart)
     2. 페이지 / 슬라이드 전체 고해상도 렌더링 (특히 PPTX는 도형·배치 자체가 의미이므로 전체 렌더링 필수)
   - **근거 연결 (Grounding)**: 본문 단락과 Figure/Table의 상호 ID 링크 및 원본 페이지/슬라이드 인덱스를 명확히 보존합니다.

---

## 2. 보안 및 파일 격리 규정 (Security & Sandbox)

1. **비신뢰 입력 검증**:
   - 파일 확장자뿐만 아니라 매직 바이트(Magic Bytes) 기반의 실제 MIME 타입을 엄격히 검증합니다.
   - 지원 파일 형식: PDF (`application/pdf`), DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`), PPTX (`application/vnd.openxmlformats-officedocument.presentationml.presentation`).
2. **리소스 및 DoS 보호**:
   - 최대 업로드 파일 크기 제한 (기본 100MB).
   - 최대 처리 페이지/슬라이드 수 제한 (기본 500 pages).
   - Zip Bomb(비정상 압축 해제 배율) 방어 및 임시 파일 디스크 용량 모니터링.
   - 비동기 작업별 타임아웃 강제 적용 (단일 파싱 실패가 서버 전체 중단으로 이어지지 않도록 프로세스/작업 격리).
3. **경로 탐색 및 인젝션 차단**:
   - 파일 저장 및 조회 시 사용자 제공 파일명을 직접 시스템 경로에 사용하지 않으며, 안전한 `document_id` (UUIDv4) 및 해시 기반으로 저장소를 격리합니다 (`Path Traversal` 방지).
   - Office 매크로 실행 및 외부 URL 리소스 자동 다운로드를 원천 차단합니다.

---

## 3. HD2Lab 브랜드 및 UI/테마 규칙 (Brand Identity)

`AGPilot0002`의 HD2Lab 표준 비주얼 아이덴티티를 1:1로 준수합니다.

1. **테마 컬러 팔레트**:
   - **Dark Theme (기본)**:
     - Background: `#101110`
     - Foreground Text: `#f2f1eb`
     - Muted Text: `#a2a59c`
     - Lines & Borders: `#30322d`, `#45473f`
     - Accent Primary: `#ff824f` (Hover: `#ff9b73`)
     - Card Background: `#1b1d19`, `#22251f` (Border: `#33362d`)
     - Olive Accent: `#82916c`, `#b9c3a7`
   - **Light Theme**:
     - Background: `#f4f5f0`
     - Foreground Text: `#191b15`
     - Accent: `#e5622d`
2. **브랜드 마크 & 로고**:
   - HD2Lab Skew Mark (`border: 3px solid var(--accent); transform: skewY(-23deg);`)
   - Favicon: `favicon.svg`, `favicon.ico`, `favicon-32x32.png`, `apple-touch-icon.png` (AGPilot0002 표준 에셋 탑재)
   - Header Eyebrow: `06 / SERVE · DOCUMENT INTELLIGENCE SERVICE`
   - Brand Link: `https://hd2lab.duckdns.org` (Title: "HD2Lab 홈으로 이동")

---

## 4. API 인터페이스 및 데이터 계약 규정 (API Contract)

AGPilot0005와의 연동을 위해 다음 REST API 규격을 준수합니다:

- `POST /api/v1/documents`: 문서 업로드 및 비동기 파싱 Job 생성
- `GET  /api/v1/documents/{id}`: 문서 메타데이터 조회
- `GET  /api/v1/documents/{id}/status`: 파싱 진행 상태 (queued, processing, completed, failed)
- `GET  /api/v1/documents/{id}/outline`: 계층형 목차 및 섹션 목록
- `GET  /api/v1/documents/{id}/sections/{section_id}`: 단일 섹션 상세 본문 및 관련 Figure ID
- `GET  /api/v1/documents/{id}/figures/{figure_id}`: 이미지/도표 메타데이터 및 캡션 정보
- `GET  /api/v1/documents/{id}/figures/{figure_id}/asset`: 실제 원본 이미지/슬라이드 바이너리 스트리밍
- `POST /api/v1/documents/{id}/search`: 문서 내 FTS 키워드 기반 정밀 검색

**응답 최적화 원칙**:
- 원본 대용량 바이너리나 수만 줄의 전체 본문은 기본 조회 응답에 포함하지 않고, 필요 시 전용 엔드포인트나 페이지네이션을 통해 제공합니다.

---

## 5. 단계별 개발 및 검증 게이트 (Phase Gates)

- **Phase 1**: 파서 기반 (FastAPI 서비스 뼈대, 안전한 업로드 및 파일 검증, 기본 파서 모듈)
- **Phase 2**: 계층 구조 엔진 (Section ID 부여, Document Tree 구성, 문맥 보존 Chunking)
- **Phase 3**: 시각 에셋 엔진 (원본 이미지 추출, 슬라이드/페이지 렌더링, Figure 메타데이터 매핑)
- **Phase 4**: REST Query API & FTS 검색 (Outline, Section, Figure, Asset 서빙, OpenAPI 스키마)
- **Phase 5**: 통합 QA 및 AGPilot0005 어댑터 (Mock Adapter, 종단간 통합 테스트, 성능 및 안정성 검증)
