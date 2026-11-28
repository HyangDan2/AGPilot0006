"""Unit tests for ``app/r3_capabilities.py`` — the read-only R3 capability manifest.

These tests need no external service, database or parser dependency: they inspect
the manifest and the route decorators declared in ``app/api/v1/*.py``.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from app import r3_capabilities as caps
from app.config import settings

MODULE_PATH = Path(caps.__file__)
WS07_FIELDS = set(caps.UNIFIED_TOOL_SPEC_FIELDS)
STDLIB_IMPORTS = {"__future__", "ast", "copy", "dataclasses", "pathlib", "typing"}


# --------------------------------------------------------------------------- #
# Manifest shape
# --------------------------------------------------------------------------- #


def test_manifest_header_identifies_provider_and_contract():
    manifest = caps.capability_manifest()
    assert manifest["schema"] == caps.MANIFEST_SCHEMA == 1
    assert manifest["contract"] == "r3-capability/v1"
    assert manifest["provider"] == "agtools"
    assert manifest["service"]["id"] == "AGPilot0006"
    assert manifest["service"]["version"] == settings.PROJECT_VERSION
    assert manifest["spec_fields"] == list(caps.UNIFIED_TOOL_SPEC_FIELDS)
    assert manifest["admin_surfaces_excluded"]["marker"] == "/settings/"


def test_every_capability_carries_exactly_the_wp07_field_set():
    capabilities = caps.list_r3_capabilities()
    assert capabilities, "capability manifest must not be empty"
    for entry in capabilities:
        assert set(entry) == WS07_FIELDS, f"{entry.get('name')} field drift: {sorted(set(entry) ^ WS07_FIELDS)}"
        assert entry["source"] == "agtools"
        assert isinstance(entry["description"], str) and entry["description"].strip()
        assert isinstance(entry["input_schema"], dict) and entry["input_schema"]["type"] == "object"
        assert isinstance(entry["output_schema"], dict) and entry["output_schema"]
        assert isinstance(entry["approval_required"], bool)
        # WP-07 validates 0 < timeout <= 600.
        assert isinstance(entry["timeout_seconds"], (int, float)) and 0 < entry["timeout_seconds"] <= 600
        assert re.fullmatch(r"\d+(\.\d+){0,3}([-+][a-zA-Z0-9.\-]+)?", entry["version"]), entry["version"]
        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}", entry["namespace"]), entry["namespace"]
        assert entry["permission"] in caps.PERMISSIONS
        assert entry["side_effect"] in caps.SIDE_EFFECTS
        # WP-07 takes a mapping here, not an id string.
        assert isinstance(entry["error_contract"], dict)
        assert entry["error_contract"]["id"] in caps.ERROR_CONTRACTS
        assert isinstance(entry["error_contract"].get("retryable"), list)


def test_capability_names_are_unique():
    names = [c["name"] for c in caps.list_r3_capabilities()]
    assert len(names) == len(set(names))
    assert names  # sanity


def test_required_input_arguments_are_declared_as_properties():
    for entry in caps.list_r3_capabilities():
        schema = entry["input_schema"]
        declared = set(schema.get("properties", {}))
        for required in schema.get("required", []):
            assert required in declared, f"{entry['name']}: required '{required}' is not a property"


def test_manifest_is_json_serializable_and_stable():
    first = caps.capability_manifest()
    payload = json.dumps(first, ensure_ascii=False, sort_keys=False)
    assert payload and json.loads(payload) == first
    assert caps.capability_manifest() == first


def test_list_returns_independent_copies():
    first = caps.list_r3_capabilities()
    first[0]["name"] = "tampered"
    first[0]["input_schema"]["properties"].clear()
    assert caps.list_r3_capabilities()[0]["name"] != "tampered"
    assert caps.list_r3_capabilities()[0]["input_schema"]["properties"]


def test_get_capability_lookup_and_unknown_name():
    spec = caps.get_r3_capability("document.search")
    assert spec["capability"] == "workspace.rag.search"
    with pytest.raises(KeyError):
        caps.get_r3_capability("document.delete_everything")


def test_filters_use_wp07_dimensions():
    all_caps = caps.list_r3_capabilities()
    # WP-07 qualified_name is `namespace.name`, so a per-family namespace would double
    # the prefix (`agtools.document.document.search`). The family lives in `name` instead.
    assert {c["namespace"] for c in all_caps} == {"agtools"}
    assert {c["name"].split(".")[0] for c in all_caps} == {"document", "docx", "web", "tools"}
    assert all(f"agtools.{c['name']}" == f"agtools.{c['name']}" for c in all_caps)
    by_namespace = caps.find_capabilities(namespace="agtools")
    assert len(by_namespace) == len(all_caps)
    rag = caps.find_capabilities(capability="workspace.rag.search")
    assert [c["name"] for c in rag] == ["document.search"]
    writes = caps.find_capabilities(side_effect="write")
    assert {c["name"] for c in writes} == {"document.parse", "docx.from_markdown"}
    external = caps.find_capabilities(side_effect="external")
    assert {c["name"] for c in external} == {"web.search", "web.fetch"}
    gated = caps.find_capabilities(approval_required=True)
    assert [c["name"] for c in gated] == ["docx.from_markdown"]


def test_side_effect_and_approval_invariants():
    """Mirror the WP-07 cross-field policy so a spec cannot be rejected at registration."""
    for entry in caps.list_r3_capabilities():
        if entry["side_effect"] in {"write", "external", "destructive"}:
            assert entry["permission"] != "read", f"{entry['name']}: writable side effect needs a write permission"
        if entry["side_effect"] == "destructive":
            assert entry["approval_required"] is True
        if entry["approval_required"]:
            assert entry["side_effect"] not in {"none", "read"}, (
                f"{entry['name']}: approval gate on a pure read is contradictory"
            )
        if entry["side_effect"] in {"none", "read"}:
            assert entry["approval_required"] is False


def test_capability_ids_are_unique_per_tool():
    """WP-07 keeps one live implementation per capability (DuplicateCapabilityError otherwise)."""
    capabilities = [c["capability"] for c in caps.list_r3_capabilities()]
    assert len(capabilities) == len(set(capabilities))


def test_read_only_module_never_imports_r21_code():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are unexpected here"
            imported.add((node.module or "").split(".")[0])
    assert imported <= STDLIB_IMPORTS, f"non-stdlib imports: {sorted(imported - STDLIB_IMPORTS)}"


# --------------------------------------------------------------------------- #
# Route truth
# --------------------------------------------------------------------------- #


def test_api_prefix_matches_service_configuration():
    assert caps.collect_api_prefix() == settings.API_V1_STR


def test_manifest_matches_declared_routes_exactly():
    coverage = caps.check_route_coverage()
    assert coverage.missing == (), f"manifest claims routes that do not exist: {coverage.missing}"
    assert coverage.uncovered == (), f"public routes missing from the manifest: {coverage.uncovered}"
    assert coverage.duplicates == (), f"duplicate bindings: {coverage.duplicates}"
    assert coverage.ok is True


def test_admin_surfaces_are_excluded_on_purpose():
    coverage = caps.check_route_coverage()
    assert coverage.admin_excluded, "expected administrator routes to be classified separately"
    assert all("/settings/" in key for key in coverage.admin_excluded)


def test_bindings_cover_every_capability_once():
    names = [c["name"] for c in caps.list_r3_capabilities()]
    bindings = caps.list_r3_capability_bindings()
    assert [b["name"] for b in bindings] == names
    for binding in bindings:
        assert binding["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert binding["path"].startswith(settings.API_V1_STR + "/")
        assert binding["request_body"] in {"none", "json", "multipart/form-data"}
        assert binding["auth"] in {"caller", "admin"}
        assert isinstance(binding["document_scoped"], bool)
        assert binding["description"].strip()


def test_route_coverage_detects_drift():
    declared = caps.collect_declared_routes()
    trimmed = [r for r in declared if r["path"] != "/api/v1/web/search"]
    coverage = caps.check_route_coverage(trimmed)
    assert coverage.ok is False
    assert "POST /api/v1/web/search" in coverage.missing


def test_alias_route_is_covered_and_not_reported_uncovered():
    declaring = {f"{r['method']} {r['path']}" for r in caps.collect_declared_routes()}
    assert "GET /api/v1/documents/{id}/assets/{asset_id}" in declaring
    alias = caps.list_r3_capability_bindings()
    asset = next(b for b in alias if b["name"] == "document.get_figure_asset")
    assert {"method": "GET", "path": "/api/v1/documents/{id}/assets/{asset_id}"} in asset["aliases"]
    assert "GET /api/v1/documents/{id}/assets/{asset_id}" not in caps.check_route_coverage().uncovered


# --------------------------------------------------------------------------- #
# Cross-repo alignment
# --------------------------------------------------------------------------- #


def test_shared_capability_interface_is_consistent():
    interface = caps.capability_coverage()
    ids = [item["capability"] for item in interface]
    assert set(ids) == {
        "workspace.rag.search",
        "office.docx.compare",
        "office.xlsx.analyze",
        "office.pptx.generate",
        "workspace.file.write",
    }
    provided = {c["capability"] for c in caps.list_r3_capabilities()}
    for item in interface:
        assert item["status"] in {"provided", "not_provided"}
        if item["status"] == "provided":
            assert item["provider"] in {c["name"] for c in caps.list_r3_capabilities()}
            assert item["capability"] in provided
        else:
            assert item["provider"] is None
            assert item["capability"] not in provided


def _agcore_v1_tool_names() -> set[str]:
    """Read ``DEFAULT_TOOLS`` names out of ``tool_manifest.py`` without importing it.

    Importing the module pulls in the embedding service (and its numpy dependency),
    which this unit test deliberately does not require.
    """
    source = Path(caps.__file__).resolve().parent / "api" / "v1" / "tool_manifest.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = {node.target.id}
        else:
            continue
        if "DEFAULT_TOOLS" not in targets:
            continue
        assert isinstance(node.value, ast.List), "DEFAULT_TOOLS must stay a literal list"
        names = set()
        for element in node.value.elts:
            assert isinstance(element, ast.Dict)
            keys = [k.value for k in element.keys if isinstance(k, ast.Constant)]
            values = dict(zip(keys, element.values))
            name = values.get("name")
            assert isinstance(name, ast.Constant) and isinstance(name.value, str)
            names.add(name.value)
        return names
    raise AssertionError("DEFAULT_TOOLS not found in app/api/v1/tool_manifest.py")


def test_agcore_v1_tool_manifest_names_are_a_subset():
    """Every tool name published to AGCore through /tools/manifest stays addressable in R3."""
    advertised = _agcore_v1_tool_names()
    assert advertised, "expected the AGCore v1 manifest to advertise tools"
    r3_names = {c["name"] for c in caps.list_r3_capabilities()}
    assert advertised <= r3_names, f"v1 tools missing from the R3 manifest: {sorted(advertised - r3_names)}"


def test_v1_and_r3_manifests_share_one_argument_dialect():
    """Document-id based tools agree on ``doc_id`` so an adapter needs no renaming."""
    r3 = {c["name"]: c for c in caps.list_r3_capabilities()}
    for name in ("document.get_outline", "document.read_chunk", "docx.to_markdown"):
        assert "doc_id" in r3[name]["input_schema"]["required"]
