PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '2');

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
    content     TEXT NOT NULL,
    is_main     INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT,
    created_at  REAL NOT NULL,
    UNIQUE(post_id, soul_name)
);

CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_soul ON comments(soul_name, created_at DESC);

CREATE TABLE IF NOT EXISTS comment_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    soul_name       TEXT NOT NULL REFERENCES souls(name) ON DELETE CASCADE,
    root_comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_message_at REAL,
    UNIQUE(post_id, soul_name)
);

CREATE INDEX IF NOT EXISTS idx_comment_threads_post ON comment_threads(post_id, soul_name);
CREATE INDEX IF NOT EXISTS idx_comment_threads_soul ON comment_threads(soul_name, last_message_at DESC);

CREATE TABLE IF NOT EXISTS comment_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL REFERENCES comment_threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comment_messages_thread ON comment_messages(thread_id, created_at);

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

CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    type             TEXT NOT NULL CHECK (type IN (
        'preference',
        'correction',
        'convention',
        'decision',
        'insight',
        'pattern',
        'state',
        'relationship',
        'todo_signal'
    )),
    title            TEXT NOT NULL,
    summary          TEXT,
    narrative        TEXT NOT NULL,
    source_channel   TEXT NOT NULL CHECK (source_channel IN (
        'post',
        'comment',
        'comment_thread',
        'chat',
        'reflection',
        'todo'
    )),
    visibility_scope TEXT NOT NULL CHECK (visibility_scope IN (
        'global',
        'post_visible',
        'soul_scoped',
        'private_blocked'
    )),
    scope_post_id    TEXT REFERENCES posts(id) ON DELETE CASCADE,
    scope_soul_name  TEXT REFERENCES souls(name) ON DELETE CASCADE,
    importance       REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    confidence       REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active',
        'merged',
        'superseded',
        'archived'
    )),
    merged_into      INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    superseded_by    INTEGER REFERENCES observations(id) ON DELETE SET NULL,
    observed_at      REAL NOT NULL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    metadata         TEXT,
    CHECK (visibility_scope != 'post_visible' OR scope_post_id IS NOT NULL),
    CHECK (visibility_scope != 'soul_scoped' OR scope_soul_name IS NOT NULL),
    CHECK (visibility_scope != 'global' OR scope_soul_name IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_observations_status_time
    ON observations(status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_post_scope
    ON observations(visibility_scope, scope_post_id, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_soul_scope
    ON observations(visibility_scope, scope_soul_name, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_type_time
    ON observations(type, observed_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    title,
    summary,
    narrative,
    tokenize='trigram',
    content='observations',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations
WHEN new.status = 'active' AND new.visibility_scope != 'private_blocked'
BEGIN
    INSERT INTO observations_fts(rowid, title, summary, narrative)
    VALUES (new.id, new.title, IFNULL(new.summary, ''), new.narrative);
END;

CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations
WHEN old.status = 'active' AND old.visibility_scope != 'private_blocked'
BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, title, summary, narrative)
        VALUES ('delete', old.id, old.title, IFNULL(old.summary, ''), old.narrative);
END;

CREATE TRIGGER IF NOT EXISTS observations_au_delete AFTER UPDATE ON observations
WHEN old.status = 'active' AND old.visibility_scope != 'private_blocked'
BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, title, summary, narrative)
        VALUES ('delete', old.id, old.title, IFNULL(old.summary, ''), old.narrative);
END;

CREATE TRIGGER IF NOT EXISTS observations_au_insert AFTER UPDATE ON observations
WHEN new.status = 'active' AND new.visibility_scope != 'private_blocked'
BEGIN
    INSERT INTO observations_fts(rowid, title, summary, narrative)
    VALUES (new.id, new.title, IFNULL(new.summary, ''), new.narrative);
END;

CREATE TABLE IF NOT EXISTS observation_sources (
    observation_id  INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    source_type     TEXT NOT NULL CHECK (source_type IN (
        'post',
        'comment',
        'comment_message',
        'chat_message',
        'todo',
        'reflection'
    )),
    source_id       TEXT NOT NULL,
    excerpt         TEXT,
    evidence_access TEXT NOT NULL CHECK (evidence_access IN (
        'all',
        'post_visible',
        'source_soul_only',
        'none'
    )),
    created_at      REAL NOT NULL,
    metadata        TEXT,
    PRIMARY KEY (observation_id, source_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_observation_sources_source
    ON observation_sources(source_type, source_id);

CREATE TABLE IF NOT EXISTS observation_cursors (
    source_kind  TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    cursor_value TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    metadata     TEXT,
    PRIMARY KEY (source_kind, source_key)
);

CREATE TRIGGER IF NOT EXISTS observation_sources_posts_ad AFTER DELETE ON posts BEGIN
    DELETE FROM observation_sources
    WHERE source_type = 'post' AND source_id = old.id;
    DELETE FROM observations
    WHERE id IN (
        SELECT observations.id
        FROM observations
        LEFT JOIN observation_sources
            ON observation_sources.observation_id = observations.id
        WHERE observation_sources.observation_id IS NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS observation_sources_comments_ad AFTER DELETE ON comments BEGIN
    DELETE FROM observation_sources
    WHERE source_type = 'comment' AND source_id = CAST(old.id AS TEXT);
    DELETE FROM observations
    WHERE id IN (
        SELECT observations.id
        FROM observations
        LEFT JOIN observation_sources
            ON observation_sources.observation_id = observations.id
        WHERE observation_sources.observation_id IS NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS observation_sources_comment_messages_ad
AFTER DELETE ON comment_messages BEGIN
    DELETE FROM observation_sources
    WHERE source_type = 'comment_message' AND source_id = CAST(old.id AS TEXT);
    DELETE FROM observations
    WHERE id IN (
        SELECT observations.id
        FROM observations
        LEFT JOIN observation_sources
            ON observation_sources.observation_id = observations.id
        WHERE observation_sources.observation_id IS NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS observation_sources_chat_messages_ad
AFTER DELETE ON chat_messages BEGIN
    DELETE FROM observation_sources
    WHERE source_type = 'chat_message' AND source_id = CAST(old.id AS TEXT);
    DELETE FROM observations
    WHERE id IN (
        SELECT observations.id
        FROM observations
        LEFT JOIN observation_sources
            ON observation_sources.observation_id = observations.id
        WHERE observation_sources.observation_id IS NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS observation_sources_todos_ad AFTER DELETE ON todos BEGIN
    DELETE FROM observation_sources
    WHERE source_type = 'todo' AND source_id = old.id;
    DELETE FROM observations
    WHERE id IN (
        SELECT observations.id
        FROM observations
        LEFT JOIN observation_sources
            ON observation_sources.observation_id = observations.id
        WHERE observation_sources.observation_id IS NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS observation_sources_reflections_ad
AFTER DELETE ON reflections BEGIN
    DELETE FROM observation_sources
    WHERE source_type = 'reflection' AND source_id = CAST(old.id AS TEXT);
    DELETE FROM observations
    WHERE id IN (
        SELECT observations.id
        FROM observations
        LEFT JOIN observation_sources
            ON observation_sources.observation_id = observations.id
        WHERE observation_sources.observation_id IS NULL
    );
END;
