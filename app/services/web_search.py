import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.database import get_db_connection

VALID_PROVIDERS = ("searxng", "duckduckgo", "wikipedia", "brave", "tavily", "none")
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; HD2Lab-AGTools/2.2; +https://hd2lab.duckdns.org)"

_rate_lock = threading.Lock()
_rate_buckets: Dict[str, deque] = defaultdict(deque)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_key(name: str) -> str:
    return os.getenv(name, "").strip()


def validate_safe_url(url: str) -> bool:
    """Reject non-HTTP(S) schemes and private/loopback/link-local targets (SSRF guard)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().strip(".")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    except ValueError:
        # Domain name: do not perform blocking DNS for every result URL, just reject obvious local patterns
        if host.endswith(".internal") or host.endswith(".lan"):
            return False
    return True


def _validate_searxng_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("SearXNG URL must start with http:// or https://")
    return cleaned


def init_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_search_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 1,
            public_enabled INTEGER NOT NULL DEFAULT 0,
            primary_provider TEXT NOT NULL DEFAULT 'duckduckgo',
            fallback_provider TEXT NOT NULL DEFAULT 'wikipedia',
            searxng_url TEXT NOT NULL DEFAULT '',
            cache_ttl_seconds INTEGER NOT NULL DEFAULT 900,
            public_rpm INTEGER NOT NULL DEFAULT 3,
            admin_rpm INTEGER NOT NULL DEFAULT 20,
            timeout_seconds REAL NOT NULL DEFAULT 7.0,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_search_cache (
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            provider TEXT NOT NULL,
            results_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_cache_exp ON web_search_cache(expires_at);")
    default_searxng = os.getenv("AGTOOLS_SEARXNG_URL", "").strip().rstrip("/")
    default_primary = "searxng" if default_searxng else "duckduckgo"
    default_fallback = "duckduckgo" if default_searxng else "wikipedia"
    conn.execute(
        """
        INSERT OR IGNORE INTO web_search_settings (
            id, enabled, public_enabled, primary_provider, fallback_provider,
            searxng_url, cache_ttl_seconds, public_rpm, admin_rpm, timeout_seconds, updated_at
        ) VALUES (1, 1, 0, ?, ?, ?, 900, 3, 20, 7.0, ?)
        """,
        (default_primary, default_fallback, default_searxng, _now_iso()),
    )
    conn.commit()


def get_settings() -> Dict[str, Any]:
    with get_db_connection() as conn:
        init_schema(conn)
        row = conn.execute("SELECT * FROM web_search_settings WHERE id = 1").fetchone()
    return {
        "enabled": bool(row["enabled"]),
        "public_enabled": bool(row["public_enabled"]),
        "primary_provider": row["primary_provider"],
        "fallback_provider": row["fallback_provider"],
        "searxng_url": row["searxng_url"] or "",
        "cache_ttl_seconds": int(row["cache_ttl_seconds"]),
        "public_rpm": int(row["public_rpm"]),
        "admin_rpm": int(row["admin_rpm"]),
        "timeout_seconds": float(row["timeout_seconds"]),
        "searxng_configured": bool((row["searxng_url"] or "").strip()),
        "brave_configured": bool(_env_key("AGTOOLS_BRAVE_API_KEY")),
        "tavily_configured": bool(_env_key("AGTOOLS_TAVILY_API_KEY")),
        "updated_at": row["updated_at"],
    }


def save_settings(
    *,
    enabled: bool,
    public_enabled: bool,
    primary_provider: str,
    fallback_provider: str,
    searxng_url: str,
    cache_ttl_seconds: int,
    public_rpm: int,
    admin_rpm: int,
    timeout_seconds: float,
) -> Dict[str, Any]:
    if primary_provider not in VALID_PROVIDERS or primary_provider == "none":
        raise ValueError("Invalid primary_provider")
    if fallback_provider not in VALID_PROVIDERS:
        raise ValueError("Invalid fallback_provider")
    clean_url = _validate_searxng_url(searxng_url)
    with get_db_connection() as conn:
        init_schema(conn)
        conn.execute(
            """
            UPDATE web_search_settings
            SET enabled = ?, public_enabled = ?, primary_provider = ?, fallback_provider = ?,
                searxng_url = ?, cache_ttl_seconds = ?, public_rpm = ?, admin_rpm = ?,
                timeout_seconds = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                1 if enabled else 0,
                1 if public_enabled else 0,
                primary_provider,
                fallback_provider,
                clean_url,
                max(0, min(int(cache_ttl_seconds), 86400)),
                max(0, min(int(public_rpm), 300)),
                max(0, min(int(admin_rpm), 600)),
                max(1.0, min(float(timeout_seconds), 20.0)),
                _now_iso(),
            ),
        )
        conn.commit()
    return get_settings()


