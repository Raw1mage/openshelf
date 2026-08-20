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
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_manifestation_work ON manifestation(work_id);

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
    PRIMARY KEY (work_id, category_id),
    FOREIGN KEY (work_id) REFERENCES work(work_id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES category(category_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_category_cat ON work_category(category_id);

-- 系統與客製化設定 (System Settings)
CREATE TABLE IF NOT EXISTS system_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
