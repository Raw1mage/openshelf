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

-- 系統與客製化設定 (System Settings)
CREATE TABLE IF NOT EXISTS system_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
