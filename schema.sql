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
    created_at  REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_conversation_seq
    ON comments(post_id, soul_name, seq);
CREATE INDEX IF NOT EXISTS idx_comments_post_soul
    ON comments(post_id, soul_name, seq);
CREATE INDEX IF NOT EXISTS idx_comments_soul_created
    ON comments(soul_name, created_at DESC);

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
    created_at  REAL NOT NULL
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
