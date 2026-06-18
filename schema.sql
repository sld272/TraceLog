PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS souls (
    name        TEXT PRIMARY KEY,
    file_path   TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_souls_enabled ON souls(enabled, sort_order);

CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    content     TEXT NOT NULL,
    importance  REAL DEFAULT 0.5,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_importance ON posts(importance DESC);

CREATE TABLE IF NOT EXISTS post_soul_orders (
    post_id    TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    soul_name  TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (post_id, soul_name)
);

CREATE INDEX IF NOT EXISTS idx_post_soul_orders_post_order
    ON post_soul_orders(post_id, sort_order, soul_name);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    content,
    content='posts',
    content_rowid='rowid'
);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts_trigram USING fts5(
    content,
    tokenize='trigram',
    content='posts',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, content) VALUES (new.rowid, new.content);
    INSERT INTO posts_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO posts_fts_trigram(posts_fts_trigram, rowid, content)
        VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO posts_fts(rowid, content) VALUES (new.rowid, new.content);

    INSERT INTO posts_fts_trigram(posts_fts_trigram, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO posts_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    soul_name   TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'assistant' CHECK(role IN ('assistant', 'user')),
    content     TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT,
    created_at  REAL NOT NULL,
    edited_at   REAL,
    rerun_at    REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_conversation_seq
    ON comments(post_id, soul_name, seq);
CREATE INDEX IF NOT EXISTS idx_comments_post_soul
    ON comments(post_id, soul_name, seq);
CREATE INDEX IF NOT EXISTS idx_comments_soul_created
    ON comments(soul_name, created_at DESC);

CREATE TABLE IF NOT EXISTS attachments (
    id                TEXT PRIMARY KEY,
    file_path         TEXT NOT NULL,
    mime_type         TEXT NOT NULL CHECK(mime_type IN ('image/jpeg', 'image/png')),
    file_size         INTEGER NOT NULL,
    width             INTEGER NOT NULL,
    height            INTEGER NOT NULL,
    sha256            TEXT NOT NULL,
    original_filename TEXT,
    linked_at         REAL,
    created_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON attachments(sha256);
CREATE INDEX IF NOT EXISTS idx_attachments_linked_created ON attachments(linked_at, created_at);

CREATE TABLE IF NOT EXISTS vision_cache (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id  TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    description    TEXT,
    visible_text   TEXT,
    uncertainties  TEXT,
    status         TEXT NOT NULL CHECK(status IN ('ok', 'failed', 'skipped')),
    error          TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE(attachment_id, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_vision_cache_attachment
    ON vision_cache(attachment_id);
CREATE INDEX IF NOT EXISTS idx_vision_cache_status
    ON vision_cache(status, updated_at);

CREATE TABLE IF NOT EXISTS post_attachments (
    post_id       TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_post_attachments_post
    ON post_attachments(post_id, sort_order);

CREATE TABLE IF NOT EXISTS comment_attachments (
    comment_id    INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (comment_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_comment_attachments_comment
    ON comment_attachments(comment_id, sort_order);

CREATE TABLE IF NOT EXISTS chat_message_attachments (
    message_id    INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_message_attachments_message
    ON chat_message_attachments(message_id, sort_order);

CREATE TABLE IF NOT EXISTS entities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    name          TEXT NOT NULL,
    aliases       TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    mention_count INTEGER DEFAULT 0,
    metadata      TEXT,
    UNIQUE(type, name)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_last_seen ON entities(last_seen DESC);

CREATE TABLE IF NOT EXISTS post_entities (
    post_id   TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role      TEXT,
    PRIMARY KEY (post_id, entity_id, role)
);

CREATE INDEX IF NOT EXISTS idx_pe_entity ON post_entities(entity_id);

CREATE TABLE IF NOT EXISTS emotions (
    post_id   TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    label     TEXT NOT NULL,
    intensity REAL NOT NULL,
    PRIMARY KEY (post_id, label)
);

CREATE INDEX IF NOT EXISTS idx_emotions_label ON emotions(label, intensity DESC);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id   TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    summary   TEXT NOT NULL,
    category  TEXT,
    metadata  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);

CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,
    strength    REAL DEFAULT 0.5,
    last_seen   TEXT,
    metadata    TEXT,
    UNIQUE(entity_a, entity_b, rel_type)
);

CREATE TABLE IF NOT EXISTS relations_log (
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    delta       REAL NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (post_id, relation_id)
);

CREATE TABLE IF NOT EXISTS reflections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    type          TEXT NOT NULL,
    scope_start   TEXT,
    scope_end     TEXT,
    content       TEXT NOT NULL,
    related_posts TEXT,
    metadata      TEXT
);

CREATE INDEX IF NOT EXISTS idx_reflections_ts ON reflections(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(type);

CREATE TABLE IF NOT EXISTS chat_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    soul_name       TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    title           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_message_at REAL
);

CREATE INDEX IF NOT EXISTS idx_chat_threads_soul ON chat_threads(soul_name, last_message_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    edited_at   REAL,
    rerun_at    REAL,
    metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS todos (
    id           TEXT PRIMARY KEY,
    task         TEXT NOT NULL,
    date         TEXT,
    start_time   TEXT,
    end_time     TEXT,
    status       TEXT NOT NULL DEFAULT '未完成',
    source_post  TEXT REFERENCES posts(id) ON DELETE SET NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, date);
CREATE INDEX IF NOT EXISTS idx_todos_date ON todos(date);

CREATE TABLE IF NOT EXISTS user_md_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot   TEXT NOT NULL,
    patch      TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_md_rev_ts ON user_md_revisions(created_at DESC);

CREATE TABLE IF NOT EXISTS soul_memory_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    soul_name  TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    snapshot   TEXT NOT NULL,
    patch      TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_soul_memory_rev_soul_ts
    ON soul_memory_revisions(soul_name, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    status        TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 1,
    error         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_type_status
    ON jobs(type, status);

CREATE TABLE IF NOT EXISTS post_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_post_events_post_id
    ON post_events(post_id, id);

CREATE TABLE IF NOT EXISTS vector_docs (
    doc_id          TEXT PRIMARY KEY,
    doc_type        TEXT NOT NULL,
    source_table    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    metadata_json   TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vector_docs_revision
    ON vector_docs(source_revision);
CREATE INDEX IF NOT EXISTS idx_vector_docs_source
    ON vector_docs(source_table, source_id);

CREATE TABLE IF NOT EXISTS vector_doc_tombstones (
    doc_id            TEXT PRIMARY KEY,
    deleted_revision  INTEGER NOT NULL,
    deleted_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vector_doc_tombstones_revision
    ON vector_doc_tombstones(deleted_revision);

CREATE TABLE IF NOT EXISTS evidence_feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    doc_id     TEXT NOT NULL,
    verdict    TEXT NOT NULL DEFAULT 'irrelevant',
    created_at REAL NOT NULL,
    UNIQUE(channel, message_id, doc_id)
);

CREATE TABLE IF NOT EXISTS vector_index_collections (
    collection_name       TEXT PRIMARY KEY,
    embedding_config_hash TEXT NOT NULL,
    embedding_model       TEXT NOT NULL,
    embedding_base_url    TEXT NOT NULL,
    synced_revision       INTEGER NOT NULL DEFAULT 0,
    ready                 INTEGER NOT NULL DEFAULT 0,
    last_audited_at       REAL,
    audit_status          TEXT NOT NULL DEFAULT 'unknown',
    updated_at            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_index_items (
    collection_name TEXT NOT NULL REFERENCES vector_index_collections(collection_name) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    indexed_at      REAL NOT NULL,
    PRIMARY KEY (collection_name, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_vector_index_items_doc
    ON vector_index_items(doc_id);

CREATE TABLE IF NOT EXISTS vector_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_name TEXT NOT NULL REFERENCES vector_index_collections(collection_name) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL,
    op              TEXT NOT NULL CHECK(op IN ('upsert', 'delete')),
    target_hash     TEXT,
    source_revision INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'succeeded', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    finished_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_vector_outbox_collection_status
    ON vector_outbox(collection_name, status, id);
CREATE INDEX IF NOT EXISTS idx_vector_outbox_doc_status
    ON vector_outbox(collection_name, doc_id, status);

-- ---------------------------------------------------------------------------
-- memory v2: append-only evidence event ledger
-- Every create/edit/rerun/delete on a business row (post/comment/chat) appends
-- an immutable evidence event in the SAME transaction. memory units bind to
-- these event versions (not to mutable source rows), so edits never silently
-- rewrite history. `id` is the monotonic consumption cursor for reconcile.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_ingest_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope      TEXT NOT NULL,              -- 'global' | 'soul:<name>'
    visibility_scope TEXT NOT NULL,              -- 'public' | 'thread:<post_id>' | 'private:soul:<name>'
    source_channel   TEXT NOT NULL
                       CHECK(source_channel IN ('post','comment','chat')),
    source_type      TEXT NOT NULL
                       CHECK(source_type IN ('post','comment_message','chat_message')),
    source_id        TEXT NOT NULL,
    source_revision  INTEGER NOT NULL,           -- monotonic from 1 per source_id
    op               TEXT NOT NULL
                       CHECK(op IN ('create','edit','rerun','delete')),
    content_snapshot TEXT,                        -- version at the time; delete may be NULL
    content_hash     TEXT,                        -- sha256(content_snapshot)
    occurred_at      REAL NOT NULL,               -- business action time
    created_at       REAL NOT NULL,               -- ledger insert time
    UNIQUE(source_type, source_id, source_revision)
);
CREATE INDEX IF NOT EXISTS idx_memory_events_boundary_id
    ON memory_ingest_events(owner_scope, visibility_scope, id);
CREATE INDEX IF NOT EXISTS idx_memory_events_source
    ON memory_ingest_events(source_type, source_id, source_revision);

CREATE TABLE IF NOT EXISTS memory_reconcile_cursors (
    owner_scope      TEXT NOT NULL,
    visibility_scope TEXT NOT NULL,
    last_event_id    INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL,
    PRIMARY KEY(owner_scope, visibility_scope)
);
