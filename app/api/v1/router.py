from fastapi import APIRouter
from app.api.v1.documents import router as documents_router

api_router = APIRouter()
api_router.include_router(documents_router)
