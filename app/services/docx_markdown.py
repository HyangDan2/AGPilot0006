import base64
import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import docx
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional image conversion
    Image = None

HEADING_PATTERN = re.compile(r"^(?:Heading|제목)\s*([1-9])$", re.IGNORECASE)
R_EMBED_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
R_ID_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
A_BLIP_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
WP_DOCPR_TAG = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
W_TXBX_TAG = qn("w:txbxContent")
W_HYPERLINK_TAG = qn("w:hyperlink")
W_R_TAG = qn("w:r")
W_T_TAG = qn("w:t")
W_TAB_TAG = qn("w:tab")
W_BR_TAG = qn("w:br")
W_P_TAG = qn("w:p")
W_TBL_TAG = qn("w:tbl")
W_TR_TAG = qn("w:tr")
W_TC_TAG = qn("w:tc")
W_TCPR_TAG = qn("w:tcPr")
W_GRIDSPAN_TAG = qn("w:gridSpan")
W_VMERGE_TAG = qn("w:vMerge")
W_PPR_TAG = qn("w:pPr")
W_PSTYLE_TAG = qn("w:pStyle")
W_OUTLINELVL_TAG = qn("w:outlineLvl")
W_NUMPR_TAG = qn("w:numPr")
W_ILVL_TAG = qn("w:ilvl")
W_NUMID_TAG = qn("w:numId")
W_RPR_TAG = qn("w:rPr")
W_B_TAG = qn("w:b")
W_I_TAG = qn("w:i")
W_STRIKE_TAG = qn("w:strike")
W_RFONTS_TAG = qn("w:rFonts")
W_VAL_ATTR = qn("w:val")

WEB_SAFE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
}


@dataclass
class ExtractedFigure:
    figure_id: str
    asset_id: str
    filename: str
    ext: str
    mime_type: str
    caption: str
    section_id: Optional[str]
    context: str  # paragraph | table | textbox
    data: bytes
    converted_to_png: bool = False


@dataclass
class ExtractedSection:
    section_id: str
    parent_id: Optional[str]
    title: str
    level: int
    content: str
    figure_ids: List[str] = field(default_factory=list)
    table_count: int = 0


@dataclass
class DocxConversionResult:
    filename: str
    markdown: str
    sections: List[ExtractedSection]
    figures: List[ExtractedFigure]
    stats: Dict[str, int]


def _maybe_convert_image(raw: bytes, ext: str) -> Tuple[bytes, str, bool]:
    ext_norm = ext.lower().lstrip(".") or "png"
    if ext_norm == "jpeg":
        ext_norm = "jpg"
    if ext_norm in WEB_SAFE_EXTS or Image is None:
        return raw, ext_norm, False
    try:
        with Image.open(io.BytesIO(raw)) as img:
            out = io.BytesIO()
            img.convert("RGBA" if img.mode in ("P", "LA", "RGBA") else "RGB").save(out, format="PNG")
            return out.getvalue(), "png", True
    except Exception:
        return raw, ext_norm, False


def _load_numbering_formats(doc: docx.Document) -> Dict[str, Dict[int, str]]:
    """Map numId -> {ilvl: numFmt} (e.g. 'decimal', 'bullet') from numbering.xml."""
    result: Dict[str, Dict[int, str]] = {}
    try:
        numbering_part = doc.part.numbering_part
    except Exception:
        return result
    if numbering_part is None:
        return result
    root = numbering_part.element
    abstract_formats: Dict[str, Dict[int, str]] = {}
    for abs_el in root.findall(qn("w:abstractNum")):
        abs_id = abs_el.get(qn("w:abstractNumId"))
        if not abs_id:
            continue
        lvl_map: Dict[int, str] = {}
        for lvl in abs_el.findall(qn("w:lvl")):
            ilvl_str = lvl.get(W_ILVL_TAG)
            num_fmt_el = lvl.find(qn("w:numFmt"))
            if ilvl_str is not None and num_fmt_el is not None:
                lvl_map[int(ilvl_str)] = num_fmt_el.get(W_VAL_ATTR, "bullet")
        abstract_formats[abs_id] = lvl_map

    for num_el in root.findall(qn("w:num")):
        num_id = num_el.get(W_NUMID_TAG)
        abs_ref = num_el.find(qn("w:abstractNumId"))
        if num_id and abs_ref is not None:
            abs_id = abs_ref.get(W_VAL_ATTR)
            if abs_id in abstract_formats:
                result[num_id] = abstract_formats[abs_id]
    return result


