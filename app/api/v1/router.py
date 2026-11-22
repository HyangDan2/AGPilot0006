from fastapi import APIRouter
from app.api.v1.documents import router as documents_router
from app.api.v1.embedding_settings import router as embedding_settings_router
from app.api.v1.search_engine import router as search_engine_router

api_router = APIRouter()
api_router.include_router(documents_router)
api_router.include_router(embedding_settings_router)
api_router.include_router(search_engine_router)
