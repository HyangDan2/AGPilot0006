"""WP-05 XLSX reader/writer: structure, formulas vs cached values, windowing, writers."""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from app.knowledge.contracts import (
    ContentBlock,
    DocumentRef,
    LocatorKind,
    NodeKind,
    OpKind,
    ReadMode,
    ReadRequest,
    UpdateOperation,
)
from app.knowledge.engines import xlsx_engine
from tests.knowledge_fixtures import make_xlsx

XLSX_MIME = xlsx_engine.XLSX_MIME


def _ref() -> DocumentRef:
    return DocumentRef(document_id="kd_xlsxtest001", name="book.xlsx", mime_type=XLSX_MIME, revision=1)


def test_reads_sheets_cells_tables_charts_merges_and_formulas(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    response = xlsx_engine.read_path(path, _ref(), ReadRequest(document_id="kd_xlsxtest001"))
    assert response.stats.sheets == 2
    meta = next(
        node for node in response.nodes
        if node.kind is NodeKind.METADATA and node.meta.get("sheet") == "Data"
    )
    assert "E1:F1" in meta.meta["merged_cells"]
    assert any(table["name"] == "SalesTable" for table in meta.meta["tables"])
    assert meta.meta["charts"] and meta.meta["charts"][0]["title"] == "Revenue by month"
    table = next(
        node for node in response.nodes
        if node.kind is NodeKind.TABLE and node.locator.sheet == "Data"
    )
    assert table.table.rows[0][:3] == ["Month", "Units", "Revenue"]
    formulas = [node for node in response.nodes if node.kind is NodeKind.FORMULA]
    assert {node.meta["formula"] for node in formulas} == {"=SUM(B2:B4)", "=SUM(C2:C4)"}
    assert all(node.meta["cached_available"] is False for node in formulas)
    assert table.locator.kind is LocatorKind.RANGE


def test_sheet_mode_and_outline(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    response = xlsx_engine.read_path(
        path, _ref(), ReadRequest(document_id="kd_xlsxtest001", mode=ReadMode.SHEET, sheet="Notes")
    )
    assert [node.meta.get("sheet") for node in response.nodes if node.kind is NodeKind.METADATA] == ["Notes"]
    outline = xlsx_engine.read_path(
        path, _ref(), ReadRequest(document_id="kd_xlsxtest001", mode=ReadMode.OUTLINE)
    )
    assert [node.text for node in outline.nodes if node.kind is NodeKind.METADATA] == [
        "sheet Data A1:F5",
        "sheet Notes A1:A1",
    ]


def test_row_window_returns_cursor(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    response = xlsx_engine.read_path(
        path, _ref(), ReadRequest(document_id="kd_xlsxtest001", max_rows=2, mode=ReadMode.SHEET, sheet="Data")
    )
    assert response.truncated is True
    assert response.next_cursor == "sheet:Data:row:3"
    assert len(next(node for node in response.nodes if node.kind is NodeKind.TABLE).table.rows) == 2


def test_set_cell_formula_and_range(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    result = xlsx_engine.apply_xlsx(
        path,
        _ref(),
        [
            UpdateOperation(op=OpKind.SET_CELL, sheet="Data", cell="D2", value=5),
            UpdateOperation(op=OpKind.SET_CELL, sheet="Data", cell="D3", formula="=D2*2"),
            UpdateOperation(op=OpKind.SET_RANGE, sheet="Notes", range="A2:B3", rows=[["x", "y"], ["z", "w"]]),
        ],
    )
    assert len(result.applied) == 3
    assert "formula_recalc_required" in result.warnings
    edited = tmp_path / "edited.xlsx"
    edited.write_bytes(result.data)
    workbook = load_workbook(str(edited))
    assert workbook["Data"]["D2"].value == 5
    assert workbook["Data"]["D3"].value == "=D2*2"
    assert workbook["Notes"]["B3"].value == "w"
    assert "SalesTable" in workbook["Data"].tables
    assert len(workbook["Data"]._charts) == 1


def test_add_table_chart_and_sheet(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    result = xlsx_engine.apply_xlsx(
        path,
        _ref(),
        [
            UpdateOperation(op=OpKind.ADD_SHEET, sheet="Summary"),
            UpdateOperation(
                op=OpKind.ADD_TABLE,
                sheet="Summary",
                range="A1:B3",
                rows=[["Region", "Sales"], ["KR", 1200], ["JP", 900]],
                title="RegionTable",
            ),
            UpdateOperation(
                op=OpKind.ADD_CHART,
                sheet="Summary",
                range="A1:B3",
                chart_type="bar",
                title="Sales by region",
                anchor="D2",
            ),
        ],
    )
    assert len(result.applied) == 3
    edited = tmp_path / "summary.xlsx"
    edited.write_bytes(result.data)
    workbook = load_workbook(str(edited))
    assert "Summary" in workbook.sheetnames
    assert "RegionTable" in workbook["Summary"].tables
    assert len(workbook["Summary"]._charts) == 1


def test_create_workbook_from_blocks(tmp_path):
    blocks = [
        ContentBlock(kind="heading", text="Q4 Report"),
        ContentBlock(kind="table", rows=[["Month", "Units"], ["Jan", 10], ["Feb", 12]], header_rows=1),
        ContentBlock(kind="paragraph", text="Generated by HD2Lab"),
    ]
    data = xlsx_engine.create_xlsx(blocks, metadata={"title": "Q4"})
    produced = tmp_path / "created.xlsx"
    produced.write_bytes(data)
    workbook = load_workbook(str(produced))
    sheet = workbook.active
    assert sheet["A1"].value == "Q4 Report"
    assert sheet["A2"].value == "Month" and sheet["B3"].value == 10
    assert workbook.properties.title == "Q4"


def test_encrypted_or_corrupt_workbook_raises(tmp_path):
    from app.knowledge.contracts import KnowledgeError, KnowledgeErrorCode

    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"PK\x03\x04broken")
    with pytest.raises(KnowledgeError) as excinfo:
        xlsx_engine.read_path(broken, _ref(), ReadRequest(document_id="kd_xlsxtest001"))
    assert excinfo.value.body.code is KnowledgeErrorCode.CORRUPT_CONTENT
