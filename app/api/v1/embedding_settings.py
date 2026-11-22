from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool

from app.core.auth import get_current_caller
from app.services import semantic_index


def require_embedding_admin(
    _caller=Depends(get_current_caller),
    role: str = Header("", alias="X-HD2Lab-Role"),
    user: str = Header("", alias="X-HD2Lab-User"),
) -> None:
    if role != "admiral" or not user:
        raise HTTPException(403, "AGTools administrator access required")


router = APIRouter(
    prefix="/settings/embeddings",
    tags=["embedding settings"],
    dependencies=[Depends(require_embedding_admin)],
)


class EmbeddingSettings(BaseModel):
    enabled: StrictBool
    min_similarity: float = Field(ge=0, le=1, allow_inf_nan=False)


class EmbeddingStatus(EmbeddingSettings):
    configured: bool
    model: str | None
    model_revision: str | None
    indexed_documents: int
    pending_documents: int
    retrying_documents: int


@router.get("", response_model=EmbeddingStatus)
def get_embedding_settings():
    return semantic_index.status()


@router.put("", response_model=EmbeddingStatus)
def put_embedding_settings(body: EmbeddingSettings):
    if body.enabled and not semantic_index.configured():
        raise HTTPException(409, "Embedding model is not configured")
    semantic_index.save_settings(body.enabled, body.min_similarity)
    return semantic_index.status()


class QueueControl(BaseModel):
    paused: StrictBool


class JobAction(BaseModel):
    action: Literal["retry", "cancel"]


@router.get("/queue")
def get_queue(
    state: Literal["all", "running", "waiting", "issues", "done", "cancelled"] = "all",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return semantic_index.queue_status(state, limit, offset)


@router.put("/queue")
def update_queue(body: QueueControl):
    semantic_index.set_paused(body.paused)
    return semantic_index.queue_status("all", 30, 0)


@router.post("/queue/{document_id}")
def update_queue_job(document_id: str, body: JobAction):
    result = semantic_index.update_job(document_id, body.action)
    if result == "not_found":
        raise HTTPException(404, "Index job not found")
    if result == "conflict":
        raise HTTPException(409, "Index job cannot be changed in its current state")
    return {"ok": True}
