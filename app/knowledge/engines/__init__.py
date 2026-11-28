"""Document engines for the Knowledge Contract.

Each engine exposes a small, uniform surface:

* ``read(path, ref, request) -> ReadResponse``
* ``create(request, blocks) -> bytes`` (writer families only)
* ``apply(path, ref, operations) -> tuple[bytes, applied, skipped, warnings, diff]``

Engines never talk to storage; `app.knowledge.service` owns revisions and atomic writes.
"""

from app.knowledge.engines import docx_engine, pdf_engine, pptx_engine, text_engine, xlsx_engine

__all__ = ["pdf_engine", "docx_engine", "xlsx_engine", "pptx_engine", "text_engine"]
