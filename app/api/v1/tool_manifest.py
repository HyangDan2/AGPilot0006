"""ADR-0017: Declarative tool manifest publication and global ON/OFF toggles for AGTools."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictBool

from app.api.v1.embedding_settings import require_embedding_admin
from app.config import settings
from app.core.auth import CallerContext, get_current_caller

router = APIRouter(tags=["tool manifest"])

MANIFEST_VERSION = "1.0.0"

DEFAULT_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "web.search",
        "category": "web",
        "kind": "http",
        "default_enabled": True,
        "is_read_only": True,
        "description": "웹(SearXNG 등)에서 최신·실시간 외부 정보, 동향, 사실 확인, 비교 자료를 검색합니다. 실시간·최신 정보가 필요한 질문에서는 도구가 없다고 답하기 전에 이 도구를 우선 호출하세요. (Search the live web for real-time/current information, recent developments, and external facts; call this before claiming lack of live data.)",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색할 키워드 또는 질의어 (1~200자)"},
                "limit": {"type": "integer", "description": "반환할 검색 결과 개수 (1~25, 미지정 시 컨텍스트 예산 비례 동적 할당)", "minimum": 1, "maximum": 25},
                "lang": {"type": "string", "description": "검색 언어 코드 (auto, ko, en 등)", "default": "auto"},
                "category": {"type": "string", "enum": ["general", "news"], "description": "검색 카테고리 (최신 뉴스/시황은 news 권장)", "default": "general"},
                "time_range": {"type": "string", "enum": ["all", "day", "week", "month"], "description": "검색 기간 필터 (오늘 뉴스는 day 권장)", "default": "all"},
                "max_snippet_chars": {"type": "integer", "description": "결과별 스니펫 최대 글자 수 (미지정 시 컨텍스트 예산 비례 동적 할당)", "minimum": 100, "maximum": 3000},
            },
            "required": ["query"],
        },
        "endpoint": {
            "method": "POST",
            "path": "/api/v1/web/search",
            "timeout_seconds": 15,
        },
    },
    {
        "name": "web.fetch",
        "category": "web",
        "kind": "http",
        "default_enabled": True,
        "is_read_only": True,
        "description": "특정 웹사이트 URL(예: https://hyangdan2.github.io)의 본문 텍스트를 직접 읽어와 분석합니다(동적 페이지 지원). (Fetch and extract readable text content from a specific web URL, including dynamic pages.)",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "조회할 웹페이지 URL 또는 도메인 (예: https://hyangdan2.github.io)"},
                "max_chars": {"type": "integer", "description": "추출할 최대 글자 수 (미지정 시 컨텍스트 예산 비례 동적 할당, 상한 100000)", "minimum": 500, "maximum": 100000},
            },
            "required": ["url"],
        },
        "endpoint": {
            "method": "POST",
            "path": "/api/v1/web/fetch",
            "timeout_seconds": 25,
        },
    },
    {
        "name": "document.get_outline",
        "category": "document",
        "kind": "builtin_bridge",
        "default_enabled": True,
        "is_read_only": True,
        "description": "업로드된 문서의 전체 목차 및 섹션 구조를 조회합니다.",
        "parameters": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
        "endpoint": {"method": "GET", "path": "/api/v1/documents/{doc_id}/outline", "timeout_seconds": 10},
    },
    {
        "name": "document.search",
        "category": "document",
        "kind": "builtin_bridge",
        "default_enabled": True,
        "is_read_only": True,
        "description": "문서 내부를 하이브리드(키워드+의미) 검색합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                "max_snippet_chars": {"type": "integer", "minimum": 100, "maximum": 4000},
            },
            "required": ["doc_id", "query"],
        },
        "endpoint": {"method": "POST", "path": "/api/v1/documents/{doc_id}/search", "timeout_seconds": 15},
    },
    {
        "name": "document.read_chunk",
        "category": "document",
        "kind": "builtin_bridge",
        "default_enabled": True,
        "is_read_only": True,
        "description": "특정 문서 청크 원문을 조회합니다.",
        "parameters": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}, "chunk_id": {"type": "string"}},
            "required": ["doc_id", "chunk_id"],
        },
        "endpoint": {"method": "GET", "path": "/api/v1/documents/{doc_id}/chunks/{chunk_id}", "timeout_seconds": 10},
    },
    {
        "name": "docx.to_markdown",
        "category": "document",
        "kind": "http",
        "default_enabled": True,
        "is_read_only": True,
        "description": "업로드된 Word(.docx) 문서를 표·제목 구조를 보존한 깔끔한 Markdown으로 변환합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "변환할 업로드된 DOCX 문서 ID"},
                "table_mode": {"type": "string", "enum": ["auto", "gfm", "html"], "default": "auto"},
                "max_chars": {"type": "integer", "description": "반환할 최대 글자 수 (미지정 시 컨텍스트 예산 비례 동적 할당)", "minimum": 500, "maximum": 100000},
            },
            "required": ["doc_id"],
        },
        "endpoint": {"method": "POST", "path": "/api/v1/tools/docx-to-md/by-doc", "timeout_seconds": 25},
    },
    {
        "name": "docx.from_markdown",
        "category": "document",
        "kind": "http",
        "default_enabled": True,
        "is_read_only": False,
        "description": "Markdown 텍스트(제목·표·목록·코드블록 포함)를 Word(.docx) 문서로 변환합니다. 변환 완료 후 응답에 포함된 markdown_link(또는 download_url)를 그대로 Markdown 링크로 사용자에게 반드시 제공하세요.",
        "parameters": {
            "type": "object",
            "properties": {
                "markdown": {"type": "string", "description": "DOCX로 변환할 Markdown 원문"},
                "filename": {"type": "string", "description": "생성할 파일명 (예: report.docx)", "default": "report.docx"},
                "title": {"type": "string", "description": "문서 상단 제목 (선택)"},
            },
            "required": ["markdown"],
        },
        "endpoint": {"method": "POST", "path": "/api/v1/tools/md-to-docx", "timeout_seconds": 20},
    },
]


def _state_path() -> Path:
    base = Path(getattr(settings, "DATA_DIR", "/var/www/services/AGTools/data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "tool_manifest_state.json"


def _load_overrides() -> Dict[str, bool]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("enabled"), dict):
            return {str(k): bool(v) for k, v in data["enabled"].items()}
    except Exception:
        pass
    return {}


def _save_overrides(overrides: Dict[str, bool]) -> None:
    path = _state_path()
    payload = {"schema": 1, "enabled": overrides}
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as tf:
        json.dump(payload, tf, ensure_ascii=False, indent=2)
        tf.flush()
        os.fsync(tf.fileno())
        tmp = Path(tf.name)
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def _catalog() -> List[Dict[str, Any]]:
    overrides = _load_overrides()
    items = []
    for item in DEFAULT_TOOLS:
        enabled = overrides.get(item["name"], bool(item.get("default_enabled", True)))
        items.append({**item, "enabled": enabled})
    return items


def build_manifest(include_disabled: bool = False) -> Dict[str, Any]:
    tools = _catalog()
    if not include_disabled:
        tools = [t for t in tools if t["enabled"]]
    return {
        "schema": 1,
        "provider": "agtools",
        "version": MANIFEST_VERSION,
        "tools": tools,
    }


class ToolToggleBody(BaseModel):
    enabled: StrictBool


@router.get("/tools/manifest")
def get_public_tool_manifest(caller: CallerContext = Depends(get_current_caller)):
    """Dynamic tool manifest consumed by AGCore."""
    return build_manifest(include_disabled=False)


@router.get("/settings/tools/manifest", dependencies=[Depends(require_embedding_admin)])
def get_admin_tool_manifest():
    """Admin console view including globally disabled tools."""
    return build_manifest(include_disabled=True)


@router.put("/settings/tools/manifest/{tool_name}", dependencies=[Depends(require_embedding_admin)])
def toggle_tool_manifest(tool_name: str, body: ToolToggleBody):
    known = {t["name"] for t in DEFAULT_TOOLS}
    if tool_name not in known:
        raise HTTPException(404, f"Unknown tool: {tool_name}")
    overrides = _load_overrides()
    overrides[tool_name] = bool(body.enabled)
    _save_overrides(overrides)
    return build_manifest(include_disabled=True)
