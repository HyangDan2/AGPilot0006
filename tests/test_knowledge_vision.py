"""WP-07 Vision interface: capability-driven model selection, honest fallback."""

from __future__ import annotations

import json

from app.knowledge.vision import VisionEngine, load_vision_capability
from tests.knowledge_fixtures import png_bytes


def test_without_capability_file_vision_is_explicitly_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_VISION_CAPABILITY_FILE", raising=False)
    capability = load_vision_capability(str(tmp_path / "absent.json"))
    assert capability.available is False
    engine = VisionEngine(capability)
    result = engine.analyze(png_bytes(), "image/png", "describe")
    assert result.available is False
    assert result.error in {"capability_file_missing", "vision_unavailable", "no_capability_file"}


def test_capability_file_selects_the_first_model_declaring_vision(tmp_path):
    path = tmp_path / "vision.json"
    path.write_text(
        json.dumps(
            {
                "engine": "gateway",
                "capability_source": "gateway:/v1/models",
                "models": [
                    {"id": "text-only-model", "capabilities": ["text"]},
                    {"id": "vision-model-x", "capabilities": ["text", "vision"]},
                ],
                "ocr_fallback": True,
                "ocr_engine": "tesseract",
            }
        ),
        encoding="utf-8",
    )
    capability = load_vision_capability(str(path))
    assert capability.available is True
    assert capability.model == "vision-model-x"  # never a hard-coded name
    assert capability.capability_source == "gateway:/v1/models"


def test_declared_capability_without_bound_analyzer_is_not_reported_available(tmp_path):
    path = tmp_path / "vision.json"
    path.write_text(json.dumps({"available": True, "model": "gateway-vlm", "engine": "gateway"}), encoding="utf-8")
    engine = VisionEngine(load_vision_capability(str(path)))
    status = engine.status()
    assert status.available is False
    assert status.reason == "analyzer_not_bound"
    assert status.model == "gateway-vlm"


def test_bound_analyzer_is_used_and_reports_its_model(tmp_path):
    path = tmp_path / "vision.json"
    path.write_text(json.dumps({"available": True, "model": "gateway-vlm", "engine": "gateway"}), encoding="utf-8")
    seen = {}

    def analyzer(image: bytes, mime: str, prompt: str) -> str:
        seen["mime"] = mime
        seen["prompt"] = prompt
        return "A bar chart titled Revenue"

    engine = VisionEngine(load_vision_capability(str(path)), analyzer=analyzer)
    result = engine.analyze(png_bytes(), "image/png", "describe the chart")
    assert result.available is True
    assert result.text == "A bar chart titled Revenue"
    assert result.model == "gateway-vlm"
    assert seen["mime"] == "image/png"


def test_analyzer_failure_falls_back_to_ocr_without_inventing_content(tmp_path):
    path = tmp_path / "vision.json"
    path.write_text(
        json.dumps({"available": True, "model": "gateway-vlm", "ocr_fallback": True, "ocr_engine": "fake"}),
        encoding="utf-8",
    )

    def analyzer(image: bytes, mime: str, prompt: str) -> str:
        raise RuntimeError("gateway down")

    engine = VisionEngine(
        load_vision_capability(str(path)),
        analyzer=analyzer,
        ocr=lambda image, mime: "OCR TEXT",
    )
    result = engine.analyze(png_bytes(), "image/png")
    assert result.available is True
    assert result.engine == "ocr"
    assert result.error and result.error.startswith("vision_failed")
    assert result.text == "OCR TEXT"


def test_no_fabrication_when_every_path_is_unavailable(tmp_path):
    path = tmp_path / "vision.json"
    path.write_text(json.dumps({"available": False, "ocr_fallback": False}), encoding="utf-8")
    engine = VisionEngine(load_vision_capability(str(path)))
    result = engine.analyze(png_bytes(), "image/png")
    assert result.available is False
    assert result.text == ""
    assert result.error
