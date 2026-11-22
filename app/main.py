import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html

from app.config import settings
from app.core.database import get_db_connection, init_db
from app.core.job_manager import JobManager
from app.api.v1.router import api_router
from app.services import semantic_index

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize directory layout and database
    settings.init_directories()
    init_db()
    # Recover any orphaned or interrupted jobs from previous restart (Work Order A-4)
    JobManager.recover_on_startup()
    with get_db_connection() as conn:
        semantic_index.init_schema(conn)
    worker = asyncio.create_task(semantic_index.run_worker()) if semantic_index.configured() else None
    try:
        yield
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

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

@app.get("/embedding-settings.js", include_in_schema=False)
def embedding_settings_script():
    return FileResponse(BASE_PATH / "app/static/embedding-settings.js", media_type="text/javascript")


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

app.mount("/admin-assets", StaticFiles(directory=BASE_PATH / "app/static"), name="admin_assets")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    return FileResponse(BASE_PATH / "app/static/indexing-console.html", media_type="text/html")
