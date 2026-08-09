from __future__ import annotations

import sqlite3
from typing import Any


def comment_from_name_version(name: str, version: str) -> str:
    """Default entity comment = report Имя + Версия."""
    parts = [((name or "").strip()), ((version or "").strip())]
    return " ".join(p for p in parts if p)


def list_entities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.*,
          (
            SELECT COUNT(*) FROM objects o
            WHERE o.entity_id=e.id AND o.kind IN ('Procedure','Function')
          ) AS bsl_method_count
        FROM entities e
        ORDER BY e.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_entity(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    return dict(row) if row else None


def get_entity_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_entity(
    conn: sqlite3.Connection,
    *,
    name: str,
    synonym: str,
    version: str,
    file_path: str,
    model: str,
    entity_type: str = "configuration",
    name_locked: bool = False,
) -> int:
    """Register or refresh entity for a new upload. Keeps existing objects for incremental parse.

    name_locked=True: keep this context name forever (do not rename from report meta).
    Used for multiple versions of the same configuration under different MCP contexts.
    """
    locked = 1 if name_locked else 0
    existing = get_entity_by_name(conn, name)
    if existing:
        if existing.get("model") != model:
            conn.execute(
                "UPDATE objects SET embed_done=0 WHERE entity_id=?",
                (existing["id"],),
            )
            conn.execute(
                "DELETE FROM pending_zvec_deletes WHERE entity_id=?",
                (existing["id"],),
            )
        # Explicit override locks the name; otherwise keep previous lock flag
        new_locked = 1 if name_locked else int(existing.get("name_locked") or 0)
        auto_comment = comment_from_name_version(name, version)
        old_auto = comment_from_name_version(existing.get("name") or "", existing.get("version") or "")
        prev_comment = (existing.get("comment") or "").strip()
        # Refresh auto comment unless user customized it
        new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, file_path=?, model=?,
              entity_type=?, name_locked=?, comment=?, status='uploaded', indexed_count=0, index_target=0,
              error_message='', updated_at=datetime('now')
            WHERE id=?
            """,
            (synonym, version, file_path, model, entity_type, new_locked, new_comment, existing["id"]),
        )
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO entities(name, synonym, comment, entity_type, version, file_path, model, name_locked, status)
        VALUES (?,?,?,?,?,?,?,?, 'uploaded')
        """,
        (
            name,
            synonym,
            comment_from_name_version(name, version),
            entity_type,
            version,
            file_path,
            model,
            locked,
        ),
    )
    return int(cur.lastrowid)


def refresh_entity_file(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    synonym: str,
    version: str,
    file_path: str,
    model: str,
    entity_type: str | None = None,
) -> int:
    """Replace report file for an existing entity (row upload / merge). Keeps name and name_locked."""
    existing = get_entity(conn, entity_id)
    if not existing:
        raise KeyError(f"entity {entity_id}")
    if existing.get("model") != model:
        conn.execute(
            "UPDATE objects SET embed_done=0 WHERE entity_id=?",
            (entity_id,),
        )
        conn.execute(
            "DELETE FROM pending_zvec_deletes WHERE entity_id=?",
            (entity_id,),
        )
    auto_comment = comment_from_name_version(existing.get("name") or "", version)
    old_auto = comment_from_name_version(
        existing.get("name") or "", existing.get("version") or ""
    )
    prev_comment = (existing.get("comment") or "").strip()
    new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment
    etype = (entity_type or existing.get("entity_type") or "configuration").strip() or "configuration"
    conn.execute(
        """
        UPDATE entities SET synonym=?, version=?, file_path=?, model=?, comment=?, entity_type=?,
          status='uploaded', indexed_count=0, index_target=0,
          error_message='', updated_at=datetime('now')
        WHERE id=?
        """,
        (synonym, version, file_path, model, new_comment, etype, entity_id),
    )
    return entity_id


def set_entity_type(conn: sqlite3.Connection, entity_id: int, entity_type: str) -> None:
    et = (entity_type or "").strip().lower()
    if et not in {"configuration", "extension"}:
        et = "configuration"
    conn.execute(
        "UPDATE entities SET entity_type=?, updated_at=datetime('now') WHERE id=?",
        (et, entity_id),
    )


def set_comment(conn: sqlite3.Connection, entity_id: int, comment: str) -> None:
    conn.execute(
        """
        UPDATE entities SET comment=?, updated_at=datetime('now') WHERE id=?
        """,
        ((comment or "").strip(), entity_id),
    )


_BUSY_STATUSES = frozenset({"parsing", "indexing", "uploaded", "loading_modules"})


