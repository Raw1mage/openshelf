import re
from pathlib import Path
from typing import Any, Optional
from app.storage.manager import StorageManager
from app.db.dao import CatalogDAO
from app.models.catalog import WorkCreate, WorkDetailRead
from app.pipeline.pdf_extractor import PDFExtractor
from app.pipeline.epub_extractor import EPUBExtractor
from app.pipeline.mobi_extractor import MobiExtractor
from app.pipeline.ocr_worker import OCRWorker


class IngestionPipeline:
    """協調整合檔案落地、去重比對、純文字抽取、OCR 降級與 FTS5 索引入庫。"""

    # 嵌入式元資料的年份欄位型別不受控（int / "1987" / "1972 June 01" / "" / None），
    # 取第一個落在合理區間的四位數；取不出來就是 None（而非 0 或今年）。
    _YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b")

    def __init__(self, storage: StorageManager = None, dao: CatalogDAO = None):
        self.storage = storage or StorageManager()
        self.dao = dao or CatalogDAO()
        self.ocr_worker = OCRWorker()

    @classmethod
    def _coerce_publication_year(cls, raw: Any) -> Optional[int]:
        """把抽取器交回的任意年份表示法收斂成 int 或 None。

        上游沒有年份時必須維持 None，不得為了讓欄位「看起來有值」而塞預設值——
        那會讓「來源沒有」與「解析失敗」再次共用同一個輸出（BR-20260820_130500）。
        """
        if raw is None:
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw if 1000 <= raw <= 2199 else None
        match = cls._YEAR_RE.search(str(raw).strip())
        return int(match.group(1)) if match else None

    def ingest_bytes(
        self,
        data: bytes,
        filename: str,
        custom_title: Optional[str] = None,
        custom_author: Optional[str] = None
    ) -> WorkDetailRead:
        """接收檔案位元組進行完整入庫處理。"""
        ext = Path(filename).suffix.lstrip(".").lower()
        if not ext:
            ext = "pdf"

        # 1. 落地原檔至 /data/raw/{sha256}.{ext}
        rel_raw_path, sha256, md5, size_bytes = self.storage.save_raw_bytes(data, ext)
        full_raw_path = self.storage.resolve_path(rel_raw_path)

        # 2. 去重檢查
        existing_work_id = self.dao.find_work_by_hash(sha256)
        if existing_work_id:
            return self.dao.get_work_detail(existing_work_id)

        # 3. 抽取文字與元資料
        extracted_text = ""
        is_scanned = False
        format_type = f"{ext}"
        meta = {}

        if ext == "pdf":
            extracted_text, is_scanned, _, meta = PDFExtractor.extract(full_raw_path)
            if is_scanned:
                format_type = "pdf_scanned"
                ocr_text = self.ocr_worker.ocr_pdf(full_raw_path)
                if ocr_text:
                    extracted_text = ocr_text
            else:
                format_type = "pdf_born_digital"
        elif ext == "epub":
            extracted_text, _, meta = EPUBExtractor.extract(full_raw_path)
            format_type = "epub"
        elif ext in ("mobi", "azw", "azw3"):
            extracted_text, _, meta = MobiExtractor.extract(full_raw_path)
            format_type = "mobi"
            # 自動產生預覽用 PDF
            converted_pdf = self.storage.parsed_dir / f"{sha256}.pdf"
            MobiExtractor.convert_to_pdf(full_raw_path, converted_pdf)

        # 4. 書名與作者推導
        inferred_title = custom_title or meta.get("title") or Path(filename).stem
        inferred_author = custom_author or meta.get("author") or None
        inferred_year = self._coerce_publication_year(meta.get("publication_year") or meta.get("year"))
        provenance = "user_edited" if custom_title else ("embedded_metadata" if meta.get("title") else "filename_parsed")

        # 5. 建立 Work
        work_create = WorkCreate(
            title=inferred_title,
            title_provenance=provenance,
            work_type="book",
            language="zh" if any("\u4e00" <= c <= "\u9fff" for c in inferred_title) else "en",
            authors_display=inferred_author,
            publication_year=inferred_year,
            availability_tier=0
        )
        work_id = self.dao.create_work(work_create)

        # 6. 儲存抽取出的純文字
        self.storage.save_parsed_markdown(work_id, extracted_text)

        # 7. 登記識別碼 (SHA256, MD5)
        self.dao.add_identifier(work_id, "sha256", sha256, "asserted")
        self.dao.add_identifier(work_id, "md5", md5, "asserted")

        # 8. 登記 Manifestation 與 FileObject
        mf_id = self.dao.add_manifestation(work_id, format_type=format_type, origin="local")
        self.dao.add_file_object(
            manifestation_id=mf_id,
            role="original",
            local_path=rel_raw_path,
            sha256=sha256,
            md5=md5,
            size_bytes=size_bytes
        )

        # 9. 更新 FTS5 索引
        self.dao.update_fts_index(work_id, inferred_title, inferred_author, extracted_text)

        return self.dao.get_work_detail(work_id)

    def process_file(
        self,
        file_path: Path,
        metadata_override: Optional[dict] = None
    ) -> dict:
        """接收已落地的磁碟實體檔案進行純文字抽取與資料庫入庫註冊。"""
        metadata_override = metadata_override or {}
        custom_title = metadata_override.get("title")
        custom_author = metadata_override.get("authors_display")
        custom_year = self._coerce_publication_year(metadata_override.get("publication_year"))

        ext = file_path.suffix.lstrip(".").lower()
        if not ext:
            ext = "pdf"

        # 1. 計算指紋
        sha256, md5, size_bytes = self.storage.compute_file_hashes(file_path)
        rel_raw_path = f"raw/{file_path.name}"

        # 2. 去重檢查
        existing_work_id = self.dao.find_work_by_hash(sha256) or self.dao.find_work_by_hash(md5)
        if existing_work_id:
            return {"work_id": existing_work_id}

        # 3. 抽取文字與元資料
        extracted_text = ""
        is_scanned = False
        format_type = f"{ext}"
        meta = {}

        if ext == "pdf":
            extracted_text, is_scanned, _, meta = PDFExtractor.extract(file_path)
            if is_scanned:
                format_type = "pdf_scanned"
                ocr_text = self.ocr_worker.ocr_pdf(file_path)
                if ocr_text:
                    extracted_text = ocr_text
            else:
                format_type = "pdf_born_digital"
        elif ext == "epub":
            extracted_text, _, meta = EPUBExtractor.extract(file_path)
            format_type = "epub"
        elif ext in ("mobi", "azw", "azw3"):
            extracted_text, _, meta = MobiExtractor.extract(file_path)
            format_type = "mobi"
            # 自動產生預覽用 PDF
            converted_pdf = self.storage.parsed_dir / f"{sha256}.pdf"
            MobiExtractor.convert_to_pdf(file_path, converted_pdf)

        # 4. 書名與作者推導
        inferred_title = custom_title or meta.get("title") or file_path.stem
        inferred_author = custom_author or meta.get("author") or None
        inferred_year = custom_year if custom_year is not None else self._coerce_publication_year(
            meta.get("publication_year") or meta.get("year")
        )
        provenance = "user_edited" if custom_title else ("embedded_metadata" if meta.get("title") else "filename_parsed")

        # 5. 建立 Work
        work_create = WorkCreate(
            title=inferred_title,
            title_provenance=provenance,
            work_type="book",
            language="zh" if any("\\u4e00" <= c <= "\\u9fff" for c in inferred_title) else "en",
            authors_display=inferred_author,
            publication_year=inferred_year,
            availability_tier=0
        )
        work_id = self.dao.create_work(work_create)

        # 6. 儲存純文字
        self.storage.save_parsed_markdown(work_id, extracted_text)

        # 7. 登記識別碼
        self.dao.add_identifier(work_id, "sha256", sha256, "asserted")
        self.dao.add_identifier(work_id, "md5", md5, "asserted")

        # 8. 登記 Manifestation 與 FileObject
        mf_id = self.dao.add_manifestation(work_id, format_type=format_type, origin="local")
        self.dao.add_file_object(
            manifestation_id=mf_id,
            role="original",
            local_path=rel_raw_path,
            sha256=sha256,
            md5=md5,
            size_bytes=size_bytes
        )

        # 9. 更新 FTS5 索引
        self.dao.update_fts_index(work_id, inferred_title, inferred_author, extracted_text)

        return {"work_id": work_id}
