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
  <meta name="theme-color" content="#FAFAFD">
  <title>HD2Lab | Document Intelligence</title>
  <link rel="icon" href="{favicon_href}" type="image/svg+xml">
  <!-- Theme bootstrap: shared hd2-theme (Navy & Lavender dark / Soft Iris light) -->
  <script>
    (function () {{
      try {{
        var theme = localStorage.getItem('hd2-theme');
        if (theme !== 'dark' && theme !== 'light') theme = 'light';
        document.documentElement.setAttribute('data-theme', theme);
        document.documentElement.style.colorScheme = theme;
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute('content', theme === 'dark' ? '#0B1020' : '#FAFAFD');
      }} catch (e) {{ /* storage unavailable: the light default still applies */ }}
    }})();
  </script>
  <style>
    :root {{
      color-scheme: light;
      --bg: #FAFAFD;
      --text: #1D192B;
      --muted: #68627A;
      --line: #E8E4F2;
      --accent: #8B6CE8;
      --accent-hover: #7655D9;
      --accent-text: #6B4CD6;
      --accent-soft: #F1ECFE;
      --card-bg: #FFFFFF;
      --card-border: #E8E4F2;
      --on-accent: #FFFFFF;
    }}
    /* Navy & Lavender — the HD2Lab dark pair of Soft Iris Light. */
    [data-theme="dark"] {{
      color-scheme: dark;
      --bg: #0B1020;
      --panel: #12192E;
      --panel-soft: #1A2340;
      --text: #EEF2FF;
      --muted: #94A3C4;
      --line: #253154;
      --line-strong: #344472;
      --accent: #A78BFA;
      --accent-hover: #8B6CE8;
      --accent-text: #C4B5FD;
      --accent-soft: rgba(167, 139, 250, 0.14);
      --card-bg: #12192E;
      --card-border: #253154;
      --on-accent: #0B1020;
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
      color: var(--accent-text);
    }}
    p {{
      color: var(--muted);
      line-height: 1.6;
    }}
    .btn:hover {{
      background: var(--accent-hover);
    }}
    .btn {{
      display: inline-block;
      padding: 10px 18px;
      background: var(--accent);
      color: var(--on-accent);
      text-decoration: none;
      font-weight: 600;
      border-radius: 6px;
      margin-top: 16px;
    }}
    .endpoints {{
      margin-top: 20px;
      font-family: monospace;
      font-size: 13px;
      background: var(--accent-soft);
      padding: 14px;
      border-radius: 6px;
      border: 1px solid var(--line);
      overflow-wrap: anywhere;
    }}
    .endpoint {{
      margin-bottom: 6px;
    }}
    .method {{
      color: var(--accent-text);
      font-weight: bold;
    }}
    /* ── Floating Light/Dark toggle (fixed bottom-right, shared hd2-theme) ─── */
    .theme-fab {{
      position: fixed;
      right: max(20px, env(safe-area-inset-right, 0px));
      bottom: max(20px, env(safe-area-inset-bottom, 0px));
      z-index: 900;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 15px;
      border-radius: 999px;
      background: var(--card-bg);
      color: var(--text);
      border: 1px solid var(--line);
      box-shadow: 0 10px 26px rgba(11, 16, 32, 0.16);
      font-family: inherit;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      cursor: pointer;
      transition: border-color 0.18s ease, color 0.18s ease, transform 0.18s ease;
      -webkit-tap-highlight-color: transparent;
    }}
    [data-theme="dark"] .theme-fab {{
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.45);
    }}
    .theme-fab:hover {{
      border-color: var(--accent);
      color: var(--accent-text);
      transform: translateY(-1px);
    }}
    .theme-fab:active {{
      transform: translateY(0);
    }}
    .theme-fab:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 3px;
    }}
    .theme-fab-icon {{
      display: inline-block;
      font-size: 13px;
      line-height: 1;
    }}
    .theme-fab-label {{
      white-space: nowrap;
    }}
    @media (max-width: 767px) {{
      .theme-fab {{
        right: max(14px, env(safe-area-inset-right, 0px));
        bottom: max(14px, env(safe-area-inset-bottom, 0px));
        padding: 9px 13px;
      }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .theme-fab {{
        transition: none;
      }}
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

  <!-- Floating Light/Dark toggle (Navy & Lavender) -->
  <button id="themeFab" class="theme-fab" type="button" title="다크 모드로 전환" aria-label="다크 모드로 전환">
    <span class="theme-fab-icon" aria-hidden="true">🌙</span>
    <span class="theme-fab-label">다크</span>
  </button>
  <script>
    (function () {{
      var root = document.documentElement;
      var fab = document.getElementById('themeFab');
      function current() {{ return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'; }}
      function render(theme) {{
        if (!fab) return;
        var icon = fab.querySelector('.theme-fab-icon');
        var label = fab.querySelector('.theme-fab-label');
        var action = theme === 'dark' ? '라이트 모드로 전환' : '다크 모드로 전환';
        if (icon) icon.textContent = theme === 'dark' ? '☀️' : '🌙';
        if (label) label.textContent = theme === 'dark' ? '라이트' : '다크';
        fab.setAttribute('aria-label', action);
        fab.setAttribute('title', action);
        fab.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
      }}
      function apply(theme) {{
        root.setAttribute('data-theme', theme);
        root.style.colorScheme = theme;
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute('content', theme === 'dark' ? '#0B1020' : '#FAFAFD');
        try {{ localStorage.setItem('hd2-theme', theme); }} catch (e) {{}}
        render(theme);
      }}
      render(current());
      if (fab) fab.addEventListener('click', function () {{ apply(current() === 'dark' ? 'light' : 'dark'); }});
      window.addEventListener('storage', function (event) {{
        if (event.key !== 'hd2-theme') return;
        var next = event.newValue === 'dark' ? 'dark' : 'light';
        if (next !== current()) {{
          root.setAttribute('data-theme', next);
          root.style.colorScheme = next;
          render(next);
        }}
      }});
    }})();
  </script>
</body>
</html>
"""
