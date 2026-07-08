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

CREATE TABLE IF NOT EXISTS goals (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    detail           TEXT,
    horizon          TEXT NOT NULL CHECK(horizon IN ('short', 'long')),
    status           TEXT NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active', 'done', 'abandoned', 'paused')),
    source           TEXT NOT NULL DEFAULT 'user'
                         CHECK(source IN ('user', 'suggested_accepted')),
    focus            INTEGER NOT NULL DEFAULT 0 CHECK(focus IN (0, 1)),
    last_progress_at REAL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_goals_status_horizon
    ON goals(status, horizon, id);

CREATE TABLE IF NOT EXISTS suggestions (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK(kind IN ('todo', 'goal')),
    payload_json   TEXT NOT NULL,
    evidence_ref   TEXT,
    confidence     REAL NOT NULL DEFAULT 0.6 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending', 'accepted', 'dismissed')),
    normalized_key TEXT,
    created_at     REAL NOT NULL,
    decided_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_suggestions_kind_status
    ON suggestions(kind, status, id);
CREATE INDEX IF NOT EXISTS idx_suggestions_normkey
    ON suggestions(normalized_key);

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
                       CHECK(source_type IN ('post','post_vision','comment_message','comment_relationship','chat_message')),
    source_id        TEXT NOT NULL,
    source_revision  INTEGER NOT NULL,           -- monotonic from 1 per source_id
    op               TEXT NOT NULL
                       CHECK(op IN ('create','edit','rerun','delete')),
    author           TEXT                          -- 'user' | 'assistant' | NULL(unknown)
                       CHECK(author IS NULL OR author IN ('user','assistant')),
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

-- ---------------------------------------------------------------------------
-- memory v2: structured belief layer (memory units) + audit + view objects
-- A memory unit is a first-class cross-evidence belief: stable id, confidence,
-- evidence chain, status, time. Reconcile writes units (not md). owner_scope =
-- who manages it; visibility_scope = where it may be used (orthogonal). CHECK
-- enums are written in full now (SQLite cannot ALTER a CHECK) to avoid future
-- table rebuilds.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_units (
    id               TEXT PRIMARY KEY,            -- mu_<ulid>
    owner_scope      TEXT NOT NULL,              -- 'global' | 'soul:<name>'
    visibility_scope TEXT NOT NULL,              -- 'public' | 'thread:<post_id>' | 'private:soul:<name>'
    source_channel   TEXT NOT NULL
                       CHECK(source_channel IN ('post','comment','chat','user')),
    prompt_policy    TEXT NOT NULL DEFAULT 'allow'
                       CHECK(prompt_policy IN ('allow','no_prompt')),
    type             TEXT NOT NULL,              -- identity/preference/goal/state/relationship/insight/freeform
    content          TEXT NOT NULL,             -- cross-evidence abstraction, NOT a single raw transcription
    confidence       REAL NOT NULL DEFAULT 0.6,

    source           TEXT NOT NULL DEFAULT 'reflected'
                       CHECK(source IN ('reflected','user_authored')),
    status           TEXT NOT NULL DEFAULT 'active'
                       CHECK(status IN ('active','pending','dormant',
                                        'retracted_by_model','retracted_by_user',
                                        'superseded','challenged')),
    retraction_reason TEXT
                       CHECK(retraction_reason IS NULL OR
                             retraction_reason IN ('false','outdated')),

    tier             TEXT NOT NULL DEFAULT 'contextual'
                       CHECK(tier IN ('core','contextual','episodic')),
    portrait_policy  TEXT NOT NULL DEFAULT 'auto'
                       CHECK(portrait_policy IN ('auto','force_include','force_exclude')),
    importance       REAL NOT NULL DEFAULT 0.5,
    sensitivity      TEXT NOT NULL DEFAULT 'normal'
                       CHECK(sensitivity IN ('high','normal','low')),

    in_portrait      INTEGER NOT NULL DEFAULT 0, -- selector result cache
    normalized_claim TEXT,                        -- canonical assertion: tombstone dedup + linker key
    superseded_by    TEXT REFERENCES memory_units(id) ON DELETE SET NULL,
    -- set when other-bucket evidence contradicts this unit (P1). The mark is
    -- attribution-free by design: read paths hedge the unit ("不太确定") and the
    -- portrait excludes it, but nothing user- or model-visible says WHY. Fresh
    -- same-bucket evidence (confirm/revise) clears it.
    contested_at     REAL,

    first_seen       REAL NOT NULL,
    last_confirmed   REAL NOT NULL,
    retrieval_count  INTEGER NOT NULL DEFAULT 0, -- reserved for later phases
    metadata         TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_boundary_status
    ON memory_units(owner_scope, visibility_scope, status, prompt_policy);
CREATE INDEX IF NOT EXISTS idx_units_portrait
    ON memory_units(owner_scope, visibility_scope, in_portrait, status);

-- FTS5 over unit content (keyword side of memory-v2 unit retrieval). Mirrors the
-- posts_fts pair: a default-tokenizer table for non-CJK and a trigram table for
-- CJK substring matching. External-content, keyed on the (implicit) integer rowid.
-- Triggers keep it in sync; a pre-existing DB seeds its historical rows once with
-- INSERT INTO memory_units_fts(memory_units_fts) VALUES('rebuild') (+ the trigram table).
CREATE VIRTUAL TABLE IF NOT EXISTS memory_units_fts USING fts5(
    content,
    content='memory_units',
    content_rowid='rowid'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_units_fts_trigram USING fts5(
    content,
    tokenize='trigram',
    content='memory_units',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS memory_units_ai AFTER INSERT ON memory_units BEGIN
    INSERT INTO memory_units_fts(rowid, content) VALUES (new.rowid, new.content);
    INSERT INTO memory_units_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_units_ad AFTER DELETE ON memory_units BEGIN
    INSERT INTO memory_units_fts(memory_units_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO memory_units_fts_trigram(memory_units_fts_trigram, rowid, content)
        VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_units_au AFTER UPDATE ON memory_units BEGIN
    INSERT INTO memory_units_fts(memory_units_fts, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO memory_units_fts(rowid, content) VALUES (new.rowid, new.content);

    INSERT INTO memory_units_fts_trigram(memory_units_fts_trigram, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    INSERT INTO memory_units_fts_trigram(rowid, content) VALUES (new.rowid, new.content);
END;

-- Cross-bucket relations between units (P1: link, never merge). Buckets stay
-- isolated — a link is metadata about two beliefs, it never moves content or
-- rewrites either side. same_fact drives read-time folding (inject one copy in
-- private scenes); contradicts marks the more-public side contested (hedged,
-- out of portrait); context_variant records "both true, different contexts" —
-- deliberately NOT a contradiction. Pairs are stored with a_unit_id < b_unit_id.
CREATE TABLE IF NOT EXISTS memory_unit_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    a_unit_id   TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    b_unit_id   TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL
                  CHECK(relation IN ('same_fact','contradicts','context_variant')),
    created_by  TEXT NOT NULL DEFAULT 'linker',  -- 'linker' | 'user'
    created_at  REAL NOT NULL,
    UNIQUE(a_unit_id, b_unit_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_unit_links_a ON memory_unit_links(a_unit_id);
CREATE INDEX IF NOT EXISTS idx_unit_links_b ON memory_unit_links(b_unit_id);

CREATE TABLE IF NOT EXISTS memory_unit_evidence (
    unit_id     TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    event_id    INTEGER NOT NULL REFERENCES memory_ingest_events(id) ON DELETE RESTRICT,
    relation    TEXT NOT NULL DEFAULT 'supports'
                  CHECK(relation IN ('supports','contradicts','revises','source')),
    -- 1 while awaiting AI re-link after a user edit: the link is NOT counted as
    -- current support and does NOT trigger challenge until the judge keeps/drops it.
    review_pending INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    PRIMARY KEY (unit_id, event_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_unit_evidence_event ON memory_unit_evidence(event_id);

CREATE TABLE IF NOT EXISTS memory_unit_reconcile_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id          TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    trigger_event_id INTEGER NOT NULL REFERENCES memory_ingest_events(id) ON DELETE RESTRICT,
    reason           TEXT NOT NULL CHECK(reason IN ('edit','delete')),
    status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','resolved')),
    created_at       REAL NOT NULL,
    resolved_at      REAL,
    UNIQUE(unit_id, trigger_event_id)
);
CREATE INDEX IF NOT EXISTS idx_unit_reconcile_queue_pending
    ON memory_unit_reconcile_queue(status, unit_id, id);
CREATE INDEX IF NOT EXISTS idx_unit_reconcile_queue_trigger
    ON memory_unit_reconcile_queue(trigger_event_id, status);

-- Dedicated queue for the AI re-link pass after a user edits a unit. Separate
-- from memory_unit_reconcile_queue because re-link is a different task (narrow
-- "does this evidence still support the new content" judgment, no trigger event,
-- no challenge decision). unit_version records unit.updated_at at enqueue time so
-- a stale judge result from before a newer edit cannot overwrite the new content.
CREATE TABLE IF NOT EXISTS memory_unit_relink_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id      TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    unit_version REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','resolved')),
    created_at   REAL NOT NULL,
    resolved_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_unit_relink_queue_pending
    ON memory_unit_relink_queue(status, unit_id, id);

CREATE TABLE IF NOT EXISTS memory_reconcile_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type         TEXT NOT NULL,
    owner_scope      TEXT NOT NULL,
    visibility_scope TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    event_id_start   INTEGER,
    event_id_end     INTEGER,
    event_count      INTEGER NOT NULL DEFAULT 0,
    summary          TEXT NOT NULL DEFAULT '',
    metadata_json    TEXT,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_reconcile_runs_boundary
    ON memory_reconcile_runs(owner_scope, visibility_scope, id DESC);
CREATE INDEX IF NOT EXISTS idx_memory_reconcile_runs_created
    ON memory_reconcile_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS memory_unit_ops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id         TEXT NOT NULL,
    related_unit_id TEXT,
    op              TEXT NOT NULL,   -- add/challenge/retain/confirm/revise/retract/supersede/
                                     -- user_create/user_edit/user_delete
    actor           TEXT NOT NULL,   -- 'reconciler' | 'user'
    before_json     TEXT,
    after_json      TEXT,
    reconcile_run_id INTEGER REFERENCES memory_reconcile_runs(id) ON DELETE SET NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unit_ops_unit ON memory_unit_ops(unit_id, id);
CREATE INDEX IF NOT EXISTS idx_unit_ops_reconcile_run ON memory_unit_ops(reconcile_run_id);

CREATE TABLE IF NOT EXISTS memory_views (
    id                   TEXT PRIMARY KEY,       -- mv_<ulid>
    owner_scope          TEXT NOT NULL,
    visibility_scope     TEXT NOT NULL,
    view_type            TEXT NOT NULL,          -- 'user_portrait' | 'soul_relationship_memory'
    content_md           TEXT NOT NULL,
    source_unit_set_hash TEXT NOT NULL,
    renderer_version     TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'fresh'
                          CHECK(status IN ('fresh','stale','failed')),
    generated_at         REAL NOT NULL,
    updated_at           REAL NOT NULL,
    metadata             TEXT,
    UNIQUE(owner_scope, visibility_scope, view_type)
);

CREATE TABLE IF NOT EXISTS memory_view_units (
    view_id     TEXT NOT NULL REFERENCES memory_views(id) ON DELETE CASCADE,
    unit_id     TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    PRIMARY KEY (view_id, unit_id)
);
