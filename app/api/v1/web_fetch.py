"""ADR-0017/0018: SSRF-safe public web page fetcher with bounded Chromium CSR fallback."""
from html.parser import HTMLParser
import ipaddress
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from typing import List, Tuple
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import CallerContext, get_current_caller

router = APIRouter(tags=["web fetch"])

MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
CSR_THRESHOLD_CHARS = 300
CHROMIUM_BIN = shutil.which("chromium") or shutil.which("chromium-browser")
_BROWSER_SEM = threading.BoundedSemaphore(2)


class WebFetchRequest(BaseModel):
    url: str = Field(..., min_length=3, max_length=500)
    max_chars: int = Field(8000, ge=500, le=100000)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_title = False
        self.title_parts: List[str] = []
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg", "iframe"):
            self._skip += 1
        elif t == "title":
            self._in_title = True
        elif t in ("p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "br", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg", "iframe") and self._skip > 0:
            self._skip -= 1
        elif t == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        text = data.strip()
        if text:
            self.parts.append(text + " ")


def _extract_html_text(html_text: str) -> Tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html_text)
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    cleaned = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return title, cleaned


def _normalize_and_validate_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "유효한 http/https URL이 필요합니다.")
    if parsed.username or parsed.password:
        raise HTTPException(400, "인증 정보가 포함된 URL은 허용되지 않습니다.")
    host = parsed.hostname.lower()
    if host in ("localhost", "ip6-localhost") or host.endswith((".local", ".internal", ".lan")):
        raise HTTPException(403, "내부 네트워크 주소는 조회할 수 없습니다.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(400, f"호스트를 찾을 수 없습니다: {host}") from exc
    if not infos:
        raise HTTPException(400, "호스트 IP를 확인할 수 없습니다.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(403, "사설/내부 IP 주소 접근은 차단됩니다.")
    return url


def _render_with_chromium(url: str) -> Tuple[str, str]:
    if not CHROMIUM_BIN:
        return "", ""
    acquired = _BROWSER_SEM.acquire(timeout=3.0)
    if not acquired:
        return "", ""
    try:
        with tempfile.TemporaryDirectory(prefix="agtools-chr-") as profile_dir:
            cmd = [
                CHROMIUM_BIN,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-extensions",
                "--mute-audio",
                "--disable-background-networking",
                "--blink-settings=imagesEnabled=false",
                "--virtual-time-budget=2500",
                "--user-agent=Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                f"--user-data-dir={profile_dir}",
                "--dump-dom",
                url,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout:
                return "", ""
            html_text = proc.stdout[: MAX_BYTES * 2].decode("utf-8", errors="replace")
            return _extract_html_text(html_text)
    except Exception:
        return "", ""
    finally:
        _BROWSER_SEM.release()


@router.post("/web/fetch")
def fetch_web_page(
    body: WebFetchRequest,
    caller: CallerContext = Depends(get_current_caller),
):
    current_url = _normalize_and_validate_url(body.url)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
    resp = None
    with httpx.Client(timeout=10.0, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                resp = client.get(current_url)
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"웹 페이지 요청 실패: {exc}") from exc
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                next_url = urljoin(current_url, resp.headers["location"])
                current_url = _normalize_and_validate_url(next_url)
                continue
            break

    if resp is None or resp.status_code >= 400:
        code = resp.status_code if resp is not None else 502
        raise HTTPException(502, f"원격 페이지 응답 오류 (HTTP {code})")

    raw = resp.content[:MAX_BYTES]
    ctype = (resp.headers.get("content-type") or "").lower()
    text_raw = raw.decode(resp.encoding or "utf-8", errors="replace")

    is_html = "html" in ctype or "<html" in text_raw[:500].lower()
    if is_html:
        title, cleaned = _extract_html_text(text_raw)
    else:
        title, cleaned = "", text_raw.strip()

    rendered_js = False
    if is_html and len(cleaned) < CSR_THRESHOLD_CHARS and CHROMIUM_BIN:
        js_title, js_cleaned = _render_with_chromium(current_url)
        if len(js_cleaned) > len(cleaned):
            title = js_title or title
            cleaned = js_cleaned
            rendered_js = True

    char_len = min(len(cleaned), body.max_chars)
    truncated = len(cleaned) > body.max_chars
    if char_len == 0:
        meta = {"quality": "empty", "reason": "empty_body", "next_hints": [{"tool": "web.search", "why": "페이지 본문이 비어 있어 검색으로 대체 조회"}]}
    elif char_len < CSR_THRESHOLD_CHARS:
        meta = {"quality": "shallow", "reason": "csr_shell_only", "next_hints": [{"tool": "web.search", "args": {"category": "news", "time_range": "day"}, "why": "동적 렌더링 후에도 본문이 짧아 검색으로 대체 조회"}]}
    else:
        meta = {"quality": "sufficient", "reason": "ok_js_rendered" if rendered_js else "ok", "next_hints": []}

    return {
        "url": current_url,
        "status_code": resp.status_code,
        "title": title,
        "content": cleaned[: body.max_chars],
        "truncated": truncated,
        "chars": char_len,
        "rendered_js": rendered_js,
        "meta": meta,
    }
