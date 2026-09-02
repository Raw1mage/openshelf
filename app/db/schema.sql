-- openshelf Core Relational Schema

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS work (
    work_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_provenance TEXT NOT NULL DEFAULT 'filename_parsed',
    work_type TEXT NOT NULL DEFAULT 'unknown',
    language TEXT,
    publication_year INTEGER,
    authors_display TEXT,
    availability_tier INTEGER NOT NULL DEFAULT 0,
    relevance_authority REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    merged_into TEXT,
    -- 分類可判定狀態：pending|classified|unclassified|error|disabled
    -- 預設 'pending' 而非 'unclassified'：新 Work 是「還沒判」不是「判不出」，
    -- 兩者共用同一個值就無法把待辦撈出來重跑（feature_smart-book-classification）。
    classification_state TEXT NOT NULL DEFAULT 'pending',
    classified_at TEXT,
    classification_error TEXT,
    FOREIGN KEY (merged_into) REFERENCES work(work_id)
);

CREATE INDEX IF NOT EXISTS idx_work_title ON work(title);
CREATE INDEX IF NOT EXISTS idx_work_year ON work(publication_year);
CREATE INDEX IF NOT EXISTS idx_work_availability ON work(availability_tier);

CREATE TABLE IF NOT EXISTS identifier (
    identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    scheme TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'asserted',
    UNIQUE (work_id, scheme, value),
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_identifier_lookup ON identifier(scheme, value);

CREATE TABLE IF NOT EXISTS manifestation (
    manifestation_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    version TEXT DEFAULT 'unknown',
    format TEXT DEFAULT 'unknown',
    origin TEXT NOT NULL DEFAULT 'local',
    license_id TEXT,
    is_retrievable INTEGER NOT NULL DEFAULT 1,
    external_url TEXT,
    -- P2P / BitTorrent 來源欄位（aggregator_torrent-p2p-integration Phase 1）
    torrent_url TEXT,
    magnet_uri TEXT,
    download_protocol TEXT NOT NULL DEFAULT 'http',
    peers_count INTEGER,
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_manifestation_work ON manifestation(work_id);
-- 注：download_protocol 等新增欄位的索引不得寫在此處。本檔經 executescript 執行於
-- DAO 欄位遷移之前；對舊 DB，CREATE TABLE IF NOT EXISTS 會静默 no-op（舊表無新欄位），
-- 紧接著的 CREATE INDEX 就會以 "no such column" 中斷啟動。
-- 新欄位索引一律由 CatalogDAO.apply_column_migrations() 於 ALTER 完成後建立。

CREATE TABLE IF NOT EXISTS file_object (
    file_id TEXT PRIMARY KEY,
    manifestation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    md5 TEXT,
    size_bytes INTEGER NOT NULL,
    produced_by TEXT,
    produced_at TEXT,
    FOREIGN KEY (manifestation_id) REFERENCES manifestation(manifestation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_file_object_sha256 ON file_object(sha256);
CREATE INDEX IF NOT EXISTS idx_file_object_md5 ON file_object(md5);

CREATE TABLE IF NOT EXISTS reading_state (
    work_id TEXT PRIMARY KEY,
    user_curation_score REAL DEFAULT 1.0,
    progress_ratio REAL DEFAULT 0.0,
    last_page INTEGER DEFAULT 1,
    total_pages INTEGER DEFAULT 1,
    last_opened_at TEXT,
    added_at TEXT NOT NULL,
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS download_job (
    job_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    expected_checksum TEXT,
    progress_percent INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- P2P / BitTorrent 來源與即時 Peer 狀態（Phase 1）
    torrent_url TEXT,
    magnet_uri TEXT,
    download_protocol TEXT NOT NULL DEFAULT 'http',
    peers_count INTEGER,
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE
);

-- FTS5 全文檢索虛擬表（支援中英文字元級 Trigram 分詞）
CREATE VIRTUAL TABLE IF NOT EXISTS work_fts USING fts5(
    work_id UNINDEXED,
    title,
    authors_display,
    content,
    tokenize='trigram'
);

-- 個人化書單 (Collections)
CREATE TABLE IF NOT EXISTS collection (
    collection_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT DEFAULT '📚',
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_item (
    collection_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    notes TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection_id, work_id),
    FOREIGN KEY (collection_id) REFERENCES collection(collection_id) ON DELETE CASCADE,
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_collection_item_work ON collection_item(work_id);

-- 多階層分類體系與架位 (Hierarchical Categories & Shelves)
CREATE TABLE IF NOT EXISTS category (
    category_id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    icon TEXT DEFAULT '📖',
    level INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES category(category_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_category_parent ON category(parent_id);
CREATE INDEX IF NOT EXISTS idx_category_slug ON category(slug);

CREATE TABLE IF NOT EXISTS work_category (
    work_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    -- 分類 provenance：rule|llm|legacy。'legacy' 專指本功能上線前由舊
    -- infer_categories_for_work() 寫入者（含已知錯誤的 cat_800+cat_850 fallback）。
    source TEXT NOT NULL DEFAULT 'legacy',
    model TEXT,
    prompt_version TEXT,
    assigned_at TEXT,
    PRIMARY KEY (work_id, category_id),
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES category(category_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_category_cat ON work_category(category_id);
-- 注：source / classification_state 的索引不得寫在此處，理由同 manifestation
-- 上方那段註解：本檔經 executescript 執行於 DAO 欄位遷移之前，對舊 DB
-- CREATE TABLE IF NOT EXISTS 會靜默 no-op，緊接著的 CREATE INDEX 就會以
-- "no such column" 中斷啟動。一律由 apply_column_migrations() 於 ALTER 後建立。

CREATE TABLE IF NOT EXISTS remote_catalog_item (
    catalog_id TEXT PRIMARY KEY,
    -- Identity 重構（DD-1/DD-2, aggregator_multi-source-provider）：
    -- `md5 TEXT UNIQUE` 曾是唯一識別碼，但 SQLite 對多筆 NULL 值不觸發 UNIQUE
    -- 衝突——非 libgen 來源（無 md5）進來時會靜默失去去重。identity 主鍵改為
    -- `source`/`source_native_id`；md5 降級為可空、非唯一的橋接欄位（既有
    -- libgen 下載流程仍依賴它，不可斷）。複合唯一索引與新欄位在舊 DB 上
    -- 由 CatalogDAO.apply_column_migrations() 於 ALTER 完成後建立/回填
    -- （理由同上方 manifestation/work_category 的欄位遷移注記）。
    source TEXT NOT NULL DEFAULT 'libgen',
    source_native_id TEXT,
    md5 TEXT,
    title TEXT NOT NULL,
    authors_display TEXT,
    publication_year INTEGER,
    language TEXT,
    format TEXT,
    extension TEXT,
    size_bytes INTEGER,
    -- Open Library 橋接欄位（DD-4, Phase 3）。全部可空：OL 是**寫入時的一次性
    -- enrich**，不是必要條件——查不到、逾時、OL 掛掉都不得阻斷書目上架，
    -- 那些情況下這幾格就停在 NULL。
    --
    -- `NULL` 在此逐字的意思是「尚未成功回填」，它同時涵蓋「還沒查」與
    -- 「查過但 OL 沒有」——所以**不可**只看這幾格判斷有沒有查過，
    -- 那是 `ol_enriched_at` 的職責（見下）。兩者刻意分開：欄位為空與
    -- 未曾查詢若共用同一個輸出，節流就無從判斷該不該重打 OL。
    ol_key TEXT,
    isbn TEXT,
    oclc TEXT,
    lccn TEXT,
    gutenberg_id TEXT,
    -- 最後一次「成功完成查詢」的時間（含查到 0 筆的 empty）。失敗不寫這格——
    -- 失敗要能被下一輪重試，寫了就等於把一次失敗當成一次有效查詢。
    ol_enriched_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_catalog_source (
    catalog_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    external_url TEXT,
    mirror_links_json TEXT NOT NULL DEFAULT '[]',
    torrent_url TEXT,
    magnet_uri TEXT,
    download_protocol TEXT NOT NULL DEFAULT 'http',
    peers_count INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, source_key),
    FOREIGN KEY (catalog_id) REFERENCES remote_catalog_item(catalog_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS remote_catalog_category (
    catalog_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    query_term TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (catalog_id, category_id),
    FOREIGN KEY (catalog_id) REFERENCES remote_catalog_item(catalog_id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES category(category_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS remote_catalog_refresh (
    refresh_id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL,
    status TEXT NOT NULL,
    query_term TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    last_success_at TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_added INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    cursor TEXT,
    FOREIGN KEY (category_id) REFERENCES category(category_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_remote_catalog_category_cat ON remote_catalog_category(category_id, catalog_id);
CREATE INDEX IF NOT EXISTS idx_remote_catalog_item_last_seen ON remote_catalog_item(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_remote_catalog_refresh_category ON remote_catalog_refresh(category_id, started_at DESC);

-- 系統與客製化設定 (System Settings)
CREATE TABLE IF NOT EXISTS system_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
