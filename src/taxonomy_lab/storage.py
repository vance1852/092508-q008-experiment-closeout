"""分类实验观察采信服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_protocol_catalog (
    evidence_protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    task_family TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (evidence_protocol_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'statistician', 'approver', 'auditor', 'instructor', 'museum_officer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS capture_devices (
    device_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    vendor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES capture_devices(device_id),
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (device_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    evidence_protocol_id TEXT NOT NULL,
    evidence_protocol_version INTEGER NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    state TEXT NOT NULL CHECK (state IN ('draft', 'running', 'sealed', 'analyzing', 'analyzed', 'decided')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    sealed_at TEXT,
    FOREIGN KEY (evidence_protocol_id, evidence_protocol_version) REFERENCES evidence_protocol_catalog(evidence_protocol_id, version)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    source_batch TEXT NOT NULL,
    source_row TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES capture_devices(device_id),
    evidence_group_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL REFERENCES users(user_id),
    imported_at TEXT NOT NULL,
    UNIQUE (batch_id, source_batch, source_row)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS exclusion_requests (
    exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_item_id INTEGER NOT NULL REFERENCES evidence_items(evidence_item_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'revoked')),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES users(user_id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(user_id),
    reviewed_at TEXT,
    review_note TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_exclusion_per_evidence_item
ON exclusion_requests(evidence_item_id)
WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    batch_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_revision)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    batch_revision INTEGER NOT NULL,
    evidence_protocol_sha256 TEXT NOT NULL CHECK (length(evidence_protocol_sha256) = 64),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    algorithm_version TEXT NOT NULL,
    seed INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_revision, input_sha256)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    analysis_id INTEGER NOT NULL REFERENCES analyses(analysis_id),
    decision TEXT NOT NULL CHECK (decision IN ('needs_more_data', 'approved', 'rejected')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (batch_id, analysis_id)
);

-- 结项流程：批次材料台账 -----------------------------------------------------

CREATE TABLE IF NOT EXISTS materials (
    material_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    material_code TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('live_observation', 'temporary_slide', 'residual_reagent', 'accession_candidate')),
    description TEXT NOT NULL,
    initial_quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL,
    UNIQUE (batch_id, material_code)
);

CREATE TABLE IF NOT EXISTS material_consumptions (
    consumption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(material_id),
    quantity TEXT NOT NULL,
    reason TEXT NOT NULL,
    consumed_by TEXT NOT NULL REFERENCES users(user_id),
    consumed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closure_summaries (
    closure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    version INTEGER NOT NULL CHECK (version > 0),
    state TEXT NOT NULL CHECK (state IN (
        'submitted', 'instructor_confirmed', 'confirmed',
        'withdrawn', 'partially_returned', 'invalidated'
    )),
    evidence_protocol_sha256 TEXT NOT NULL CHECK (length(evidence_protocol_sha256) = 64),
    analysis_id INTEGER REFERENCES analyses(analysis_id),
    analysis_input_sha256 TEXT,
    participants_json TEXT NOT NULL,
    participants_digest TEXT NOT NULL CHECK (length(participants_digest) = 64),
    snapshot_json TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL CHECK (length(snapshot_digest) = 64),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    instructor_confirmed_by TEXT REFERENCES users(user_id),
    instructor_confirmed_at TEXT,
    museum_confirmed_by TEXT REFERENCES users(user_id),
    museum_confirmed_at TEXT,
    withdrawn_by TEXT REFERENCES users(user_id),
    withdrawn_at TEXT,
    withdrawn_reason TEXT,
    returned_at TEXT,
    invalidated_by TEXT REFERENCES users(user_id),
    invalidated_at TEXT,
    invalidation_reason TEXT,
    invalidation_category TEXT,
    UNIQUE (batch_id, version)
);

CREATE TABLE IF NOT EXISTS closure_materials (
    closure_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
    closure_id INTEGER NOT NULL REFERENCES closure_summaries(closure_id),
    material_id INTEGER NOT NULL REFERENCES materials(material_id),
    category TEXT NOT NULL,
    initial_quantity TEXT NOT NULL,
    consumed_quantity TEXT NOT NULL,
    accession_quantity TEXT NOT NULL,
    returned_quantity TEXT NOT NULL,
    destroyed_quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accession', 'return', 'destroy', 'mixed', 'consumed')),
    disposition_reason TEXT NOT NULL,
    UNIQUE (closure_id, material_id)
);

CREATE TABLE IF NOT EXISTS closure_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    closure_id INTEGER NOT NULL REFERENCES closure_summaries(closure_id),
    material_id INTEGER NOT NULL REFERENCES materials(material_id),
    field TEXT NOT NULL CHECK (field IN ('accession_quantity', 'returned_quantity', 'destroyed_quantity')),
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    reason TEXT NOT NULL,
    adjusted_by TEXT NOT NULL REFERENCES users(user_id),
    adjusted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closure_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    closure_id INTEGER NOT NULL REFERENCES closure_summaries(closure_id),
    party TEXT NOT NULL CHECK (party IN ('instructor', 'museum')),
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    replay_matched INTEGER NOT NULL CHECK (replay_matched IN (0, 1)),
    confirmed_at TEXT NOT NULL,
    UNIQUE (closure_id, party)
);

CREATE TABLE IF NOT EXISTS accession_records (
    accession_id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(material_id),
    closure_id INTEGER NOT NULL REFERENCES closure_summaries(closure_id),
    batch_id TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    catalog_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    accessioned_by TEXT NOT NULL REFERENCES users(user_id),
    accessioned_at TEXT NOT NULL,
    UNIQUE (closure_id, material_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_closures_batch ON closure_summaries(batch_id, version);
CREATE INDEX IF NOT EXISTS idx_closure_materials_closure ON closure_materials(closure_id);
CREATE INDEX IF NOT EXISTS idx_materials_batch ON materials(batch_id);
CREATE INDEX IF NOT EXISTS idx_consumptions_material ON material_consumptions(material_id);
CREATE INDEX IF NOT EXISTS idx_accessions_material ON accession_records(material_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "evidence_protocol_catalog", "users", "capture_devices", "builds", "batches",
    "evidence_items", "idempotency_keys", "exclusion_requests", "analysis_jobs",
    "analyses", "decisions", "audit_events",
    "materials", "material_consumptions", "closure_summaries", "closure_materials",
    "closure_confirmations", "accession_records",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
