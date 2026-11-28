"""Real-format fixtures for the R21 knowledge engine tests.

Nothing here is mocked: every fixture is produced by the same library the engine reads, so a
passing test means the engine handled a genuinely compatible file.
"""

from __future__ import annotations

import io
from pathlib import Path


def png_bytes(width: int = 40, height: int = 30, color=(200, 60, 60)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_docx(path: Path, *, header_text: str = "HD2Lab Confidential") -> Path:
    from docx import Document
    from docx.shared import Pt

    document = Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = header_text
    document.add_heading("R21 Knowledge Engine", level=1)
    paragraph = document.add_paragraph("The document knowledge engine reads ")
    run = paragraph.add_run("structured citations")
    run.bold = True
    run.font.size = Pt(12)
    paragraph.add_run(" from every paragraph.")
    document.add_heading("Table Section", level=2)
    table = document.add_table(rows=2, cols=3)
    table.style = "Table Grid"
    for column, value in enumerate(["Item", "Qty", "Price"]):
        table.cell(0, column).text = value
    for column, value in enumerate(["Widget", "3", "1200"]):
        table.cell(1, column).text = value
    document.add_paragraph("A closing paragraph that must survive edits.")
    document.add_picture(io.BytesIO(png_bytes()))
    document.save(str(path))
    return path


def make_xlsx(path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Font
    from openpyxl.worksheet.table import Table, TableStyleInfo

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"], sheet["B1"], sheet["C1"] = "Month", "Units", "Revenue"
    for row, (month, units, revenue) in enumerate(
        [("Jan", 10, 1200), ("Feb", 12, 1440), ("Mar", 9, 1080)], start=2
    ):
        sheet.cell(row=row, column=1, value=month)
        sheet.cell(row=row, column=2, value=units)
        sheet.cell(row=row, column=3, value=revenue)
    sheet["B5"] = "=SUM(B2:B4)"
    sheet["C5"] = "=SUM(C2:C4)"
    sheet["A1"].font = Font(bold=True)
    sheet["C2"].number_format = "#,##0"
    sheet.merge_cells("E1:F1")
    sheet["E1"] = "Merged heading"
    table = Table(displayName="SalesTable", ref="A1:C4")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    chart = BarChart()
    chart.title = "Revenue by month"
    data = Reference(sheet, min_col=3, min_row=1, max_row=4)
    categories = Reference(sheet, min_col=1, min_row=2, max_row=4)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(categories)
    sheet.add_chart(chart, "H2")
    second = workbook.create_sheet("Notes")
    second["A1"] = "Second sheet content"
    workbook.save(str(path))
    return path


def make_pptx(path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    layout = presentation.slide_layouts[1]
    first = presentation.slides.add_slide(layout)
    first.shapes.title.text = "Q4 Review"
    body = first.placeholders[1]
    body.text = "Revenue grew"
    frame = body.text_frame
    frame.add_paragraph().text = "Costs fell"
    shape = first.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1))
    shape.table.cell(0, 0).text = "Region"
    shape.table.cell(0, 1).text = "Sales"
    shape.table.cell(1, 0).text = "KR"
    shape.table.cell(1, 1).text = "1200"
    first.notes_slide.notes_text_frame.text = "Speaker note for slide one"
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "Appendix"
    presentation.save(str(path))
    return path


def make_pdf(path: Path, pages: int = 3) -> Path:
    import fitz

    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 96), f"Page {index + 1} heading", fontsize=18)
        page.insert_text(
            (72, 130),
            f"This is body text on page {index + 1} describing the knowledge engine and its citations.",
            fontsize=11,
        )
        if index == 0:
            page.insert_text((72, 160), "Revenue reached 1200 units in January.", fontsize=11)
    document.save(str(path))
    document.close()
    return path


def make_encrypted_pdf(path: Path) -> Path:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 96), "secret", fontsize=12)
    document.save(
        str(path),
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw="user-pass",
        owner_pw="owner-pass",
    )
    document.close()
    return path
