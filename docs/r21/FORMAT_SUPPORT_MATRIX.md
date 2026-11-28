# R21 format support matrix — actual engine depth

Generated from `KnowledgeService.manifest()` and verified by the R21 tests. "Read" and "Write"
below mean the engine returns the listed structures, not that it renders a visual replica.

| Family | MIME | Read | Write | Citations | Limits / notes |
| --- | --- | --- | --- | --- | --- |
| PDF | `application/pdf` | page text, block structure (heading heuristic by font size/weight), tables (`page.find_tables`), image metadata (xref/size), outline (bookmarks or first line) | — (read-only; documents are generated as DOCX/XLSX/PPTX) | page (1-based), document | 500 pages default; `max_chars` window + `page:N` cursor; encrypted → `encrypted_document`; damaged → `corrupt_content`; page without text layer → warning `page_N_no_text_layer` + vision/OCR hook |
| DOCX | `…officedocument.wordprocessingml.document` | headings (style/outlineLvl), paragraphs, runs (bold/italic/underline/size/font), tables (merged span count), inline images (rId, dimensions, bytes), headers/footers, sections (size/orientation/margins), styles | create (heading/paragraph/bullets/table/image/page break), edit: replace_text, insert/delete paragraph, add table, insert/replace image, set metadata | paragraph (1-based body index), section (heading path), document | template-based creation preserves styles; editing preserves headers, footers, numbering, untouched runs and section properties (tested) |
| XLSX | `…officedocument.spreadsheetml.sheet` | workbook/sheets, dimensions, cells, ranges, formulas **and cached values separately**, tables, charts (type/title/series), named ranges, number formats/fonts, merged cells | create (headings/paragraphs/tables), edit: set_cell, set_range, add_sheet, add_table, add_chart (bar/line/pie), set metadata | sheet, cell (A1), range (A1:B10) | row-window `max_rows` with `sheet:Name:row:N` cursor; `max_cells` guard; a written formula has **no cached value** until Excel/LibreOffice recalculates → warning `formula_recalc_required` |
| PPTX | `…officedocument.presentationml.presentation` | slides, layout/master names, theme colour scheme, shapes (text/table/chart/picture/placeholder), shape text with paragraph levels, speaker notes, slide size | create (title/bullets/table/image), edit: add/delete/move slide, replace_text (run-aware), insert image, set metadata | slide (1-based), document | template creation preserves layouts/masters/theme (tested by theme-colour comparison); `replace_image` by shape requires an identifying pass and is reported as skipped |
| Text / Markdown | `text/plain`, `text/markdown` | paragraphs, markdown headings as section path, outline | create/edit (`replace_text`, append paragraph) | paragraph, section, document | encoding: utf-8-sig → utf-8 → cp949 → replacement (warning `encoding_replaced`) |
| CSV | `text/csv` | table rows (header row 1), `max_rows` window | create/edit as text | document | malformed row → warning `csv_parse_failed:<Error>`; no silent truncation of columns |
| Images | `image/png|jpeg|webp|gif|tiff|bmp` | asset metadata only | — | image, document | content description only when a vision capability is bound; otherwise warning `image_content_requires_vision` and no invented text |

## Search depth per family

All families are chunked with the same structural chunker (heading/slide/page boundaries first,
sentence split second, ~640 target / 1 000 max tokens):

* keyword: SQLite FTS5 (`unicode61`) with a deterministic `LIKE` fallback when FTS5 is missing;
* vector/hybrid: only when the deployment supplies an embedder (`set_embedder` + optional
  semantic search hook) — otherwise `degraded=true`, `degradation_reason="embedding_unavailable"`;
* metadata filters: MIME, family, document id, name, path prefix, updated-after, arbitrary
  metadata key/value;
* de-duplication: by SHA-256 of the excerpt, counted in `duplicates_removed`.

## Deliberate non-goals (stated, not hidden)

* no PDF writing, no page re-layout;
* no DOCX tracked changes or comments API (comments are preserved on edit, not authored);
* no Excel recalculation engine (formulas are written, not evaluated);
* no PPTX chart authoring (existing charts are read; new charts are not created);
* no rendering of Office pages to images in the knowledge API (the legacy parser pipeline keeps
  its renderer for durable analysis).