def clear_cache() -> int:
    with get_db_connection() as conn:
        init_schema(conn)
        cur = conn.execute("DELETE FROM web_search_cache")
        conn.commit()
        return cur.rowcount or 0


def _check_rate_limit(bucket_key: str, rpm: int) -> bool:
    if rpm <= 0:
        return True
    now = time.monotonic()
    with _rate_lock:
        dq = _rate_buckets[bucket_key]
        while dq and now - dq[0] >= 60.0:
            dq.popleft()
        if len(dq) >= rpm:
            return False
        dq.append(now)
        return True



def _is_portal_root_url(url: str) -> bool:
    try:
        p = urlparse(url)
        segs = [s for s in (p.path or "").split("/") if s]
        if len(segs) == 0:
            return True
        if len(segs) == 1 and segs[0].lower() in ("main", "index", "home", "news", "market", "stock", "domestic", "finance", "economy") and not p.query:
            return True
        return False
    except Exception:
        return False


def _evaluate_search_quality(results: List[Dict[str, Any]], category: str, time_range: str) -> Dict[str, Any]:
    if not results:
        hints = []
        if category == "news":
            hints.append({"tool": "web.search", "args": {"category": "general", "time_range": "all"}, "why": "뉴스 카테고리 결과 없음 — 일반 검색으로 확대"})
        else:
            hints.append({"tool": "web.search", "args": {"category": "news", "time_range": "week"}, "why": "검색 결과 없음 — 뉴스/다른 키워드로 재검색"})
        return {"quality": "empty", "reason": "zero_hits", "next_hints": hints}
    portal_count = sum(1 for r in results if _is_portal_root_url(r.get("url") or ""))
    avg_snip = sum(len((r.get("snippet") or "").strip()) for r in results) / max(1, len(results))
    if (portal_count / len(results) >= 0.6) or avg_snip < 55:
        hints = []
        if category != "news":
            hints.append({"tool": "web.search", "args": {"category": "news", "time_range": "day"}, "why": "포털 메인 링크 위주 결과 — news 카테고리(time_range=day)로 개별 최신 기사 재검색"})
        article_urls = [r["url"] for r in results if not _is_portal_root_url(r.get("url") or "")]
        if article_urls:
            hints.append({"tool": "web.fetch", "args": {"url": article_urls[0]}, "why": "상위 기사 URL 본문 직접 조회"})
        return {"quality": "shallow", "reason": "portal_links_only", "next_hints": hints}
    return {"quality": "sufficient", "reason": "ok", "next_hints": []}

def _cache_key(query: str, limit: int, lang: str, primary: str, category: str = "general", time_range: str = "all", max_snippet_chars: int = 800) -> str:
    raw = f"{primary}|{category}|{time_range}|{lang}|{limit}|{max_snippet_chars}|{query.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    now = time.time()
    with get_db_connection() as conn:
        init_schema(conn)
        row = conn.execute(
            "SELECT provider, results_json, expires_at FROM web_search_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        if float(row["expires_at"]) <= now:
            conn.execute("DELETE FROM web_search_cache WHERE cache_key = ?", (key,))
            conn.commit()
            return None
        return row["provider"], json.loads(row["results_json"])


def _write_cache(key: str, query: str, provider: str, results: List[Dict[str, Any]], ttl: int) -> None:
    if ttl <= 0:
        return
    now = time.time()
    with get_db_connection() as conn:
        init_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO web_search_cache (cache_key, query, provider, results_json, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, query[:200], provider, json.dumps(results, ensure_ascii=False), now + ttl, _now_iso()),
        )
        # Opportunistically purge expired rows
        conn.execute("DELETE FROM web_search_cache WHERE expires_at <= ?", (now,))
        conn.commit()


