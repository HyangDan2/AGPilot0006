from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool

from app.api.v1.embedding_settings import require_embedding_admin
from app.core.auth import CallerContext, get_current_caller
from app.services import semantic_index, web_search
from app.services.document_search import DocumentSearchService

router = APIRouter(tags=["search engine"])


class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(5, ge=1, le=10)
    lang: str = Field("auto", max_length=16)
    category: Literal["general", "news"] = "general"
    time_range: Literal["all", "day", "week", "month"] = "all"
    max_snippet_chars: int = Field(800, ge=100, le=3000)
    bypass_cache: StrictBool = False
    token_budget: Optional[int] = Field(default=None, ge=64, le=131072)


class WebSearchSettingsBody(BaseModel):
    enabled: StrictBool
    public_enabled: StrictBool
    primary_provider: Literal["searxng", "duckduckgo", "wikipedia", "brave", "tavily"]
    fallback_provider: Literal["searxng", "duckduckgo", "wikipedia", "brave", "tavily", "none"]
    searxng_url: str = Field("", max_length=300)
    cache_ttl_seconds: int = Field(900, ge=0, le=86400)
    public_rpm: int = Field(15, ge=0, le=300)
    admin_rpm: int = Field(0, ge=0, le=600)
    timeout_seconds: float = Field(7.0, ge=1.0, le=20.0, allow_inf_nan=False)


@router.post("/web/search")
def run_web_search(
    body: WebSearchRequest,
    caller: CallerContext = Depends(get_current_caller),
):
    try:
        res = web_search.search(
            query=body.query,
            limit=body.limit,
            lang=body.lang,
            category=body.category,
            time_range=body.time_range,
            max_snippet_chars=body.max_snippet_chars,
            tenant_id=caller.tenant_id,
            owner_id=caller.owner_id,
            bypass_cache=body.bypass_cache,
        )
        if body.token_budget is not None and res.get("results"):
            char_budget = max(160, int(body.token_budget * 2.5))
            per_snippet = max(60, min(400, char_budget // max(1, len(res["results"]))))
            kept = []
            used = 0
            for item in res["results"]:
                trimmed_item = {**item, "snippet": (item.get("snippet") or "")[:per_snippet]}
                cost = len((trimmed_item["title"] + trimmed_item["url"] + trimmed_item["snippet"]).encode("utf-8")) + 48
                if kept and used + cost > char_budget:
                    break
                kept.append(trimmed_item)
                used += cost
            res = {**res, "results": kept, "total_results": len(kept), "token_budget_applied": body.token_budget}
        return res
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "rate_limited":
            raise HTTPException(429, "Web search rate limit exceeded")
        raise HTTPException(502, "Web search provider failed")


@router.get("/settings/search/overview", dependencies=[Depends(require_embedding_admin)])
def get_search_overview(log_limit: int = Query(15, ge=1, le=50)):
    return {
        "document_hybrid": DocumentSearchService.hybrid_status(log_limit=log_limit),
        "web_search": web_search.status(log_limit=log_limit),
        "embedding": semantic_index.status(),
    }


@router.post("/settings/search/fts-backfill", dependencies=[Depends(require_embedding_admin)])
def backfill_fts():
    result = DocumentSearchService.backfill_fts_indexes()
    return {"ok": True, **result}


@router.get("/settings/search/web", dependencies=[Depends(require_embedding_admin)])
def get_web_search_settings():
    return web_search.status()


@router.put("/settings/search/web", dependencies=[Depends(require_embedding_admin)])
def put_web_search_settings(body: WebSearchSettingsBody):
    try:
        web_search.save_settings(
            enabled=body.enabled,
            public_enabled=body.public_enabled,
            primary_provider=body.primary_provider,
            fallback_provider=body.fallback_provider,
            searxng_url=body.searxng_url,
            cache_ttl_seconds=body.cache_ttl_seconds,
            public_rpm=body.public_rpm,
            admin_rpm=body.admin_rpm,
            timeout_seconds=body.timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return web_search.status()


@router.post("/settings/search/web/test", dependencies=[Depends(require_embedding_admin)])
def test_web_search(body: WebSearchRequest):
    try:
        return web_search.search(
            query=body.query,
            limit=body.limit,
            lang=body.lang,
            category=body.category,
            time_range=body.time_range,
            max_snippet_chars=body.max_snippet_chars,
            tenant_id="default_tenant",
            owner_id="admin-console",
            bypass_cache=body.bypass_cache,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "rate_limited":
            raise HTTPException(429, "Web search rate limit exceeded")
        raise HTTPException(502, "Web search provider failed")


@router.delete("/settings/search/web/cache", dependencies=[Depends(require_embedding_admin)])
def purge_web_search_cache():
    removed = web_search.clear_cache()
    return {"ok": True, "removed": removed}
