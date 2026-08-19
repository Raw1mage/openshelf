import pytest
from app.crawler.libgen_live import LibgenCrawler
from app.crawler.mirror_resolver import MirrorResolver
from app.crawler.download_worker import DownloadWorker, DownloadJob


def test_parse_size_to_bytes():
    assert LibgenCrawler.parse_size_to_bytes("10 Mb") == 10 * 1024 * 1024
    assert LibgenCrawler.parse_size_to_bytes("500 Kb") == 500 * 1024
    assert LibgenCrawler.parse_size_to_bytes("1.5 Gb") == int(1.5 * 1024 * 1024 * 1024)
    assert LibgenCrawler.parse_size_to_bytes("invalid") == 0


def test_parse_libgen_html():
    crawler = LibgenCrawler()
    sample_html = """
    <table class="c">
        <tr>
            <th>ID</th><th>Author(s)</th><th>Title</th><th>Publisher</th><th>Year</th>
            <th>Pages</th><th>Language</th><th>Size</th><th>Extension</th><th>Mirrors</th>
        </tr>
        <tr>
            <td>1001</td>
            <td>Knuth, Donald E.</td>
            <td><a href="book.php?id=1001"><b>The Art of Computer Programming</b></a><font color="green"><i> [Vol 1]</i></font></td>
            <td>Addison-Wesley</td>
            <td>1997</td>
            <td>672</td>
            <td>English</td>
            <td>25 Mb</td>
            <td>pdf</td>
            <td><a href="http://library.lol/main/8095d81a62e1a56e6c2133e41c20941b">library.lol</a></td>
        </tr>
    </table>
    """
    results = crawler._parse_libgen_is_html(sample_html, "https://libgen.is")
    assert len(results) == 1
    item = results[0]
    assert "The Art of Computer Programming" in item["title"]
    assert item["authors_display"] == "Knuth, Donald E."
    assert item["md5"] == "8095d81a62e1a56e6c2133e41c20941b"
    assert item["format"] == "pdf_born_digital"
    assert item["size_bytes"] == 25 * 1024 * 1024
    assert item["availability_tier"] == 2


def test_download_worker_job_lifecycle(tmp_path):
    from app.storage.manager import StorageManager
    from app.db.engine import DatabaseEngine
    from app.db.dao import CatalogDAO
    from app.pipeline.ingest import IngestionPipeline

    storage = StorageManager(base_dir=tmp_path)
    engine = DatabaseEngine(db_path=storage.get_db_path())
    dao = CatalogDAO(engine=engine)
    pipeline = IngestionPipeline(storage=storage, dao=dao)

    worker = DownloadWorker(pipeline=pipeline)
    job = worker.enqueue(
        md5="8095d81a62e1a56e6c2133e41c20941b",
        title="演算法之美",
        authors="Brian Christian",
        extension="pdf"
    )
    assert job.status in ("queued", "downloading")
    assert job.md5 == "8095d81a62e1a56e6c2133e41c20941b"
    
    retrieved = worker.get_job(job.job_id)
    assert retrieved is not None
    assert retrieved["title"] == "演算法之美"
