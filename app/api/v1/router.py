from fastapi import APIRouter
from app.api.v1.documents import router as documents_router
from app.api.v1.docx_tools import router as docx_tools_router
from app.api.v1.embedding_settings import router as embedding_settings_router
from app.api.v1.search_engine import router as search_engine_router
from app.api.v1.tool_manifest import router as tool_manifest_router
from app.api.v1.web_fetch import router as web_fetch_router

api_router = APIRouter()
api_router.include_router(documents_router)
api_router.include_router(docx_tools_router)
api_router.include_router(embedding_settings_router)
api_router.include_router(search_engine_router)
api_router.include_router(tool_manifest_router)
api_router.include_router(web_fetch_router)