def _is_flag_on(rpr, tag: str) -> bool:
    if rpr is None:
        return False
    el = rpr.find(tag)
    if el is None:
        return False
    val = el.get(W_VAL_ATTR)
    return val is None or val.lower() not in ("0", "false", "off", "none")


def _is_monospace_run(rpr) -> bool:
    if rpr is None:
        return False
    fonts = rpr.find(W_RFONTS_TAG)
    if fonts is None:
        return False
    for attr in (qn("w:ascii"), qn("w:hAnsi"), qn("w:cs")):
        val = (fonts.get(attr) or "").lower()
        if any(k in val for k in ("courier", "consolas", "menlo", "monaco", "mono", "d2coding")):
            return True
    return False


def _style_name_map(doc: docx.Document) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for st in doc.styles:
        if st.style_id and st.name:
            mapping[st.style_id] = st.name
    return mapping


def _detect_heading_level(p_el, style_map: Dict[str, str]) -> Optional[int]:
    ppr = p_el.find(W_PPR_TAG)
    if ppr is None:
        return None
    pstyle = ppr.find(W_PSTYLE_TAG)
    if pstyle is not None:
        sid = pstyle.get(W_VAL_ATTR, "")
        sname = style_map.get(sid, sid)
        m = HEADING_PATTERN.match(sname.strip())
        if m:
            return int(m.group(1))
        m_id = re.match(r"^(?:Heading|제목)([1-9])$", sid.strip(), re.IGNORECASE)
        if m_id:
            return int(m_id.group(1))
    outline = ppr.find(W_OUTLINELVL_TAG)
    if outline is not None:
        val = outline.get(W_VAL_ATTR)
        if val is not None and val.isdigit():
            lvl = int(val)
            if 0 <= lvl <= 8:
                return lvl + 1
    return None


def _detect_list_prefix(p_el, num_formats: Dict[str, Dict[int, str]], list_counters: Dict[Tuple[str, int], int]) -> str:
    ppr = p_el.find(W_PPR_TAG)
    if ppr is None:
        return ""
    numpr = ppr.find(W_NUMPR_TAG)
    if numpr is None:
        return ""
    ilvl_el = numpr.find(W_ILVL_TAG)
    numid_el = numpr.find(W_NUMID_TAG)
    ilvl = int(ilvl_el.get(W_VAL_ATTR, "0")) if ilvl_el is not None else 0
    num_id = numid_el.get(W_VAL_ATTR, "0") if numid_el is not None else "0"
    if num_id == "0":
        return ""
    fmt = num_formats.get(num_id, {}).get(ilvl, "bullet")
    indent = "  " * min(ilvl, 6)
    if fmt in ("decimal", "lowerLetter", "upperLetter", "lowerRoman", "upperRoman"):
        key = (num_id, ilvl)
        list_counters[key] = list_counters.get(key, 0) + 1
        return f"{indent}{list_counters[key]}. "
    return f"{indent}- "


