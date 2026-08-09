from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  color TEXT NOT NULL DEFAULT '#64748b',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  name_locked INTEGER NOT NULL DEFAULT 0,
  synonym TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  entity_type TEXT NOT NULL DEFAULT 'configuration',
  version TEXT NOT NULL DEFAULT '',
  file_path TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  bsl_enabled INTEGER NOT NULL DEFAULT 1,
  bsl_load_mode TEXT NOT NULL DEFAULT '',
  bsl_embed_mode TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'uploaded',
  object_count INTEGER NOT NULL DEFAULT 0,
  link_count INTEGER NOT NULL DEFAULT 0,
  indexed_count INTEGER NOT NULL DEFAULT 0,
  index_target INTEGER NOT NULL DEFAULT 0,
  index_started_at REAL NOT NULL DEFAULT 0,
  parse_gen INTEGER NOT NULL DEFAULT 0,
  parse_added INTEGER NOT NULL DEFAULT 0,
  parse_changed INTEGER NOT NULL DEFAULT 0,
  parse_deleted INTEGER NOT NULL DEFAULT 0,
  parse_unchanged INTEGER NOT NULL DEFAULT 0,
  error_message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entity_tags (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (entity_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_tags_tag ON entity_tags(tag_id);

CREATE TABLE IF NOT EXISTS objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  kind_ru TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  synonym TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  belong TEXT NOT NULL DEFAULT 'Own',
  base_object TEXT NOT NULL DEFAULT '',
  props_json TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL DEFAULT '',
  parse_gen INTEGER NOT NULL DEFAULT 0,
  embed_done INTEGER NOT NULL DEFAULT 0,
  UNIQUE(entity_id, path)
);

CREATE INDEX IF NOT EXISTS idx_objects_entity ON objects(entity_id);
CREATE INDEX IF NOT EXISTS idx_objects_kind ON objects(entity_id, kind);
CREATE INDEX IF NOT EXISTS idx_objects_embed ON objects(entity_id, embed_done);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  from_path TEXT NOT NULL,
  to_ref TEXT NOT NULL,
  link_type TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_links_from ON links(entity_id, from_path);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(entity_id, to_ref);

CREATE TABLE IF NOT EXISTS pending_zvec_deletes (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  doc_id TEXT NOT NULL,
  PRIMARY KEY (entity_id, doc_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MIGRATIONS = [
    ("entities", "index_target", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "index_started_at", "REAL NOT NULL DEFAULT 0"),
    ("entities", "name_locked", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "parse_gen", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "parse_added", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "parse_changed", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "parse_deleted", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "parse_unchanged", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "bsl_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("entities", "bsl_load_mode", "TEXT NOT NULL DEFAULT ''"),
    ("entities", "bsl_embed_mode", "TEXT NOT NULL DEFAULT ''"),
    ("entities", "comment", "TEXT NOT NULL DEFAULT ''"),
    ("entities", "index_scope", "TEXT NOT NULL DEFAULT ''"),
    ("objects", "content_hash", "TEXT NOT NULL DEFAULT ''"),
    ("objects", "parse_gen", "INTEGER NOT NULL DEFAULT 0"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection) -> None:
    added_comment = False
    for table, column, decl in _MIGRATIONS:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            if table == "entities" and column == "comment":
                added_comment = True
    if added_comment or (
        _column_exists(conn, "entities", "comment")
        and conn.execute(
            "SELECT 1 FROM entities WHERE IFNULL(comment,'')='' LIMIT 1"
        ).fetchone()
    ):
        # Seed comment from report Имя + Версия for empty rows
        conn.execute(
            """
            UPDATE entities
            SET comment = trim(name || CASE WHEN IFNULL(version,'')='' THEN '' ELSE ' ' || version END)
            WHERE IFNULL(comment,'')=''
            """
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_zvec_deletes (
          entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          doc_id TEXT NOT NULL,
          PRIMARY KEY (entity_id, doc_id)
        )
        """
    )
    if _column_exists(conn, "objects", "parse_gen"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_parse_gen ON objects(entity_id, parse_gen)"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          color TEXT NOT NULL DEFAULT '#64748b',
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if _table_exists(conn, "tags") and not _column_exists(conn, "tags", "color"):
        conn.execute(
            "ALTER TABLE tags ADD COLUMN color TEXT NOT NULL DEFAULT '#64748b'"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_tags (
          entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
          PRIMARY KEY (entity_id, tag_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_tags_tag ON entity_tags(tag_id)"
    )
    conn.commit()


_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _db_key(db_path: Path) -> str:
    try:
        return str(db_path.resolve())
    except Exception:
        return str(db_path)


def init_db(db_path: Path) -> None:
    """Apply SCHEMA + migrations once per process/DB path."""
    key = _db_key(db_path)
    with _init_lock:
        if key in _initialized_paths:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            migrate(conn)
            _initialized_paths.add(key)
        finally:
            conn.close()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection. Schema/migrate run only via init_db (lazy once if needed)."""
    key = _db_key(db_path)
    if key not in _initialized_paths:
        init_db(db_path)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # foreign_keys is per-connection; WAL persists on the DB file after init.
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
