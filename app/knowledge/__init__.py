"""R21 Document & Knowledge Engine (AGTools side).

The package implements the `dk/v1` Document Knowledge Contract defined in AGCore
(`app/knowledge/contracts.py`) and documented in `docs/r21/DOCUMENT_KNOWLEDGE_CONTRACT.md`.
Everything here is additive: the legacy `/api/v1/documents` document-intelligence API keeps
its behaviour and payloads.
"""

from app.knowledge.contracts import CONTRACT_VERSION

__all__ = ["CONTRACT_VERSION"]
