"""XLSX reader / writer (WP-05).

Read side distinguishes a stored formula from its cached value (openpyxl's two modes), reports
workbook structure (sheets, dimensions, merged cells, tables, charts, named ranges, number
formats), and windows large ranges by row so a 50k-row sheet never has to be materialised at
once.

Write side sets cells, ranges, formulas, tables and charts and saves atomically through the
store. A formula written by openpyxl has no cached result until the file is opened by Excel or
LibreOffice; the engine says so explicitly (`formula_recalc_required`) instead of pretending a
value exists.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.knowledge.contracts import (
    ContentBlock,
    DocNode,
    DocumentRef,
    KnowledgeError,
    KnowledgeErrorCode,
    Locator,
    LocatorKind,
    NodeKind,
    ReadMode,
    ReadRequest,
    ReadResponse,
    ReadStats,
    TableData,
    make_citation,
)
from app.knowledge.engines.base import ApplyResult

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CELL_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_RANGE_RE = re.compile(r"^([A-Z]{1,3}[1-9][0-9]{0,6})(?::([A-Z]{1,3}[1-9][0-9]{0,6}))?$")


def _load(path: Path, *, data_only: bool):
    from openpyxl import load_workbook

    try:
        return load_workbook(str(path), data_only=data_only, read_only=False)
    except Exception as exc:
        name = type(exc).__name__
        if "encrypted" in str(exc).lower() or "password" in str(exc).lower():
            raise KnowledgeError(KnowledgeErrorCode.ENCRYPTED_DOCUMENT, "workbook is encrypted") from exc
        raise KnowledgeError(
            KnowledgeErrorCode.CORRUPT_CONTENT,
            "xlsx workbook could not be opened",
            detail={"reason": name},
        ) from exc


def _parse_range(value: str, sheet) -> Tuple[int, int, int, int]:
    match = _RANGE_RE.match((value or "").replace("$", "").upper())
    if not match:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, f"invalid range {value!r}")
    from openpyxl.utils import column_index_from_string, range_boundaries

    min_col, min_row, max_col, max_row = range_boundaries(
        f"{match.group(1)}:{match.group(2)}" if match.group(2) else match.group(1)
    )
    return min_row, min_col, max_row, max_col


def _cell_ref(row: int, column: int) -> str:
    from openpyxl.utils import get_column_letter

    return f"{get_column_letter(column)}{row}"


def _defined_names(workbook) -> List[Dict[str, Any]]:
    names: List[Dict[str, Any]] = []
    try:
        entries = workbook.defined_names.items()
    except AttributeError:  # pragma: no cover - very old openpyxl
        entries = []
    for name, defined in entries:
        try:
            destinations = list(defined.destinations)
        except Exception:
            destinations = []
        names.append({"name": name, "ranges": [f"{sheet}!{ref}" for sheet, ref in destinations]})
    return names


def _chart_info(chart) -> Dict[str, Any]:
    return {
        "type": type(chart).__name__,
        "title": _chart_title(chart),
        "series": [getattr(series, "tx", None).strRef.f if getattr(getattr(series, "tx", None), "strRef", None) else None for series in getattr(chart, "series", [])],
    }


def _chart_title(chart) -> str:
    try:
        title = chart.title
        if title is None:
            return ""
        text = ""
        for paragraph in title.tx.rich.p:
            for run in paragraph.r:
                text += run.t or ""
        return text
    except Exception:
        return ""


def read_xlsx(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    workbook = _load(path, data_only=False)
    cached = _load(path, data_only=True)
    warnings: List[str] = []
    nodes: List[DocNode] = []
    citations: List = []
    total_named = _defined_names(workbook)
    sheet_names = list(workbook.sheetnames)
    if request.mode is ReadMode.SHEET and request.sheet:
        if request.sheet not in sheet_names:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_REQUEST, f"sheet {request.sheet!r} not found",
                detail={"sheets": sheet_names},
            )
        targets = [request.sheet]
    else:
        targets = sheet_names

    max_cells = int(getattr(settings, "KNOWLEDGE_MAX_CELLS", 50000) or 50000)
    stats = ReadStats(sheets=len(sheet_names))
    truncated = False
    next_cursor = None

    for sheet_name in targets:
        sheet = workbook[sheet_name]
        cached_sheet = cached[sheet_name] if sheet_name in cached.sheetnames else None
        dimension = sheet.dimensions or "A1"
        merged = [str(item) for item in sheet.merged_cells.ranges]
        tables = [
            {"name": name, "range": sheet.tables[name].ref}
            for name in list(getattr(sheet, "tables", {}) or {})
        ]
        charts = [_chart_info(chart) for chart in getattr(sheet, "_charts", [])]
        nodes.append(
            DocNode(
                node_id=f"sheet-{sheet_name}-meta",
                kind=NodeKind.METADATA,
                text=f"sheet {sheet_name} {dimension}",
                locator=Locator(kind=LocatorKind.SHEET, sheet=sheet_name),
                meta={
                    "sheet": sheet_name,
                    "dimensions": dimension,
                    "merged_cells": merged,
                    "tables": tables,
                    "charts": charts,
                    "named_ranges": [entry for entry in total_named if any(sheet_name == item.split("!")[0] for item in entry["ranges"])],
                    "max_row": sheet.max_row,
                    "max_column": sheet.max_column,
                },
            )
        )
        if request.mode is ReadMode.OUTLINE:
            continue

        start_row = 1
        if request.cursor and request.cursor.startswith(f"sheet:{sheet_name}:row:"):
            start_row = max(1, int(request.cursor.rsplit(":", 1)[-1]))
        if request.cell_range:
            min_row, min_col, max_row, max_col = _parse_range(request.cell_range, sheet)
        else:
            min_row, min_col = 1, 1
            max_row = min(sheet.max_row or 1, start_row + request.max_rows - 1)
            max_col = min(sheet.max_column or 1, 200)
        max_row = min(max_row, start_row + request.max_rows - 1)
        row_window = max_row - min_row + 1
        col_window = max_col - min_col + 1
        if row_window * col_window > max_cells:
            max_row = min_row + max(1, max_cells // max(1, col_window)) - 1
        rows: List[List[str]] = []
        formulas: List[DocNode] = []
        for row_index in range(min_row, max_row + 1):
            values: List[str] = []
            for column_index in range(min_col, max_col + 1):
                cell = sheet.cell(row=row_index, column=column_index)
                raw = cell.value
                if raw is None:
                    values.append("")
                    continue
                if isinstance(raw, str) and raw.startswith("="):
                    cached_cell = (
                        cached_sheet.cell(row=row_index, column=column_index)
                        if cached_sheet is not None
                        else None
                    )
                    cached_value = cached_cell.value if cached_cell is not None else None
                    values.append(f"{raw} -> {cached_value if cached_value is not None else '∅'}")
                    formulas.append(
                        DocNode(
                            node_id=f"f-{sheet_name}-{_cell_ref(row_index, column_index)}",
                            kind=NodeKind.FORMULA,
                            text=raw,
                            locator=Locator(
                                kind=LocatorKind.CELL,
                                sheet=sheet_name,
                                cell=_cell_ref(row_index, column_index),
                            ),
                            meta={
                                "formula": raw,
                                "cached_value": cached_value,
                                "cached_available": cached_value is not None,
                                "number_format": cell.number_format,
                            },
                        )
                    )
                    continue
                values.append(str(raw))
            rows.append(values)
        if max_row < (sheet.max_row or 1) and not request.cell_range:
            truncated = True
            next_cursor = f"sheet:{sheet_name}:row:{max_row + 1}"
        nodes.append(
            DocNode(
                node_id=f"sheet-{sheet_name}-table",
                kind=NodeKind.TABLE,
                text=f"sheet {sheet_name} rows {min_row}-{max_row}",
                locator=Locator(
                    kind=LocatorKind.RANGE,
                    sheet=sheet_name,
                    range=f"{_cell_ref(min_row, min_col)}:{_cell_ref(max_row, max_col)}",
                ),
                table=TableData(
                    rows=rows,
                    header_rows=1,
                    source_range=f"{sheet_name}!{_cell_ref(min_row, min_col)}:{_cell_ref(max_row, max_col)}",
                    truncated=max_row < (sheet.max_row or 1),
                ),
            )
        )
        nodes.extend(formulas)
        stats.tables += 1
        stats.chars += sum(len("".join(row)) for row in rows)

    text_sample = "\n".join(node.text for node in nodes if node.kind is NodeKind.TABLE)[:600]
    if text_sample:
        citations.append(
            make_citation(ref, Locator(kind=LocatorKind.SHEET, sheet=targets[0]), text_sample)
        )
    return ReadResponse(
        document=ref,
        mode=request.mode,
        nodes=nodes,
        citations=citations,
        truncated=truncated,
        next_cursor=next_cursor,
        warnings=warnings,
        stats=stats,
    )


def create_xlsx(
    blocks: List[ContentBlock],
    *,
    template: Optional[Path] = None,
    metadata: Optional[Dict[str, Any]] = None,
    sheet_name: str = "Sheet1",
) -> bytes:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font
    from openpyxl.worksheet.table import Table as XLTable, TableStyleInfo

    workbook = load_workbook(str(template)) if template else Workbook()
    if template:
        sheet = workbook[workbook.sheetnames[0]]
    else:
        sheet = workbook.active
        sheet.title = sheet_name[:31]
    if metadata:
        _apply_metadata(workbook, metadata)

    row_cursor = 1
    for block in blocks:
        if block.kind.value == "heading":
            sheet.cell(row=row_cursor, column=1, value=block.text).font = Font(bold=True, size=14)
            row_cursor += 1
        elif block.kind.value == "paragraph":
            sheet.cell(row=row_cursor, column=1, value=block.text)
            row_cursor += 1
        elif block.kind.value == "bullets":
            for bullet in block.bullets:
                sheet.cell(row=row_cursor, column=1, value=bullet)
                row_cursor += 1
        elif block.kind.value == "table":
            if not block.rows:
                continue
            start = row_cursor
            for row in block.rows:
                for column_index, value in enumerate(row, start=1):
                    sheet.cell(row=row_cursor, column=column_index, value=value)
                row_cursor += 1
            if block.rows:
                columns = max(len(row) for row in block.rows)
                if columns and len(block.rows) > 1:
                    ref = f"A{start}:{_cell_ref(row_cursor - 1, columns)}"
                    table = XLTable(displayName=f"Table{start}", ref=ref)
                    table.tableStyleInfo = TableStyleInfo(
                        name="TableStyleMedium2", showRowStripes=True
                    )
                    sheet.add_table(table)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _apply_metadata(workbook, metadata: Dict[str, Any]) -> None:
    properties = workbook.properties
    mapping = {
        "title": "title",
        "author": "creator",
        "creator": "creator",
        "subject": "subject",
        "keywords": "keywords",
        "description": "description",
    }
    for key, attribute in mapping.items():
        if key in metadata and metadata[key] is not None:
            setattr(properties, attribute, str(metadata[key]))


def _ensure_sheet(workbook, name: str):
    if name in workbook.sheetnames:
        return workbook[name]
    return workbook.create_sheet(title=name[:31])


def apply_xlsx(path: Path, ref: DocumentRef, operations: List[Any]) -> ApplyResult:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.worksheet.table import Table as XLTable, TableStyleInfo

    workbook = _load(path, data_only=False)
    applied: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []
    diff: List[str] = []

    for operation in operations:
        kind = operation.op.value
        if kind == "set_cell":
            sheet = workbook[operation.sheet] if operation.sheet in workbook.sheetnames else None
            if sheet is None:
                skipped.append(f"set_cell:sheet_not_found:{operation.sheet}")
                continue
            if not operation.cell or not _CELL_RE.match(operation.cell.replace("$", "").upper()):
                skipped.append(f"set_cell:invalid_cell:{operation.cell}")
                continue
            value = operation.formula or operation.value
            if operation.formula is not None or (
                isinstance(value, str) and value.startswith("=")
            ):
                warnings.append("formula_recalc_required")
            sheet[operation.cell] = value
            applied.append(f"set_cell:{operation.sheet}!{operation.cell}")
            diff.append(f"{operation.sheet}!{operation.cell} = {value!r}")
        elif kind == "set_range":
            sheet = workbook[operation.sheet] if operation.sheet in workbook.sheetnames else None
            if sheet is None or not operation.cell_range:
                skipped.append("set_range:sheet_or_range_missing")
                continue
            min_row, min_col, _, _ = _parse_range(operation.cell_range, sheet)
            for row_offset, row in enumerate(operation.rows):
                for column_offset, value in enumerate(row):
                    sheet.cell(
                        row=min_row + row_offset, column=min_col + column_offset, value=value
                    )
            applied.append(f"set_range:{operation.sheet}!{operation.cell_range}")
            diff.append(f"{operation.sheet}!{operation.cell_range} <- {len(operation.rows)} row(s)")
        elif kind == "add_sheet":
            if operation.sheet in workbook.sheetnames:
                skipped.append(f"add_sheet:exists:{operation.sheet}")
            else:
                workbook.create_sheet(title=operation.sheet[:31])
                applied.append(f"add_sheet:{operation.sheet}")
                diff.append(f"added sheet {operation.sheet}")
        elif kind == "add_table":
            sheet = workbook[operation.sheet] if operation.sheet in workbook.sheetnames else None
            if sheet is None or not operation.cell_range or not operation.rows:
                skipped.append("add_table:sheet_range_or_rows_missing")
                continue
            min_row, min_col, _, _ = _parse_range(operation.cell_range, sheet)
            for row_offset, row in enumerate(operation.rows):
                for column_offset, value in enumerate(row):
                    sheet.cell(row=min_row + row_offset, column=min_col + column_offset, value=value)
            columns = max(len(row) for row in operation.rows)
            end = _cell_ref(min_row + len(operation.rows) - 1, min_col + columns - 1)
            start = _cell_ref(min_row, min_col)
            name = operation.title or f"Table{min_row}"
            name = re.sub(r"[^A-Za-z0-9_]", "_", name)[:240]
            try:
                table = XLTable(displayName=name, ref=f"{start}:{end}")
                table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
                sheet.add_table(table)
                applied.append(f"add_table:{operation.sheet}!{start}:{end}")
                diff.append(f"added table {name} {start}:{end}")
            except Exception as exc:
                skipped.append(f"add_table:failed:{type(exc).__name__}")
        elif kind == "add_chart":
            sheet = workbook[operation.sheet] if operation.sheet in workbook.sheetnames else None
            if sheet is None:
                skipped.append("add_chart:sheet_not_found")
                continue
            if not operation.cell_range:
                skipped.append("add_chart:range_missing")
                continue
            min_row, min_col, max_row, max_col = _parse_range(operation.cell_range, sheet)
            chart_type = (operation.chart_type or "bar").lower()
            chart = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(
                chart_type, BarChart
            )()
            chart.title = operation.title or "Chart"
            data = Reference(sheet, min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)
            chart.add_data(data, titles_from_data=min_row < max_row)
            sheet.add_chart(chart, operation.anchor or f"E{min_row}")
            applied.append(f"add_chart:{operation.sheet}:{chart_type}")
            diff.append(f"added {chart_type} chart on {operation.sheet}")
        elif kind == "set_metadata":
            _apply_metadata(workbook, operation.metadata)
            applied.append("set_metadata")
            diff.append("updated workbook properties")
        else:
            skipped.append(f"{kind}:unsupported_for_xlsx")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return ApplyResult(buffer.getvalue(), applied, skipped, warnings, diff)


def read_path(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    return read_xlsx(path, ref, request)
