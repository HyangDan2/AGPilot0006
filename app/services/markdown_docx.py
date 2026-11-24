"""Deterministic Markdown -> DOCX converter using python-docx."""
from io import BytesIO
import re
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


def _set_cell_shading(cell, fill_hex: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill_hex)
    tc_pr.append(shd)


def _add_inline_runs(paragraph, text: str) -> None:
    """Parse basic inline **bold**, *italic*, and `code` spans."""
    token_re = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)")
    pos = 0
    for m in token_re.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos : m.start()])
        tok = m.group(0)
        if tok.startswith("**") and tok.endswith("**"):
            run = paragraph.add_run(tok[2:-2])
            run.bold = True
        elif tok.startswith("`") and tok.endswith("`"):
            run = paragraph.add_run(tok[1:-1])
            run.font.name = "Courier New"
            run.font.size = Pt(9.5)
        elif tok.startswith("*") and tok.endswith("*"):
            run = paragraph.add_run(tok[1:-1])
            run.italic = True
        else:
            paragraph.add_run(tok)
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _parse_table_row(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    if "|" not in s:
        return False
    cells = _parse_table_row(s)
    return bool(cells) and all(bool(re.match(r"^:?-{3,}:?$", c.strip())) for c in cells)


def convert_markdown_to_docx(markdown_text: str, title: Optional[str] = None) -> Tuple[bytes, Dict[str, int]]:
    doc = Document()
    stats = {"headings": 0, "paragraphs": 0, "tables": 0, "code_blocks": 0, "list_items": 0}

    if title and title.strip():
        doc.add_heading(title.strip(), level=0)
        stats["headings"] += 1

    lines = (markdown_text or "").replace("\r\n", "\n").split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            code_lines: List[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(12)
            run = p.add_run("\n".join(code_lines))
            run.font.name = "Courier New"
            run.font.size = Pt(9)
            stats["code_blocks"] += 1
            continue

        # Heading
        hm = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if hm:
            level = min(4, len(hm.group(1)))
            doc.add_heading(hm.group(2).strip(), level=level)
            stats["headings"] += 1
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^([-*_])\1{2,}$", stripped):
            p = doc.add_paragraph("─" * 40)
            if p.runs:
                p.runs[0].font.color.rgb = RGBColor(0xA0, 0x9A, 0xB8)
            i += 1
            continue

        # GFM Table
        if "|" in stripped and i + 1 < n and _is_table_separator(lines[i + 1]):
            headers = _parse_table_row(stripped)
            i += 2
            rows: List[List[str]] = []
            while i < n and "|" in lines[i].strip() and lines[i].strip():
                rows.append(_parse_table_row(lines[i]))
                i += 1
            col_count = max(len(headers), max((len(r) for r in rows), default=0))
            if col_count > 0:
                table = doc.add_table(rows=1 + len(rows), cols=col_count)
                table.style = "Table Grid"
                for c_idx in range(col_count):
                    cell = table.rows[0].cells[c_idx]
                    cell.text = ""
                    p = cell.paragraphs[0]
                    run = p.add_run(headers[c_idx] if c_idx < len(headers) else "")
                    run.bold = True
                    _set_cell_shading(cell, "F1ECFE")
                for r_idx, row_vals in enumerate(rows):
                    for c_idx in range(col_count):
                        cell = table.rows[r_idx + 1].cells[c_idx]
                        cell.text = ""
                        _add_inline_runs(cell.paragraphs[0], row_vals[c_idx] if c_idx < len(row_vals) else "")
                stats["tables"] += 1
            continue

        # Blockquote
        if stripped.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(16)
            _add_inline_runs(p, stripped[2:].strip())
            stats["paragraphs"] += 1
            i += 1
            continue

        # Bullet list
        bm = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bm:
            p = doc.add_paragraph(style="List Bullet")
            _add_inline_runs(p, bm.group(1).strip())
            stats["list_items"] += 1
            i += 1
            continue

        # Numbered list
        nm = re.match(r"^\d+\.\s+(.*)$", stripped)
        if nm:
            p = doc.add_paragraph(style="List Number")
            _add_inline_runs(p, nm.group(1).strip())
            stats["list_items"] += 1
            i += 1
            continue

        # Normal paragraph (join consecutive plain lines)
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith(("#", "```", "> ", "- ", "* ", "+ "))
                or re.match(r"^\d+\.\s+", nxt)
                or re.match(r"^([-*_])\1{2,}$", nxt)
                or ("|" in nxt and i + 1 < n and _is_table_separator(lines[i + 1]))
            ):
                break
            para_lines.append(nxt)
            i += 1
        p = doc.add_paragraph()
        _add_inline_runs(p, " ".join(para_lines))
        stats["paragraphs"] += 1

    buf = BytesIO()
    doc.save(buf)
    raw = buf.getvalue()
    stats["size_bytes"] = len(raw)
    return raw, stats
