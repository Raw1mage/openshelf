import pytest
import tempfile
import shutil
from pathlib import Path
from app.storage.manager import StorageManager


def test_storage_manager_basic():
    temp_dir = tempfile.mkdtemp()
    try:
        storage = StorageManager(base_dir=temp_dir)
        assert storage.raw_dir.exists()
        assert storage.parsed_dir.exists()
        assert storage.db_dir.exists()

        data = b"Hello, OpenShelf Test PDF Stream!"
        rel_path, sha256, md5, size = storage.save_raw_bytes(data, "pdf")

        assert rel_path.startswith("raw/")
        assert rel_path.endswith(".pdf")
        assert size == len(data)

        # 讀取測試
        abs_path = storage.resolve_path(rel_path)
        assert abs_path.exists()
        assert abs_path.read_bytes() == data

        # 儲存與讀取 Markdown
        work_id = "wk_test123"
        md_text = "# Test Chapter\n\nContent paragraph here."
        saved_md_path = storage.save_parsed_markdown(work_id, md_text)
        assert saved_md_path == f"parsed/{work_id}.md"

        read_content = storage.get_parsed_content(work_id)
        assert read_content == md_text

        # 安全路徑防護測試
        with pytest.raises(ValueError):
            storage.resolve_path("../../../etc/passwd")

    finally:
        shutil.rmtree(temp_dir)