def _record_log(
    *,
    tenant_id: str,
    query: str,
    mode: str,
    provider: str,
    result_count: int,
    latency_ms: int,
    cache_hit: bool,
    fallback_used: bool,
    detail: str = "",
) -> None:
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO search_engine_logs (
                    engine_type, tenant_id, query, mode, provider,
                    result_count, latency_ms, cache_hit, fallback_used, detail, created_at
                ) VALUES ('web', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    query[:200],
                    mode,
                    provider,
                    int(result_count),
                    int(latency_ms),
                    1 if cache_hit else 0,
                    1 if fallback_used else 0,
                    detail[:200],
                    _now_iso(),
                ),
            )
            conn.execute(
                """
                DELETE FROM search_engine_logs
                WHERE engine_type = 'web'
                  AND id NOT IN (
                      SELECT id FROM search_engine_logs
                      WHERE engine_type = 'web'
                      ORDER BY id DESC LIMIT 500
                  )
                """
            )
            conn.commit()
    except Exception:
        pass


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: List[Dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._cur_title: List[str] = []
        self._cur_href: str = ""
        self._cur_snippet: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = dict(attrs)
        cls = attr_map.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._in_title = True
            self._cur_title = []
            self._cur_href = attr_map.get("href") or ""
        elif tag in ("a", "div", "span") and "result__snippet" in cls:
            self._in_snippet = True
            self._cur_snippet = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            title = re.sub(r"\s+", " ", unescape("".join(self._cur_title))).strip()
            url = self._normalize_ddg_href(self._cur_href)
            if title and url:
                self.items.append({"title": title, "url": url, "snippet": ""})
        elif self._in_snippet and tag in ("a", "div", "span"):
            self._in_snippet = False
            snippet = re.sub(r"\s+", " ", unescape("".join(self._cur_snippet))).strip()
            if self.items and snippet and not self.items[-1]["snippet"]:
                self.items[-1]["snippet"] = snippet

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._cur_title.append(data)
        elif self._in_snippet:
            self._cur_snippet.append(data)

    @staticmethod
    def _normalize_ddg_href(href: str) -> str:
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
            qs = parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                return unquote(qs["uddg"][0])
        return href


def _provider_searxng(query: str, limit: int, lang: str, timeout: float, searxng_url: str, category: str = "general", time_range: str = "all", max_snippet_chars: int = 800) -> List[Dict[str, Any]]:
    if not searxng_url:
        raise RuntimeError("searxng_not_configured")
    params = {"q": query, "format": "json"}
    if category == "news":
        params["categories"] = "news"
    if time_range in ("day", "week", "month", "year"):
        params["time_range"] = time_range
    if lang and lang != "auto":
        params["language"] = lang
    resp = httpx.get(
        f"{searxng_url}/search",
        params=params,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("results", []):
        url = (item.get("url") or "").strip()
        if not validate_safe_url(url):
            continue
        results.append(
            {
                "title": (item.get("title") or url).strip()[:200],
                "url": url,
                "snippet": (item.get("content") or "").strip()[:max_snippet_chars],
                "source": item.get("engine") or "searxng",
            }
        )
        if len(results) >= limit:
            break
    return results


def _provider_duckduckgo(query: str, limit: int, lang: str, timeout: float, max_snippet_chars: int = 800) -> List[Dict[str, Any]]:
    resp = httpx.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": DEFAULT_USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    parser = _DuckDuckGoHTMLParser()
    parser.feed(resp.text)
    results = []
    for item in parser.items:
        url = item["url"]
        if not validate_safe_url(url):
            continue
        results.append(
            {
                "title": item["title"][:200],
                "url": url,
                "snippet": item["snippet"][:max_snippet_chars],
                "source": "duckduckgo",
            }
        )
        if len(results) >= limit:
            break
    return results


def _provider_wikipedia(query: str, limit: int, lang: str, timeout: float, max_snippet_chars: int = 800) -> List[Dict[str, Any]]:
    has_korean = bool(re.search(r"[가-힣]", query))
    wiki_lang = "ko" if (lang.startswith("ko") or (lang == "auto" and has_korean)) else "en"
    resp = httpx.get(
        f"https://{wiki_lang}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": limit,
            "utf8": 1,
        },
        headers={"User-Agent": DEFAULT_USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("query", {}).get("search", []):
        title = item.get("title") or ""
        page_id = item.get("pageid")
        url = f"https://{wiki_lang}.wikipedia.org/?curid={page_id}" if page_id else ""
        if not url or not validate_safe_url(url):
            continue
        raw_snippet = re.sub(r"<[^>]+>", "", item.get("snippet") or "")
        results.append(
            {
                "title": title[:200],
                "url": url,
                "snippet": unescape(raw_snippet).strip()[:max_snippet_chars],
                "source": f"wikipedia-{wiki_lang}",
            }
        )
        if len(results) >= limit:
            break
    return results


def _provider_brave(query: str, limit: int, lang: str, timeout: float, max_snippet_chars: int = 800) -> List[Dict[str, Any]]:
    api_key = _env_key("AGTOOLS_BRAVE_API_KEY")
    if not api_key:
        raise RuntimeError("brave_not_configured")
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("web", {}).get("results", []):
        url = (item.get("url") or "").strip()
        if not validate_safe_url(url):
            continue
        results.append(
            {
                "title": (item.get("title") or url).strip()[:200],
                "url": url,
                "snippet": (item.get("description") or "").strip()[:max_snippet_chars],
                "source": "brave",
            }
        )
        if len(results) >= limit:
            break
    return results


def _provider_tavily(query: str, limit: int, lang: str, timeout: float, max_snippet_chars: int = 800) -> List[Dict[str, Any]]:
    api_key = _env_key("AGTOOLS_TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("tavily_not_configured")
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": limit},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("results", []):
        url = (item.get("url") or "").strip()
        if not validate_safe_url(url):
            continue
        results.append(
            {
                "title": (item.get("title") or url).strip()[:200],
                "url": url,
                "snippet": (item.get("content") or "").strip()[:max_snippet_chars],
                "source": "tavily",
            }
        )
        if len(results) >= limit:
            break
    return results


def _call_provider(provider: str, query: str, limit: int, lang: str, cfg: Dict[str, Any], category: str = "general", time_range: str = "all", max_snippet_chars: int = 800) -> List[Dict[str, Any]]:
    timeout = float(cfg["timeout_seconds"])
    if provider == "searxng":
        return _provider_searxng(query, limit, lang, timeout, cfg["searxng_url"], category=category, time_range=time_range, max_snippet_chars=max_snippet_chars)
    if provider == "duckduckgo":
        return _provider_duckduckgo(query, limit, lang, timeout, max_snippet_chars=max_snippet_chars)
    if provider == "wikipedia":
        return _provider_wikipedia(query, limit, lang, timeout, max_snippet_chars=max_snippet_chars)
    if provider == "brave":
        return _provider_brave(query, limit, lang, timeout, max_snippet_chars=max_snippet_chars)
    if provider == "tavily":
        return _provider_tavily(query, limit, lang, timeout, max_snippet_chars=max_snippet_chars)
    raise ValueError(f"Unsupported provider: {provider}")


def search(
    *,
    query: str,
    limit: int = 5,
    lang: str = "auto",
    category: str = "general",
    time_range: str = "all",
    max_snippet_chars: int = 800,
    tenant_id: str = "default_tenant",
    owner_id: str = "admin",
    bypass_cache: bool = False,
) -> Dict[str, Any]:
    started = time.perf_counter()
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Search query cannot be empty")
    limit = max(1, min(int(limit), 25))
    max_snippet_chars = max(100, min(int(max_snippet_chars), 3000))
    cfg = get_settings()

    if not cfg["enabled"]:
        raise PermissionError("Web search is currently disabled")
    if tenant_id == "public_tenant" and not cfg["public_enabled"]:
        raise PermissionError("Web search is disabled for public tenant")

    key = _cache_key(clean_query, limit, lang, cfg["primary_provider"], category, time_range, max_snippet_chars)
    if not bypass_cache and cfg["cache_ttl_seconds"] > 0:
        cached = _read_cache(key)
        if cached is not None:
            cached_provider, cached_results = cached
            latency_ms = int((time.perf_counter() - started) * 1000)
            _record_log(
                tenant_id=tenant_id,
                query=clean_query,
                mode="cache_hit",
                provider=cached_provider,
                result_count=len(cached_results),
                latency_ms=latency_ms,
                cache_hit=True,
                fallback_used=False,
                detail="sqlite-ttl-cache",
            )
            return {
                "query": clean_query,
                "provider": cached_provider,
                "cache_hit": True,
                "fallback_used": False,
                "latency_ms": latency_ms,
                "category": category,
                "time_range": time_range,
                "total_results": len(cached_results),
                "results": cached_results,
                "meta": _evaluate_search_quality(cached_results, category, time_range),
            }

    rpm = cfg["public_rpm"] if tenant_id == "public_tenant" else cfg["admin_rpm"]
    bucket_key = f"{tenant_id}:{owner_id}"
    if not _check_rate_limit(bucket_key, rpm):
        raise RuntimeError("rate_limited")

    primary = cfg["primary_provider"]
    fallback = cfg["fallback_provider"]
    used_provider = primary
    fallback_used = False
    primary_err = ""
    results: List[Dict[str, Any]] = []

    try:
        results = _call_provider(primary, clean_query, limit, lang, cfg, category=category, time_range=time_range, max_snippet_chars=max_snippet_chars)
    except Exception as exc:
        primary_err = type(exc).__name__
        results = []

    if not results and fallback and fallback != "none" and fallback != primary:
        try:
            results = _call_provider(fallback, clean_query, limit, lang, cfg, category=category, time_range=time_range, max_snippet_chars=max_snippet_chars)
            used_provider = fallback
            fallback_used = True
        except Exception as exc:
            detail = f"primary={primary}({primary_err or 'empty'}) fallback={fallback}({type(exc).__name__})"
            latency_ms = int((time.perf_counter() - started) * 1000)
            _record_log(
                tenant_id=tenant_id,
                query=clean_query,
                mode="error",
                provider=fallback,
                result_count=0,
                latency_ms=latency_ms,
                cache_hit=False,
                fallback_used=True,
                detail=detail,
            )
            raise RuntimeError(f"search_failed:{detail}")

    if results:
        _write_cache(key, clean_query, used_provider, results, cfg["cache_ttl_seconds"])

    latency_ms = int((time.perf_counter() - started) * 1000)
    mode = "fallback" if fallback_used else "primary"
    detail = f"primary_err={primary_err}" if primary_err else "ok"
    _record_log(
        tenant_id=tenant_id,
        query=clean_query,
        mode=mode,
        provider=used_provider,
        result_count=len(results),
        latency_ms=latency_ms,
        cache_hit=False,
        fallback_used=fallback_used,
        detail=detail,
    )
    return {
        "query": clean_query,
        "provider": used_provider,
        "cache_hit": False,
        "fallback_used": fallback_used,
        "latency_ms": latency_ms,
        "category": category,
        "time_range": time_range,
        "total_results": len(results),
        "results": results,
        "meta": _evaluate_search_quality(results, category, time_range),
    }


def status(log_limit: int = 15) -> Dict[str, Any]:
    cfg = get_settings()
    now = time.time()
    with get_db_connection() as conn:
        init_schema(conn)
        active_cache = conn.execute(
            "SELECT COUNT(*) FROM web_search_cache WHERE expires_at > ?",
            (now,),
        ).fetchone()[0]
        stats_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_queries,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits,
                SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_queries
            FROM search_engine_logs
            WHERE engine_type = 'web'
            """
        ).fetchone()
        recent_rows = conn.execute(
            """
            SELECT id, tenant_id, query, mode, provider, result_count, latency_ms, cache_hit, fallback_used, detail, created_at
            FROM search_engine_logs
            WHERE engine_type = 'web'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(log_limit, 50)),),
        ).fetchall()

    total_q = int(stats_row["total_queries"] or 0)
    cache_hits = int(stats_row["cache_hits"] or 0)
    return {
        "settings": cfg,
        "active_cache_entries": int(active_cache or 0),
        "total_queries": total_q,
        "avg_latency_ms": round(float(stats_row["avg_latency_ms"] or 0), 1),
        "cache_hits": cache_hits,
        "cache_hit_rate": round((cache_hits / total_q) * 100, 1) if total_q else 0.0,
        "fallback_queries": int(stats_row["fallback_queries"] or 0),
        "recent_logs": [dict(r) for r in recent_rows],
    }
