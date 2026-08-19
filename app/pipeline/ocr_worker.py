from pathlib import Path
from typing import Optional
import fitz  # PyMuPDF


class OCRWorker:
    """使用 RapidOCR (ONNX CPU 版) 對掃描頁面進行非同步光學字元辨識。"""

    def __init__(self):
        self._engine = None
        self._initialized = False

    def _get_engine(self):
        if not self._initialized:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._engine = RapidOCR()
            except ImportError:
                self._engine = None
            self._initialized = True
        return self._engine

    def ocr_pdf(self, file_path: Path, max_pages: int = 200) -> str:
        """對掃描 PDF 渲染圖片並執行 OCR。"""
        engine = self._get_engine()
        if engine is None:
            return "（系統未安裝 rapidocr-onnxruntime，已跳過 OCR 辨識）"

        doc = fitz.open(str(file_path))
        total_pages = min(len(doc), max_pages)
        ocr_results = []

        for page_num in range(total_pages):
            page = doc[page_num]
            # 渲染為 150 DPI 圖片
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")

            # 執行 RapidOCR
            result, _ = engine(img_bytes)
            if result:
                page_text = "\n".join([line[1] for line in result])
                ocr_results.append(f"\n\n## 第 {page_num + 1} 頁 (OCR)\n\n{page_text}")

        doc.close()
        return "".join(ocr_results).strip()
