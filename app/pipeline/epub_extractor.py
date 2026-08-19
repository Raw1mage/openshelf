from pathlib import Path
from typing import Dict, Any, Tuple
import fitz  # PyMuPDF 原生支援 EPUB 抽取


class EPUBExtractor:
    """使用 PyMuPDF 高速擷取 EPUB 內文與元資料。"""

    @staticmethod
    def extract(file_path: Path) -> Tuple[str, int, Dict[str, Any]]:
        """
        抽取 EPUB 內容。
        回傳: (markdown_text, total_pages, metadata)
        """
        doc = fitz.open(str(file_path))
        total_pages = len(doc)
        meta = doc.metadata or {}

        markdown_lines = []
        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text("text").strip()
            if text:
                markdown_lines.append(f"\n\n{text}")

        extracted_text = "".join(markdown_lines).strip()
        doc.close()

        metadata = {
            "title": meta.get("title") or "",
            "author": meta.get("author") or "",
            "subject": meta.get("subject") or "",
            "format": "epub"
        }

        return extracted_text, total_pages, metadata