def rename_entity(conn: sqlite3.Connection, entity_id: int, new_name: str) -> dict[str, Any]:
    """Rename MCP context. Sets name_locked=1. Files/zvec stay on entity id."""
    name = (new_name or "").strip()
    if not name:
        raise ValueError("Name must not be empty")
    if len(name) > 200:
        raise ValueError("Name is too long (max 200 characters)")
    row = get_entity(conn, entity_id)
    if not row:
        raise KeyError(f"entity {entity_id}")
    if row["status"] in _BUSY_STATUSES:
        raise ValueError(f"Entity is busy (status={row['status']})")
    old_name = row["name"]
    if old_name == name:
        return dict(row)
    other = get_entity_by_name(conn, name)
    if other and int(other["id"]) != int(entity_id):
        raise ValueError(f"Name already used: {name}")

    version = row.get("version") or ""
    auto_old = comment_from_name_version(old_name, version)
    auto_new = comment_from_name_version(name, version)
    prev = (row.get("comment") or "").strip()
    new_comment = auto_new if (not prev or prev == auto_old) else prev

    try:
        from app.services.search import invalidate_entity_cache

        invalidate_entity_cache(old_name)
    except Exception:
        pass

    conn.execute(
        """
        UPDATE entities
        SET name=?, name_locked=1, comment=?, updated_at=datetime('now')
        WHERE id=?
        """,
        (name, new_comment, entity_id),
    )
    try:
        from app.services.search import invalidate_entity_cache

        invalidate_entity_cache(name)
    except Exception:
        pass
    updated = get_entity(conn, entity_id)
    return dict(updated) if updated else {"id": entity_id, "name": name}


def set_status(
    conn: sqlite3.Connection,
    entity_id: int,
    status: str,
    *,
    error_message: str | None = None,
    object_count: int | None = None,
    link_count: int | None = None,
    indexed_count: int | None = None,
    index_target: int | None = None,
    index_started_at: float | None = None,
    index_scope: str | None = None,
    model: str | None = None,
    enabled: int | None = None,
    bsl_enabled: int | None = None,
    parse_gen: int | None = None,
    parse_added: int | None = None,
    parse_changed: int | None = None,
    parse_deleted: int | None = None,
    parse_unchanged: int | None = None,
) -> None:
    fields = ["status=?", "updated_at=datetime('now')"]
    vals: list[Any] = [status]
    mapping = [
        ("error_message", error_message),
        ("object_count", object_count),
        ("link_count", link_count),
        ("indexed_count", indexed_count),
        ("index_target", index_target),
        ("index_started_at", index_started_at),
        ("index_scope", index_scope),
        ("model", model),
        ("enabled", enabled),
        ("bsl_enabled", bsl_enabled),
        ("parse_gen", parse_gen),
        ("parse_added", parse_added),
        ("parse_changed", parse_changed),
        ("parse_deleted", parse_deleted),
        ("parse_unchanged", parse_unchanged),
    ]
    for col, val in mapping:
        if val is not None:
            fields.append(f"{col}=?")
            vals.append(val)
    vals.append(entity_id)
    conn.execute(f"UPDATE entities SET {', '.join(fields)} WHERE id=?", vals)
    _invalidate_search_cache(conn, entity_id)


def set_bsl_load_mode(conn: sqlite3.Connection, entity_id: int, mode: str) -> None:
    conn.execute(
        "UPDATE entities SET bsl_load_mode=?, updated_at=datetime('now') WHERE id=?",
        ((mode or "").strip(), entity_id),
    )


def set_bsl_embed_mode(conn: sqlite3.Connection, entity_id: int, mode: str) -> None:
    conn.execute(
        "UPDATE entities SET bsl_embed_mode=?, updated_at=datetime('now') WHERE id=?",
        ((mode or "").strip(), entity_id),
    )


def delete_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    _invalidate_search_cache(conn, entity_id)
    conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))


def _invalidate_search_cache(conn: sqlite3.Connection, entity_id: int) -> None:
    """Drop MCP resolve_context TTL entry for this entity (lazy import)."""
    try:
        row = conn.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
        name = row["name"] if row else None
        from app.services.search import invalidate_entity_cache

        invalidate_entity_cache(name)
    except Exception:
        pass


def list_ready_contexts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.synonym, e.version, e.model, e.object_count,
          (
            SELECT GROUP_CONCAT(t.name, char(31))
            FROM entity_tags et
            JOIN tags t ON t.id = et.tag_id
            WHERE et.entity_id = e.id
          ) AS tags_joined
        FROM entities e
        WHERE e.enabled=1 AND e.status='ready'
        ORDER BY e.name
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("tags_joined", None) or ""
        d["tags"] = [p for p in str(raw).split("\x1f") if p] if raw else []
        out.append(d)
    return out


def list_ready_entities_for_tag(conn: sqlite3.Connection, tag_name: str) -> list[dict[str, Any]]:
    """Ready+enabled entities that have the given tag (by tag name, case-sensitive match as stored)."""
    name = (tag_name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.synonym, e.version, e.model, e.object_count,
               e.enabled, e.status, e.bsl_enabled
        FROM entities e
        JOIN entity_tags et ON et.entity_id = e.id
        JOIN tags t ON t.id = et.tag_id
        WHERE e.enabled=1 AND e.status='ready' AND t.name = ?
        ORDER BY e.name
        """,
        (name,),
    ).fetchall()
    return [dict(r) for r in rows]
