import re
import struct
from pathlib import Path
from typing import Tuple, Dict, Any, List, Optional
import pymupdf


class MobiExtractor:
    """解析 MOBI / AZW 格式電子書與漫畫，抽取純文字並自動轉成 PDF / EPUB 供線上閱讀器瀏覽。"""

    @staticmethod
    def decompress_palmdoc(data: bytes) -> bytes:
        """PalmDoc (LZ77) 演算法解壓縮。"""
        out = bytearray()
        i = 0
        length_data = len(data)
        while i < length_data:
            c = data[i]
            i += 1
            if 1 <= c <= 8:
                out.extend(data[i:i+c])
                i += c
            elif c <= 0x7f:
                out.append(c)
            elif c >= 0xc0:
                out.append(32)
                out.append(c ^ 0x80)
            else:
                if i >= length_data:
                    break
                c2 = data[i]
                i += 1
                distance = (((c << 8) | c2) >> 3) & 0x07ff
                length = (c2 & 7) + 3
                start = len(out) - distance
                for _ in range(length):
                    if 0 <= start < len(out):
                        out.append(out[start])
                    start += 1
        return bytes(out)

    @classmethod
    def extract(cls, file_path: Path) -> Tuple[str, List[bytes], Dict[str, Any]]:
        """抽取 MOBI 檔案的純文字、內嵌圖片與元資料。"""
        with open(file_path, "rb") as f:
            raw = f.read()

        if len(raw) < 78:
            return "", [], {}

        num_records, = struct.unpack(">H", raw[76:78])
        offsets = []
        for i in range(num_records):
            pos = 78 + i * 8
            if pos + 4 > len(raw):
                break
            off, = struct.unpack(">I", raw[pos:pos+4])
            offsets.append(off)

        if not offsets:
            return "", [], {}

        # 讀取 Record 0 (PalmDoc / MOBI 標頭)
        rec0_end = offsets[1] if len(offsets) > 1 else len(raw)
        rec0 = raw[offsets[0]:rec0_end]
        
        compression = struct.unpack(">H", rec0[:2])[0] if len(rec0) >= 2 else 1
        num_text_records = struct.unpack(">H", rec0[8:10])[0] if len(rec0) >= 10 else 0

        # 解壓所有文字區塊
        decompressed_chunks = bytearray()
        for i in range(1, min(num_text_records + 1, len(offsets))):
            end_off = offsets[i+1] if i + 1 < len(offsets) else len(raw)
            rec_data = raw[offsets[i]:end_off]
            if compression == 2:
                decompressed_chunks.extend(cls.decompress_palmdoc(rec_data))
            else:
                decompressed_chunks.extend(rec_data)

        html_text = decompressed_chunks.decode("utf-8", errors="ignore")
        # 清理 HTML 標籤取得乾淨純文字
        clean_text = re.sub(r"<[^>]+>", " ", html_text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        # 抽取圖片（漫畫頁面或插圖）
        images: List[bytes] = []
        for i in range(num_text_records + 1, len(offsets) - 1):
            end_off = offsets[i+1]
            rec_data = raw[offsets[i]:end_off]
            if rec_data.startswith(b"\xff\xd8\xff") or rec_data.startswith(b"\x89PNG") or rec_data.startswith(b"GIF8"):
                images.append(rec_data)

        meta = {
            "title": file_path.stem,
            "images_count": len(images),
            "text_length": len(clean_text)
        }

        return clean_text, images, meta

    @classmethod
    def convert_to_pdf(cls, file_path: Path, output_pdf_path: Path) -> bool:
        """將 MOBI（漫畫或文字書）轉換為標準 PDF 以供線上閱讀器預覽。"""
        clean_text, images, meta = cls.extract(file_path)

        pdf_doc = pymupdf.open()
        if images:
            # 圖片/漫畫型書籍：將每一張圖片組裝為一頁高畫質 PDF
            for img_bytes in images:
                try:
                    img_doc = pymupdf.open(stream=img_bytes, filetype="jpeg")
                    rect = img_doc[0].rect
                    page = pdf_doc.new_page(width=rect.width, height=rect.height)
                    page.insert_image(rect, stream=img_bytes)
                except Exception:
                    continue
        elif clean_text:
            # 純文字型書籍：分頁排版產生 PDF
            lines = clean_text.splitlines()
            page_width, page_height = 595, 842  # A4
            margin = 50
            line_height = 20
            lines_per_page = int((page_height - margin * 2) / line_height)

            for i in range(0, max(1, len(lines)), lines_per_page):
                page = pdf_doc.new_page(width=page_width, height=page_height)
                page_text = "\n".join(lines[i:i+lines_per_page])
                page.insert_text((margin, margin), page_text, fontsize=12)

        if len(pdf_doc) > 0:
            pdf_doc.save(str(output_pdf_path))
            pdf_doc.close()
            return True
        return False
