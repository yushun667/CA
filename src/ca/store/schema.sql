-- CA SQLite schema (core SCHEMA_VERSION 1 / store_schema_version 4), ca.store.
--
-- meta["schema_version"] tracks the ca.core.schema contract; meta
-- ["store_schema_version"] tracks this physical layout. Additive tables are
-- created with IF NOT EXISTS on open(), and finalize() adds the indexes, so a
-- phase-2 database upgrades in place without a destructive migration.
--
-- One table per record type in ca.core.schema, plus `meta` and `tus`.
-- Source ranges are flattened into (file, line, col, end_line, end_col)
-- columns because queries always filter/order by file+line. Variable-length
-- tuple fields are stored as JSON arrays (it is cheaper than child tables for
-- the query workload, which never filters on them). Primary keys are the
-- stable TEXT ids from ca.core.ids; duplicates are ignored at write time.
--
-- No FOREIGN KEY constraints: writers run in parallel batches and must never
-- pay per-row constraint checks during bulk load. Referential integrity is
-- maintained by the pipeline (all references come from the same parse tree)
-- and repaired for unresolved callees in finalize() by inserting placeholder
-- `functions` rows.
--
-- Secondary indexes are intentionally NOT created here: they are built once in
-- SqliteStore.finalize() after the bulk load, which is much faster than
-- maintaining them per INSERT. Only the implicit primary-key indexes exist
-- while writing.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tus (
    id        TEXT PRIMARY KEY,
    file      TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL DEFAULT '',
    repo_root TEXT NOT NULL DEFAULT '',
    arguments TEXT NOT NULL DEFAULT '[]',  -- JSON array of argv strings
    language  TEXT NOT NULL DEFAULT 'c',
    source    TEXT NOT NULL DEFAULT '',
    driver    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS functions (
    id             TEXT PRIMARY KEY,
    tu_id          TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL DEFAULT '',
    qualified_name TEXT NOT NULL DEFAULT '',
    usr            TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'unknown',
    file           TEXT NOT NULL DEFAULT '',
    line           INTEGER NOT NULL DEFAULT 0,
    col            INTEGER NOT NULL DEFAULT 0,
    end_line       INTEGER NOT NULL DEFAULT 0,
    end_col        INTEGER NOT NULL DEFAULT 0,
    is_definition  INTEGER NOT NULL DEFAULT 0,
    return_type    TEXT NOT NULL DEFAULT '',
    signature      TEXT NOT NULL DEFAULT '',
    display_name   TEXT NOT NULL DEFAULT '',
    storage_class  TEXT NOT NULL DEFAULT '',
    linkage        TEXT NOT NULL DEFAULT 'unknown',
    is_static      INTEGER NOT NULL DEFAULT 0,
    is_inline      INTEGER NOT NULL DEFAULT 0,
    is_variadic    INTEGER NOT NULL DEFAULT 0,
    parent_id      TEXT NOT NULL DEFAULT '',
    -- ParamRecord list as JSON (no stable id exists for params; the task
    -- allows an optional JSON column instead of a child table).
    params_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS types (
    id             TEXT PRIMARY KEY,
    tu_id          TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL DEFAULT '',
    qualified_name TEXT NOT NULL DEFAULT '',
    usr            TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'unknown',
    file           TEXT NOT NULL DEFAULT '',
    line           INTEGER NOT NULL DEFAULT 0,
    col            INTEGER NOT NULL DEFAULT 0,
    end_line       INTEGER NOT NULL DEFAULT 0,
    end_col        INTEGER NOT NULL DEFAULT 0,
    is_definition  INTEGER NOT NULL DEFAULT 0,
    size_bytes     INTEGER NOT NULL DEFAULT -1,
    align_bytes    INTEGER NOT NULL DEFAULT -1,
    underlying_type TEXT NOT NULL DEFAULT '',
    parent_id      TEXT NOT NULL DEFAULT '',
    is_anonymous   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fields (
    id           TEXT PRIMARY KEY,
    tu_id        TEXT NOT NULL DEFAULT '',
    type_id      TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    type_spelling TEXT NOT NULL DEFAULT '',
    file         TEXT NOT NULL DEFAULT '',
    line         INTEGER NOT NULL DEFAULT 0,
    col          INTEGER NOT NULL DEFAULT 0,
    end_line     INTEGER NOT NULL DEFAULT 0,
    end_col      INTEGER NOT NULL DEFAULT 0,
    offset_bits  INTEGER NOT NULL DEFAULT -1,
    bit_width    INTEGER NOT NULL DEFAULT -1,
    is_static    INTEGER NOT NULL DEFAULT 0,
    is_bitfield  INTEGER NOT NULL DEFAULT 0,
    "index"      INTEGER NOT NULL DEFAULT 0,
    access       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS calls (
    id                   TEXT PRIMARY KEY,
    tu_id                TEXT NOT NULL DEFAULT '',
    caller_id            TEXT NOT NULL DEFAULT '',
    file                 TEXT NOT NULL DEFAULT '',
    line                 INTEGER NOT NULL DEFAULT 0,
    col                  INTEGER NOT NULL DEFAULT 0,
    end_line             INTEGER NOT NULL DEFAULT 0,
    end_col              INTEGER NOT NULL DEFAULT 0,
    callee_id            TEXT NOT NULL DEFAULT '',
    -- Redundant columns kept on the call row so callers/callees joins and
    -- unresolved-callee queries never need a second lookup.
    callee_usr           TEXT NOT NULL DEFAULT '',
    callee_name          TEXT NOT NULL DEFAULT '',
    callee_qualified_name TEXT NOT NULL DEFAULT '',
    kind                 TEXT NOT NULL DEFAULT 'unknown',
    is_resolved          INTEGER NOT NULL DEFAULT 0,
    is_virtual           INTEGER NOT NULL DEFAULT 0,
    is_macro_expansion   INTEGER NOT NULL DEFAULT 0,
    arg_count            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cfg_blocks (
    id              TEXT PRIMARY KEY,
    function_id     TEXT NOT NULL DEFAULT '',
    "index"         INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'normal',
    terminator      TEXT NOT NULL DEFAULT 'none',
    file            TEXT NOT NULL DEFAULT '',
    line            INTEGER NOT NULL DEFAULT 0,
    col             INTEGER NOT NULL DEFAULT 0,
    end_line        INTEGER NOT NULL DEFAULT 0,
    end_col         INTEGER NOT NULL DEFAULT 0,
    statement_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cfg_edges (
    id          TEXT PRIMARY KEY,
    function_id TEXT NOT NULL DEFAULT '',
    src         TEXT NOT NULL DEFAULT '',
    dst         TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'unconditional',
    label       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS branches (
    id          TEXT PRIMARY KEY,
    function_id TEXT NOT NULL DEFAULT '',
    block_id    TEXT NOT NULL DEFAULT '',
    file        TEXT NOT NULL DEFAULT '',
    line        INTEGER NOT NULL DEFAULT 0,
    col         INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    end_col     INTEGER NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL DEFAULT 'if',
    condition   TEXT NOT NULL DEFAULT '',
    var_names   TEXT NOT NULL DEFAULT '[]',  -- JSON array
    true_block  TEXT NOT NULL DEFAULT '',
    false_block TEXT NOT NULL DEFAULT '',
    is_negated  INTEGER NOT NULL DEFAULT 0,
    case_value  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS assignments (
    id           TEXT PRIMARY KEY,
    function_id  TEXT NOT NULL DEFAULT '',
    block_id     TEXT NOT NULL DEFAULT '',
    file         TEXT NOT NULL DEFAULT '',
    line         INTEGER NOT NULL DEFAULT 0,
    col          INTEGER NOT NULL DEFAULT 0,
    end_line     INTEGER NOT NULL DEFAULT 0,
    end_col      INTEGER NOT NULL DEFAULT 0,
    lhs          TEXT NOT NULL DEFAULT '',
    rhs          TEXT NOT NULL DEFAULT '',
    var_names    TEXT NOT NULL DEFAULT '[]',  -- JSON array
    is_decl_init INTEGER NOT NULL DEFAULT 0,
    "operator"   TEXT NOT NULL DEFAULT '=',
    type_spelling TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS returns (
    id          TEXT PRIMARY KEY,
    function_id TEXT NOT NULL DEFAULT '',
    block_id    TEXT NOT NULL DEFAULT '',
    file        TEXT NOT NULL DEFAULT '',
    line        INTEGER NOT NULL DEFAULT 0,
    col         INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    end_col     INTEGER NOT NULL DEFAULT 0,
    expr        TEXT NOT NULL DEFAULT '',
    var_names   TEXT NOT NULL DEFAULT '[]',  -- JSON array
    is_implicit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS resource_ops (
    id            TEXT PRIMARY KEY,
    function_id   TEXT NOT NULL DEFAULT '',
    block_id      TEXT NOT NULL DEFAULT '',
    file          TEXT NOT NULL DEFAULT '',
    line          INTEGER NOT NULL DEFAULT 0,
    col           INTEGER NOT NULL DEFAULT 0,
    end_line      INTEGER NOT NULL DEFAULT 0,
    end_col       INTEGER NOT NULL DEFAULT 0,
    op            TEXT NOT NULL DEFAULT 'other',
    resource_kind TEXT NOT NULL DEFAULT 'other',
    api_name      TEXT NOT NULL DEFAULT '',
    target        TEXT NOT NULL DEFAULT '',
    target_vars   TEXT NOT NULL DEFAULT '[]',  -- JSON array
    call_id       TEXT NOT NULL DEFAULT '',
    matcher       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    function_id   TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'other',
    resource_kind TEXT NOT NULL DEFAULT 'other',
    message       TEXT NOT NULL DEFAULT '',
    severity      TEXT NOT NULL DEFAULT 'warning',
    acquire_id    TEXT NOT NULL DEFAULT '',
    release_ids   TEXT NOT NULL DEFAULT '[]',  -- JSON array
    path_blocks   TEXT NOT NULL DEFAULT '[]',  -- JSON array
    evidence      TEXT NOT NULL DEFAULT '[]'   -- JSON array
);

-- Secondary indexes are created in finalize(); see writer.py.

-- SQLite FTS5 symbol search (optional, created by finalize()):
--
--   CREATE VIRTUAL TABLE functions_fts USING fts5(
--       name, qualified_name,
--       content='functions', content_rowid='rowid', tokenize='trigram');
--
-- It is not declared here because open() must work on SQLite builds without
-- FTS5: finalize() creates/populates it best-effort and queries fall back to
-- LIKE when meta.fts_ready != '1'. The trigram tokenizer (SQLite >= 3.34) is
-- required for arbitrary substring search; see writer.py for the rationale.

-- Phase-3 records and multi-repo metadata (store schema version 2; additive).

CREATE TABLE IF NOT EXISTS call_targets (
    id          TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL DEFAULT '',
    caller_id   TEXT NOT NULL DEFAULT '',
    callee_id   TEXT NOT NULL DEFAULT '',
    callee_name TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'indirect_candidate',
    confidence  REAL NOT NULL DEFAULT 0.0,
    reason      TEXT NOT NULL DEFAULT '',
    file        TEXT NOT NULL DEFAULT '',
    line        INTEGER NOT NULL DEFAULT 0,
    col         INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    end_col     INTEGER NOT NULL DEFAULT 0
);

-- One workspace (a checked-out multi-repo tree) owns projects and variants.
-- OpenHarmony example: path=/home/yu/CA_OH/openharmony, 500 .repo projects,
-- variant product=opi5plus board=rk3588 version=<current release>.
CREATE TABLE IF NOT EXISTS workspaces (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'local',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    repo          TEXT NOT NULL DEFAULT '',
    product       TEXT NOT NULL DEFAULT '',
    board         TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'repo',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS variants (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    product       TEXT NOT NULL DEFAULT '',
    board         TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    db_path       TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

-- Per-function deep-analysis status (layered indexing / on-demand deep pass).
-- Base (L0) records live in the tables above and never depend on this table;
-- a missing row means "pending" (implicit pending), so an L0-only database can
-- answer symbol/call/structure queries without a deep pass.
CREATE TABLE IF NOT EXISTS deep_status (
    function_id  TEXT PRIMARY KEY,
    tu_id        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | ready | failed
    analyzed_at  TEXT NOT NULL DEFAULT '',         -- ISO-8601 UTC
    tool_version TEXT NOT NULL DEFAULT '',
    source_hash  TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    facts_json   TEXT NOT NULL DEFAULT '{}'        -- optional per-kind fact counts
);

-- Cross-TU callback / function-pointer registration facts (store schema v4).
-- A fact is a function reference stored/registered at an object path; it is
-- independent of any call site. The cross-TU resolver joins these with
-- indirect call sites to emit candidate call_targets; callback_target_links
-- records which candidate edges a registration fact produced.
CREATE TABLE IF NOT EXISTS callback_registrations (
    id                   TEXT PRIMARY KEY,
    object_path          TEXT NOT NULL DEFAULT '',  -- e.g. g_ops.read / ops->cb
    object_field         TEXT NOT NULL DEFAULT '',  -- last path component
    target_function_id   TEXT NOT NULL DEFAULT '',
    target_function_name TEXT NOT NULL DEFAULT '',
    target_usr           TEXT NOT NULL DEFAULT '',
    file                 TEXT NOT NULL DEFAULT '',
    line                 INTEGER NOT NULL DEFAULT 0,
    col                  INTEGER NOT NULL DEFAULT 0,
    end_line             INTEGER NOT NULL DEFAULT 0,
    end_col              INTEGER NOT NULL DEFAULT 0,
    tu_id                TEXT NOT NULL DEFAULT '',
    registration_kind    TEXT NOT NULL DEFAULT 'unknown',
    confidence           REAL NOT NULL DEFAULT 0.5,
    evidence_json        TEXT NOT NULL DEFAULT '[]',
    module               TEXT NOT NULL DEFAULT '',
    directory            TEXT NOT NULL DEFAULT '',
    registrar_function_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS callback_target_links (
    registration_id TEXT NOT NULL DEFAULT '',
    call_target_id  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (registration_id, call_target_id)
);
