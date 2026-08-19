from pathlib import Path
from typing import Dict, Any, Tuple
import fitz  # PyMuPDF


class PDFExtractor:
    """使用 PyMuPDF 高速擷取 PDF 內文、內嵌元資料並偵測掃描件。"""

    @staticmethod
    def extract(file_path: Path) -> Tuple[str, bool, int, Dict[str, Any]]:
        """
        抽取 PDF 內容。
        回傳: (markdown_text, is_scanned, total_pages, metadata)
        """
        doc = fitz.open(str(file_path))
        total_pages = len(doc)
        meta = doc.metadata or {}

        markdown_lines = []
        total_chars = 0

        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text("text").strip()
            total_chars += len(text)
            if text:
                markdown_lines.append(f"\n\n## 第 {page_num + 1} 頁\n\n{text}")

        # 啟發式判定掃描件：若提取字數為 0 或平均字數極少（< 5 字）
        avg_chars = total_chars / total_pages if total_pages > 0 else 0
        is_scanned = (total_chars == 0) or (avg_chars < 5)

        extracted_text = "".join(markdown_lines).strip()
        doc.close()

        metadata = {
            "title": meta.get("title") or "",
            "author": meta.get("author") or "",
            "subject": meta.get("subject") or "",
            "keywords": meta.get("keywords") or "",
            "creator": meta.get("creator") or "",
            "producer": meta.get("producer") or "",
            "format": "pdf_scanned" if is_scanned else "pdf_born_digital"
        }

        return extracted_text, is_scanned, total_pages, metadata
