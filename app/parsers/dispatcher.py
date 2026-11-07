from typing import Dict, Type
from app.parsers.base import BaseParser
from app.parsers.pdf_parser import PDFParser
from app.parsers.docx_parser import DOCXParser
from app.parsers.pptx_parser import PPTXParser

class ParserDispatcher:
    _parsers: Dict[str, BaseParser] = {
        "pdf": PDFParser(),
        "docx": DOCXParser(),
        "pptx": PPTXParser(),
    }

    @classmethod
    def get_parser(cls, doc_type: str) -> BaseParser:
        parser = cls._parsers.get(doc_type.lower())
        if not parser:
            raise ValueError(f"No parser registered for document type: {doc_type}")
        return parser
