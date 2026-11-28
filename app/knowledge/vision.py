"""Vision Engine interface (WP-07).

A vision model is never chosen here. The deployment writes the Gateway's model capability
snapshot to ``KNOWLEDGE_VISION_CAPABILITY_FILE``; this module reads it, reports the model and
its source, and calls whatever analyzer the deployment bound to it. When no capability says
"vision", the engine reports ``vision_unavailable`` and falls back to OCR or the document's own
text layer — it never invents a description.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from app.config import settings
from app.knowledge.contracts import VisionCapability

Analyzer = Callable[[bytes, str, str], str]


@dataclass
class VisionResult:
    available: bool
    text: str = ""
    engine: str = "none"
    model: Optional[str] = None
    capability_source: str = "none"
    error: Optional[str] = None


class VisionEngine:
    def __init__(
        self,
        capability: VisionCapability,
        *,
        analyzer: Optional[Analyzer] = None,
        ocr: Optional[Callable[[bytes, str], str]] = None,
    ) -> None:
        self.capability = capability
        self._analyzer = analyzer
        self._ocr = ocr

    @classmethod
    def from_settings(cls, analyzer: Optional[Analyzer] = None) -> "VisionEngine":
        capability = load_vision_capability()
        return cls(capability, analyzer=analyzer)

    # ------------------------------------------------------------------ status
    def status(self) -> VisionCapability:
        capability = self.capability.model_copy(deep=True)
        if capability.available and self._analyzer is None:
            capability.available = False
            capability.reason = "analyzer_not_bound"
        if not capability.ocr_fallback:
            capability.ocr_engine = None
        return capability

    def available(self) -> bool:
        return self.status().available

    # ------------------------------------------------------------------ analysis
    def analyze(self, image_bytes: bytes, mime_type: str, prompt: str = "") -> VisionResult:
        status = self.status()
        if not image_bytes:
            return VisionResult(False, engine="none", error="empty_image")
        if status.available and self._analyzer is not None:
            try:
                text = self._analyzer(image_bytes, mime_type, prompt)
                return VisionResult(
                    True,
                    text=text or "",
                    engine=status.engine,
                    model=status.model,
                    capability_source=status.capability_source,
                )
            except Exception as exc:
                ocr_text = self._try_ocr(image_bytes, mime_type)
                return VisionResult(
                    bool(ocr_text),
                    text=ocr_text or "",
                    engine="ocr" if ocr_text else "none",
                    model=status.model,
                    capability_source=status.capability_source,
                    error=f"vision_failed:{type(exc).__name__}",
                )
        ocr_text = self._try_ocr(image_bytes, mime_type)
        if ocr_text:
            return VisionResult(True, text=ocr_text, engine="ocr", capability_source="ocr_fallback")
        return VisionResult(
            False,
            engine="none",
            model=status.model,
            capability_source=status.capability_source,
            error=status.reason or "vision_unavailable",
        )

    def _try_ocr(self, image_bytes: bytes, mime_type: str) -> Optional[str]:
        if not self.capability.ocr_fallback:
            return None
        if self._ocr is not None:
            try:
                return self._ocr(image_bytes, mime_type)
            except Exception:
                return None
        if not getattr(settings, "KNOWLEDGE_OCR_ENABLED", False):
            return None
        try:  # optional dependency: never required for a basic install
            import pytesseract  # type: ignore
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes))
            return pytesseract.image_to_string(image) or None
        except Exception:
            return None


def load_vision_capability(path: Optional[str] = None) -> VisionCapability:
    """Read the deployment's Gateway capability snapshot.

    Accepted file shape (written by ops, sourced from the Gateway)::

        {"available": true, "engine": "gateway", "model": "<gateway model id>",
         "capability_source": "gateway:/v1/models", "ocr_fallback": true,
         "ocr_engine": "tesseract"}
    """

    candidate = path or os.getenv("KNOWLEDGE_VISION_CAPABILITY_FILE") or getattr(
        settings, "KNOWLEDGE_VISION_CAPABILITY_FILE", None
    )
    if not candidate:
        return VisionCapability(
            available=False,
            engine="none",
            capability_source="none",
            ocr_fallback=bool(getattr(settings, "KNOWLEDGE_OCR_ENABLED", False)),
            ocr_engine="tesseract" if getattr(settings, "KNOWLEDGE_OCR_ENABLED", False) else None,
            reason="no_capability_file",
        )
    file_path = Path(candidate)
    if not file_path.exists():
        return VisionCapability(
            available=False, engine="none", capability_source=f"file:{candidate}", reason="capability_file_missing"
        )
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VisionCapability(
            available=False,
            engine="none",
            capability_source=f"file:{candidate}",
            reason=f"capability_file_invalid:{type(exc).__name__}",
        )
    models = payload.get("models") or []
    selected = payload.get("model")
    if not selected and models:
        # Pick the first model whose declared capabilities include vision; never a name list.
        for model in models:
            caps = [str(item).lower() for item in model.get("capabilities", [])]
            if "vision" in caps or "image_input" in caps or model.get("vision") is True:
                selected = model.get("id") or model.get("name")
                break
    available = bool(payload.get("available", bool(selected))) and bool(
        payload.get("vision", True)
    )
    if payload.get("models") and not selected:
        available = False
    return VisionCapability(
        available=available,
        engine=str(payload.get("engine", "gateway")),
        model=selected,
        capability_source=str(payload.get("capability_source", f"file:{candidate}")),
        ocr_fallback=bool(payload.get("ocr_fallback", getattr(settings, "KNOWLEDGE_OCR_ENABLED", False))),
        ocr_engine=payload.get("ocr_engine"),
        reason=None if available else "capability_absent",
    )
