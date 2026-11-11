from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html

from app.config import settings
from app.core.database import init_db
from app.core.job_manager import JobManager
from app.api.v1.router import api_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize directory layout and database
    settings.init_directories()
    init_db()
    # Recover any orphaned or interrupted jobs from previous restart (Work Order A-4)
    JobManager.recover_on_startup()
    yield

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.PROJECT_VERSION,
    description="HD2Lab Document Intelligence Service (PDF, DOCX, PPTX Parsing & Query API)",
    lifespan=lifespan,
    docs_url=None,  # Custom styled Swagger UI below
    redoc_url=None
)

# Include API v1 router
app.include_router(api_router, prefix=settings.API_V1_STR)

# Serve brand assets
BASE_PATH = Path(__file__).resolve().parent.parent

@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    path = BASE_PATH / "favicon.ico"
    return FileResponse(path) if path.exists() else None

@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    path = BASE_PATH / "favicon.svg"
    return FileResponse(path, media_type="image/svg+xml") if path.exists() else None

@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request):
    """Custom styled Swagger documentation adhering to HD2Lab Dark Palette."""
    orig_uri = request.headers.get("x-original-uri", "")
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "")
    prefix = ""
    if "/admin/agtools" in orig_uri or forwarded_prefix.startswith("/admin/agtools") or request.url.path.startswith("/admin/agtools") or "agtools" in str(request.headers.get("referer", "")):
        prefix = "/admin/agtools"
    openapi_url = f"{prefix}/openapi.json" if prefix else "./openapi.json"
    swagger_favicon = f"{prefix}/favicon.svg" if prefix else "./favicon.svg"
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title=f"{settings.PROJECT_NAME} — API Docs",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_favicon_url=swagger_favicon,
    )

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    """HD2Lab Brand Status & Dashboard Landing Page."""
    orig_uri = request.headers.get("x-original-uri", "")
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "")
    prefix = ""
    if "/admin/agtools" in orig_uri or forwarded_prefix.startswith("/admin/agtools") or request.url.path.startswith("/admin/agtools") or "agtools" in str(request.headers.get("referer", "")):
        prefix = "/admin/agtools"
    favicon_href = f"{prefix}/favicon.svg" if prefix else "./favicon.svg"
    docs_href = f"{prefix}/docs" if prefix else "docs"
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HD2Lab | Document Intelligence</title>
  <link rel="icon" href="{favicon_href}" type="image/svg+xml">
  <style>
    :root {{
      --bg: #101110;
      --text: #f2f1eb;
      --muted: #a2a59c;
      --line: #30322d;
      --accent: #ff824f;
      --card-bg: #1b1d19;
      --card-border: #33362d;
    }}
    body {{
      margin: 0;
      padding: 40px 20px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      justify-content: center;
    }}
    .container {{
      max-width: 800px;
      width: 100%;
    }}
    .eyebrow {{
      font-size: 11px;
      color: var(--muted);
      letter-spacing: 1.5px;
      margin-bottom: 8px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      text-decoration: none;
      color: var(--text);
      margin-bottom: 24px;
    }}
    .mark {{
      display: inline-block;
      width: 16px;
      height: 20px;
      border: 3px solid var(--accent);
      transform: skewY(-23deg);
      position: relative;
    }}
    .mark:after {{
      content: '';
      position: absolute;
      inset: 4px -7px -6px 4px;
      border: 2px solid var(--accent);
    }}
    .brand-name {{
      font-size: 20px;
      font-weight: 800;
    }}
    .brand em {{
      font-style: normal;
      color: var(--muted);
      font-size: 14px;
      margin-left: 8px;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 24px;
      margin-top: 20px;
    }}
    h1 {{
      font-size: 24px;
      margin: 0 0 12px 0;
      color: var(--accent);
    }}
    p {{
      color: var(--muted);
      line-height: 1.6;
    }}
    .btn {{
      display: inline-block;
      padding: 10px 18px;
      background: var(--accent);
      color: #101110;
      text-decoration: none;
      font-weight: 600;
      border-radius: 6px;
      margin-top: 16px;
    }}
    .endpoints {{
      margin-top: 20px;
      font-family: monospace;
      font-size: 13px;
      background: #141613;
      padding: 14px;
      border-radius: 6px;
      border: 1px solid var(--line);
    }}
    .endpoint {{
      margin-bottom: 6px;
    }}
    .method {{
      color: var(--accent);
      font-weight: bold;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="eyebrow">06 / SERVE · DOCUMENT INTELLIGENCE SERVICE</div>
    <a href="https://hd2lab.duckdns.org" class="brand" target="_blank" rel="noopener noreferrer">
      <span class="mark"></span>
      <span class="brand-name">HD2Lab</span>
      <em>Document Intelligence Service</em>
    </a>
    
    <div class="card">
      <h1>Document Intelligence API is Active</h1>
      <p>PDF, DOCX, PPTX 문서를 구조화하고 시각적 요소(슬라이드 렌더링, 이미지 에셋)를 보존하여 AGPilot0005에 온디맨드 쿼리 인터페이스를 제공합니다.</p>
      
      <div class="endpoints">
        <div class="endpoint"><span class="method">POST</span> /api/v1/documents - 문서 비동기 업로드 및 파싱 Job 생성</div>
        <div class="endpoint"><span class="method">GET</span>  /api/v1/documents/{{id}}/status - 파싱 진행 상태 확인</div>
        <div class="endpoint"><span class="method">GET</span>  /api/v1/documents/{{id}}/outline - 계층 구조 및 목차 조회</div>
        <div class="endpoint"><span class="method">GET</span>  /api/v1/documents/{{id}}/sections/{{sec_id}} - 본문 및 연결 도표 조회</div>
        <div class="endpoint"><span class="method">GET</span>  /api/v1/documents/{{id}}/figures/{{fig_id}} - 도표 메타데이터 조회</div>
        <div class="endpoint"><span class="method">GET</span>  /api/v1/documents/{{id}}/figures/{{fig_id}}/asset - 이미지/슬라이드 바이너리</div>
        <div class="endpoint"><span class="method">POST</span> /api/v1/documents/{{id}}/search - FTS 키워드 검색</div>
      </div>

      <a href="{docs_href}" class="btn">Explore OpenAPI Docs →</a>
    </div>
  </div>
</body>
</html>
"""