def convert_docx(
    file_path: Path,
    *,
    filename: Optional[str] = None,
    table_mode: Literal["auto", "gfm", "html"] = "auto",
    image_mode: Literal["assets", "base64", "none"] = "assets",
    asset_prefix: str = "assets/",
) -> DocxConversionResult:
    """
    Deterministic, LLM-free complex DOCX -> structured Markdown converter.
    Preserves merged tables (colspan/rowspan), inline images in paragraphs/tables/textboxes,
    Korean/English headings, nested lists, inline formatting, and section<->figure mapping.
    """
    doc = docx.Document(str(file_path))
    display_name = filename or file_path.name
    style_map = _style_name_map(doc)
    num_formats = _load_numbering_formats(doc)
    list_counters: Dict[Tuple[str, int], int] = {}

    # 1. Pre-index image parts by relationship ID
    rel_images: Dict[str, Tuple[bytes, str, bool]] = {}
    converted_count = 0
    for rel in doc.part.rels.values():
        if "image" in (rel.reltype or "") or "image" in (rel.target_ref or ""):
            try:
                part = rel.target_part
                ext = Path(str(part.partname)).suffix.lstrip(".").lower() or "png"
                img_bytes, final_ext, converted = _maybe_convert_image(part.blob, ext)
                rel_images[rel.rId] = (img_bytes, final_ext, converted)
                if converted:
                    converted_count += 1
            except Exception:
                continue

    # Hyperlink relationships
    rel_links: Dict[str, str] = {}
    for rel in doc.part.rels.values():
        if "hyperlink" in (rel.reltype or "") and getattr(rel, "_target", None):
            rel_links[rel.rId] = str(rel._target)

    figures: List[ExtractedFigure] = []
    rid_to_figure: Dict[str, ExtractedFigure] = {}
    figure_counter = 0

    sections: List[ExtractedSection] = []
    current_title = "Overview"
    current_level = 1
    current_content_lines: List[str] = []
    current_figure_ids: List[str] = []
    table_count_in_sec = 0
    section_idx = 1
    parent_stack: List[Tuple[int, str]] = []
    has_encountered_first_heading = False
    md_lines: List[str] = [f"# {Path(display_name).stem}\n"]

    total_tables = 0
    merged_tables = 0

    def active_section_id() -> str:
        return f"sec_{section_idx:03d}"

    def register_figure(r_id: str, caption_hint: str, context: str) -> Optional[ExtractedFigure]:
        nonlocal figure_counter
        if r_id not in rel_images:
            return None
        if r_id in rid_to_figure:
            fig = rid_to_figure[r_id]
        else:
            figure_counter += 1
            img_bytes, ext, converted = rel_images[r_id]
            fig_id = f"fig_d_{figure_counter:03d}"
            asset_id = f"asset_d_{figure_counter:03d}"
            fname = f"{fig_id}.{ext}"
            caption = caption_hint.strip() or f"Image {figure_counter}"
            fig = ExtractedFigure(
                figure_id=fig_id,
                asset_id=asset_id,
                filename=fname,
                ext=ext,
                mime_type=MIME_BY_EXT.get(ext, "application/octet-stream"),
                caption=caption,
                section_id=active_section_id(),
                context=context,
                data=img_bytes,
                converted_to_png=converted,
            )
            figures.append(fig)
            rid_to_figure[r_id] = fig
        if fig.figure_id not in current_figure_ids:
            current_figure_ids.append(fig.figure_id)
        return fig

    def render_figure_md(fig: ExtractedFigure) -> str:
        if image_mode == "none":
            return f"[{fig.caption}]"
        safe_alt = fig.caption.replace("[", "").replace("]", "").replace("\n", " ").strip()
        if image_mode == "base64":
            b64 = base64.b64encode(fig.data).decode("ascii")
            return f"![{safe_alt}](data:{fig.mime_type};base64,{b64})"
        return f"![{safe_alt}]({asset_prefix}{fig.filename})"

    def render_figure_html(fig: ExtractedFigure) -> str:
        if image_mode == "none":
            return f"[{html.escape(fig.caption)}]"
        safe_alt = html.escape(fig.caption, quote=True)
        if image_mode == "base64":
            b64 = base64.b64encode(fig.data).decode("ascii")
            src = f"data:{fig.mime_type};base64,{b64}"
        else:
            src = f"{asset_prefix}{fig.filename}"
        return f'<img src="{html.escape(src, quote=True)}" alt="{safe_alt}" />'

    def extract_run_content(r_el, context: str, for_html: bool = False) -> str:
        parts: List[str] = []
        rpr = r_el.find(W_RPR_TAG)
        bold = _is_flag_on(rpr, W_B_TAG)
        italic = _is_flag_on(rpr, W_I_TAG)
        strike = _is_flag_on(rpr, W_STRIKE_TAG)
        mono = _is_monospace_run(rpr)

        text_chunks: List[str] = []
        inline_media: List[str] = []

        for node in r_el.iter():
            tag = node.tag
            if tag == W_T_TAG and node.text:
                text_chunks.append(node.text)
            elif tag == W_TAB_TAG:
                text_chunks.append("    ")
            elif tag == W_BR_TAG:
                text_chunks.append("<br>" if for_html else "\n")
            elif tag == A_BLIP_TAG:
                r_id = node.get(R_EMBED_ATTR)
                if r_id:
                    caption_hint = ""
                    parent = node.getparent()
                    while parent is not None and parent.tag != W_R_TAG:
                        docpr = parent.find(f".//{WP_DOCPR_TAG}")
                        if docpr is not None:
                            caption_hint = (
                                docpr.get("descr") or docpr.get("title") or docpr.get("name") or ""
                            ).strip()
                            if caption_hint:
                                break
                        parent = parent.getparent()
                    fig = register_figure(r_id, caption_hint, context)
                    if fig is not None:
                        inline_media.append(render_figure_html(fig) if for_html else render_figure_md(fig))

        raw_text = "".join(text_chunks)
        if raw_text:
            stripped = raw_text.strip()
            if stripped:
                lead = raw_text[: len(raw_text) - len(raw_text.lstrip())]
                trail = raw_text[len(raw_text.rstrip()) :]
                formatted = html.escape(stripped) if for_html else stripped
                if for_html:
                    if mono:
                        formatted = f"<code>{formatted}</code>"
                    if bold:
                        formatted = f"<strong>{formatted}</strong>"
                    if italic:
                        formatted = f"<em>{formatted}</em>"
                    if strike:
                        formatted = f"<del>{formatted}</del>"
                else:
                    if mono and "`" not in formatted:
                        formatted = f"`{formatted}`"
                    if bold:
                        formatted = f"**{formatted}**"
                    if italic:
                        formatted = f"*{formatted}*"
                    if strike:
                        formatted = f"~~{formatted}~~"
                parts.append(f"{lead}{formatted}{trail}")
            else:
                parts.append(raw_text)

        if inline_media:
            parts.extend(inline_media)
        return "".join(parts)

    def render_paragraph(p_el, context: str = "paragraph", for_html: bool = False) -> Tuple[str, List[str]]:
        """Render paragraph text+images and also collect any nested textbox paragraphs."""
        pieces: List[str] = []
        textbox_lines: List[str] = []

        # Extract textbox contents separately so callouts/shapes are not lost
        for txbx in p_el.findall(f".//{W_TXBX_TAG}"):
            for inner_p in txbx.findall(W_P_TAG):
                t_line, _ = render_paragraph(inner_p, context="textbox", for_html=for_html)
                if t_line:
                    textbox_lines.append(t_line)

        for child in p_el:
            if child.tag == W_R_TAG:
                # Skip runs that only wrap textbox content already extracted above
                if child.find(f".//{W_TXBX_TAG}") is not None:
                    continue
                pieces.append(extract_run_content(child, context=context, for_html=for_html))
            elif child.tag == W_HYPERLINK_TAG:
                r_id = child.get(R_ID_ATTR)
                href = rel_links.get(r_id, "") if r_id else ""
                link_text = "".join(
                    extract_run_content(r, context=context, for_html=for_html) for r in child.findall(W_R_TAG)
                ).strip()
                if href and link_text:
                    if for_html:
                        pieces.append(f'<a href="{html.escape(href, quote=True)}">{link_text}</a>')
                    else:
                        pieces.append(f"[{link_text}]({href})")
                elif link_text:
                    pieces.append(link_text)

        text = "".join(pieces).strip()
        return text, textbox_lines

    def render_table(tbl_el) -> str:
        nonlocal total_tables, merged_tables
        total_tables += 1

        rows_raw: List[List[Dict[str, Any]]] = []
        has_merge = False
        has_nested_table = False

        for tr in tbl_el.findall(W_TR_TAG):
            row_cells: List[Dict[str, Any]] = []
            col_cursor = 0
            for tc in tr.findall(W_TC_TAG):
                tcpr = tc.find(W_TCPR_TAG)
                colspan = 1
                vmerge_state: Optional[str] = None
                if tcpr is not None:
                    gs = tcpr.find(W_GRIDSPAN_TAG)
                    if gs is not None and (gs.get(W_VAL_ATTR) or "").isdigit():
                        colspan = max(1, int(gs.get(W_VAL_ATTR)))
                    vm = tcpr.find(W_VMERGE_TAG)
                    if vm is not None:
                        vmerge_state = vm.get(W_VAL_ATTR, "continue")
                if colspan > 1 or vmerge_state is not None:
                    has_merge = True
                if tc.find(W_TBL_TAG) is not None:
                    has_nested_table = True

                row_cells.append(
                    {
                        "tc": tc,
                        "col": col_cursor,
                        "colspan": colspan,
                        "vmerge": vmerge_state,
                        "rowspan": 1,
                        "skip": vmerge_state == "continue",
                    }
                )
                col_cursor += colspan
            if row_cells:
                rows_raw.append(row_cells)

        if not rows_raw:
            return ""

        if has_merge or has_nested_table:
            merged_tables += 1

        # Compute rowspan for vertically merged cells
        active_vmerge: Dict[int, Dict[str, Any]] = {}
        for row in rows_raw:
            for cell in row:
                c_idx = cell["col"]
                if cell["vmerge"] == "restart":
                    active_vmerge[c_idx] = cell
                elif cell["vmerge"] == "continue":
                    if c_idx in active_vmerge:
                        active_vmerge[c_idx]["rowspan"] += 1
                else:
                    active_vmerge.pop(c_idx, None)

        use_html = table_mode == "html" or (table_mode == "auto" and (has_merge or has_nested_table))

        def cell_content(tc, for_html: bool) -> str:
            lines: List[str] = []
            for child in tc:
                if child.tag == W_P_TAG:
                    line, tb_lines = render_paragraph(child, context="table", for_html=for_html)
                    if line:
                        lines.append(line)
                    lines.extend(tb_lines)
                elif child.tag == W_TBL_TAG:
                    nested = render_table(child)
                    if nested:
                        lines.append(nested)
            if for_html:
                return "<br>".join(lines)
            joined = " <br> ".join(lines)
            return joined.replace("|", "\\|").replace("\n", " ").strip()

        if use_html:
            html_lines = ["<table>"]
            for r_idx, row in enumerate(rows_raw):
                html_lines.append("  <tr>")
                tag = "th" if r_idx == 0 else "td"
                for cell in row:
                    if cell["skip"]:
                        continue
                    attrs = ""
                    if cell["colspan"] > 1:
                        attrs += f' colspan="{cell["colspan"]}"'
                    if cell["rowspan"] > 1:
                        attrs += f' rowspan="{cell["rowspan"]}"'
                    inner = cell_content(cell["tc"], for_html=True)
                    html_lines.append(f"    <{tag}{attrs}>{inner}</{tag}>")
                html_lines.append("  </tr>")
            html_lines.append("</table>")
            return "\n".join(html_lines)

        # Simple GFM table
        max_cols = max(sum(c["colspan"] for c in r) for r in rows_raw)
        gfm_lines: List[str] = []
        for r_idx, row in enumerate(rows_raw):
            vals: List[str] = []
            for cell in row:
                val = "" if cell["skip"] else cell_content(cell["tc"], for_html=False)
                vals.append(val)
                for _ in range(cell["colspan"] - 1):
                    vals.append("")
            while len(vals) < max_cols:
                vals.append("")
            gfm_lines.append("| " + " | ".join(vals[:max_cols]) + " |")
            if r_idx == 0:
                gfm_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        return "\n".join(gfm_lines)

    def flush_section() -> None:
        nonlocal section_idx, current_content_lines, current_figure_ids, table_count_in_sec
        content_str = "\n\n".join(line for line in current_content_lines if line.strip()).strip()
        if not has_encountered_first_heading and not content_str and not current_figure_ids:
            return
        while parent_stack and parent_stack[-1][0] >= current_level:
            parent_stack.pop()
        parent_id = parent_stack[-1][1] if parent_stack else None
        sec_id = f"sec_{section_idx:03d}"
        sections.append(
            ExtractedSection(
                section_id=sec_id,
                parent_id=parent_id,
                title=current_title,
                level=current_level,
                content=content_str,
                figure_ids=list(current_figure_ids),
                table_count=table_count_in_sec,
            )
        )
        parent_stack.append((current_level, sec_id))
        section_idx += 1
        current_content_lines = []
        current_figure_ids = []
        table_count_in_sec = 0

    # 2. Traverse body elements in exact document order
    for child in doc.element.body:
        if isinstance(child, CT_P):
            h_lvl = _detect_heading_level(child, style_map)
            p_text, tb_lines = render_paragraph(child, context="paragraph", for_html=False)
            if h_lvl is not None:
                flush_section()
                has_encountered_first_heading = True
                current_level = h_lvl
                clean_title = re.sub(r"[*_~`]+", "", p_text).strip() or f"Heading {h_lvl}"
                current_title = clean_title
                md_lines.append(f"\n{'#' * min(h_lvl + 1, 6)} {clean_title}\n")
                for tb in tb_lines:
                    current_content_lines.append(tb)
                    md_lines.append(f"> {tb}\n")
            else:
                prefix = _detect_list_prefix(child, num_formats, list_counters)
                if p_text:
                    line = f"{prefix}{p_text}"
                    current_content_lines.append(line)
                    md_lines.append(line + ("\n" if prefix else "\n\n"))
                for tb in tb_lines:
                    current_content_lines.append(tb)
                    md_lines.append(f"> {tb}\n\n")
        elif isinstance(child, CT_Tbl):
            table_count_in_sec += 1
            tbl_md = render_table(child)
            if tbl_md:
                current_content_lines.append(tbl_md)
                md_lines.append("\n" + tbl_md + "\n\n")

    flush_section()

    # Include any unreferenced embedded images at the end so nothing is silently dropped
    unreferenced = [r_id for r_id in rel_images if r_id not in rid_to_figure]
    if unreferenced:
        for r_id in unreferenced:
            fig = register_figure(r_id, "", "embedded")
            if fig is not None:
                rendered = render_figure_md(fig)
                md_lines.append(f"\n{rendered}\n")
                if sections:
                    sections[-1].figure_ids.append(fig.figure_id)

    markdown_text = re.sub(r"\n{3,}", "\n\n", "".join(md_lines)).strip() + "\n"
    return DocxConversionResult(
        filename=display_name,
        markdown=markdown_text,
        sections=sections,
        figures=figures,
        stats={
            "sections": len(sections),
            "tables": total_tables,
            "merged_tables": merged_tables,
            "images": len(figures),
            "converted_images": converted_count,
        },
    )


def build_markdown_zip(result: DocxConversionResult, asset_prefix: str = "assets/") -> bytes:
    """Package document.md + extracted image assets into an in-memory ZIP archive."""
    buf = io.BytesIO()
    prefix = asset_prefix.strip("/")
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("document.md", result.markdown.encode("utf-8"))
        for fig in result.figures:
            path_in_zip = f"{prefix}/{fig.filename}" if prefix else fig.filename
            zf.writestr(path_in_zip, fig.data)
    return buf.getvalue()
