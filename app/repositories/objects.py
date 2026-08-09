from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable


def content_hash(fields: dict[str, Any]) -> str:
    """Stable hash of fields that affect search / embeddings."""
    props = fields.get("props") or {}
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except json.JSONDecodeError:
            props = {}
    payload = {
        "path": fields.get("path") or "",
        "kind": fields.get("kind") or "",
        "name": fields.get("name") or "",
        "synonym": fields.get("synonym") or "",
        "comment": fields.get("comment") or "",
        "belong": fields.get("belong") or "Own",
        "base_object": fields.get("base_object") or "",
        "props": props,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_path_index(conn: sqlite3.Connection, entity_id: int) -> dict[str, tuple[int, str]]:
    """path -> (id, content_hash). Prefer lookup_paths for streaming parse."""
    rows = conn.execute(
        "SELECT id, path, content_hash FROM objects WHERE entity_id=?",
        (entity_id,),
    ).fetchall()
    return {r["path"]: (int(r["id"]), r["content_hash"] or "") for r in rows}


def lookup_paths(
    conn: sqlite3.Connection,
    entity_id: int,
    paths: list[str],
) -> dict[str, tuple[int, str]]:
    """Batch path -> (id, content_hash) without loading the whole entity index."""
    if not paths:
        return {}
    out: dict[str, tuple[int, str]] = {}
    # SQLite variable limit is typically 999 — chunk IN lists
    chunk = 400
    for i in range(0, len(paths), chunk):
        part = paths[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT id, path, content_hash FROM objects
            WHERE entity_id=? AND path IN ({placeholders})
            """,
            (entity_id, *part),
        ).fetchall()
        for r in rows:
            out[r["path"]] = (int(r["id"]), r["content_hash"] or "")
    return out


def insert_objects_batch(conn: sqlite3.Connection, entity_id: int, rows: Iterable[dict[str, Any]]) -> int:
    """Legacy full insert (embed_done=0). Prefer merge_objects_batch for incremental parse."""
    data = []
    for r in rows:
        props_json = json.dumps(r.get("props") or {}, ensure_ascii=False)
        ch = r.get("content_hash") or content_hash(r)
        data.append(
            (
                entity_id,
                r["path"],
                r["kind_ru"],
                r["kind"],
                r["name"],
                r.get("synonym") or "",
                r.get("comment") or "",
                r.get("belong") or "Own",
                r.get("base_object") or "",
                props_json,
                ch,
                int(r.get("parse_gen") or 0),
                0,
            )
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO objects(
          entity_id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
          props_json, content_hash, parse_gen, embed_done
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        data,
    )
    return len(data)


def insert_new_objects(conn: sqlite3.Connection, entity_id: int, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    data = []
    for r in rows:
        props_json = json.dumps(r.get("props") or {}, ensure_ascii=False)
        ch = r.get("content_hash") or content_hash(r)
        data.append(
            (
                entity_id,
                r["path"],
                r["kind_ru"],
                r["kind"],
                r["name"],
                r.get("synonym") or "",
                r.get("comment") or "",
                r.get("belong") or "Own",
                r.get("base_object") or "",
                props_json,
                ch,
                int(r["parse_gen"]),
                0,
            )
        )
    conn.executemany(
        """
        INSERT INTO objects(
          entity_id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
          props_json, content_hash, parse_gen, embed_done
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(entity_id, path) DO UPDATE SET
          kind_ru=excluded.kind_ru,
          kind=excluded.kind,
          name=excluded.name,
          synonym=excluded.synonym,
          comment=excluded.comment,
          belong=excluded.belong,
          base_object=excluded.base_object,
          props_json=excluded.props_json,
          content_hash=excluded.content_hash,
          parse_gen=excluded.parse_gen,
          embed_done=0
        """,
        data,
    )
    return len(data)


def update_changed_objects(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    data = []
    for r in rows:
        props_json = json.dumps(r.get("props") or {}, ensure_ascii=False)
        ch = r.get("content_hash") or content_hash(r)
        data.append(
            (
                r["kind_ru"],
                r["kind"],
                r["name"],
                r.get("synonym") or "",
                r.get("comment") or "",
                r.get("belong") or "Own",
                r.get("base_object") or "",
                props_json,
                ch,
                int(r["parse_gen"]),
                0,
                int(r["id"]),
            )
        )
    conn.executemany(
        """
        UPDATE objects SET
          kind_ru=?, kind=?, name=?, synonym=?, comment=?, belong=?, base_object=?,
          props_json=?, content_hash=?, parse_gen=?, embed_done=?
        WHERE id=?
        """,
        data,
    )
    return len(data)


def touch_unchanged_objects(conn: sqlite3.Connection, ids: list[int], parse_gen: int) -> int:
    if not ids:
        return 0
    conn.executemany(
        "UPDATE objects SET parse_gen=? WHERE id=?",
        [(parse_gen, i) for i in ids],
    )
    return len(ids)


def _queue_zvec_deletes(conn: sqlite3.Connection, entity_id: int, ids: list[int]) -> None:
    from app.services.bsl_embed import zvec_doc_ids_for_object

    rows = [
        (entity_id, doc_id)
        for i in ids
        for doc_id in zvec_doc_ids_for_object(i)
    ]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO pending_zvec_deletes(entity_id, doc_id) VALUES (?,?)",
            rows,
        )


def delete_stale_objects(
    conn: sqlite3.Connection,
    entity_id: int,
    parse_gen: int,
    *,
    kinds_only: tuple[str, ...] | None = None,
    exclude_kinds: tuple[str, ...] | None = None,
) -> list[int]:
    """Delete objects not seen in this parse; queue their ids for zvec delete. Returns deleted ids."""
    sql = "SELECT id FROM objects WHERE entity_id=? AND parse_gen < ?"
    params: list[Any] = [entity_id, parse_gen]
    if kinds_only:
        placeholders = ",".join("?" * len(kinds_only))
        sql += f" AND kind IN ({placeholders})"
        params.extend(kinds_only)
    if exclude_kinds:
        placeholders = ",".join("?" * len(exclude_kinds))
        sql += f" AND kind NOT IN ({placeholders})"
        params.extend(exclude_kinds)
    rows = conn.execute(sql, params).fetchall()
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return []
    _queue_zvec_deletes(conn, entity_id, ids)
    conn.executemany("DELETE FROM objects WHERE id=?", [(i,) for i in ids])
    return ids


def list_pending_zvec_deletes(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT doc_id FROM pending_zvec_deletes WHERE entity_id=?",
        (entity_id,),
    ).fetchall()
    return [r["doc_id"] for r in rows]


def clear_pending_zvec_deletes(conn: sqlite3.Connection, entity_id: int) -> None:
    conn.execute("DELETE FROM pending_zvec_deletes WHERE entity_id=?", (entity_id,))


def delete_method_objects(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """Remove all Procedure/Function objects and queue zvec doc deletes."""
    rows = conn.execute(
        "SELECT id FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
        (entity_id,),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    return delete_objects_by_ids(conn, entity_id, ids)


def method_parent_path(path: str, props_json: str | None) -> str:
    """Resolve metadata parent path for a BSL method object."""
    import json

    try:
        props = json.loads(props_json or "{}")
    except Exception:
        props = {}
    parent = str(props.get("parent_path") or "").strip()
    if parent:
        return parent
    marker = ".Методы."
    p = path or ""
    idx = p.find(marker)
    return p[:idx] if idx > 0 else ""


def list_orphan_method_ids(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """BSL methods whose parent metadata object is missing (deleted from report)."""
    parents = {
        str(r["path"])
        for r in conn.execute(
            """
            SELECT path FROM objects
            WHERE entity_id=? AND kind NOT IN ('Procedure','Function')
            """,
            (entity_id,),
        ).fetchall()
    }
    rows = conn.execute(
        """
        SELECT id, path, props_json FROM objects
        WHERE entity_id=? AND kind IN ('Procedure','Function')
        """,
        (entity_id,),
    ).fetchall()
    orphan: list[int] = []
    for r in rows:
        parent = method_parent_path(str(r["path"] or ""), r["props_json"])
        if not parent or parent not in parents:
            orphan.append(int(r["id"]))
    return orphan


def count_orphan_methods(conn: sqlite3.Connection, entity_id: int) -> int:
    return len(list_orphan_method_ids(conn, entity_id))


def delete_objects_by_ids(
    conn: sqlite3.Connection, entity_id: int, ids: list[int]
) -> list[int]:
    if not ids:
        return []
    _queue_zvec_deletes(conn, entity_id, ids)
    conn.executemany("DELETE FROM objects WHERE id=?", [(i,) for i in ids])
    return ids


def delete_orphan_method_objects(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """Remove BSL methods without a living parent metadata object."""
    return delete_objects_by_ids(conn, entity_id, list_orphan_method_ids(conn, entity_id))


def count_embed_pending(conn: sqlite3.Connection, entity_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? AND embed_done=0",
        (entity_id,),
    ).fetchone()
    return int(row["c"] if row else 0)


def count_embed_pending_split(conn: sqlite3.Connection, entity_id: int) -> tuple[int, int]:
    """Return (meta_pending, method_pending) for embed_done=0 rows."""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN kind IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS methods,
          COALESCE(SUM(CASE WHEN kind NOT IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS meta
        FROM objects
        WHERE entity_id=? AND embed_done=0
        """,
        (entity_id,),
    ).fetchone()
    return int(row["meta"] if row else 0), int(row["methods"] if row else 0)


def count_objects_split(conn: sqlite3.Connection, entity_id: int) -> tuple[int, int]:
    """Return (meta_count, method_count) for all objects of an entity."""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN kind IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS methods,
          COALESCE(SUM(CASE WHEN kind NOT IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS meta
        FROM objects
        WHERE entity_id=?
        """,
        (entity_id,),
    ).fetchone()
    return int(row["meta"] if row else 0), int(row["methods"] if row else 0)


def insert_links_batch(conn: sqlite3.Connection, entity_id: int, links: Iterable[tuple[str, str, str]]) -> int:
    data = [(entity_id, a, b, c) for a, b, c in links]
    conn.executemany(
        "INSERT INTO links(entity_id, from_path, to_ref, link_type) VALUES (?,?,?,?)",
        data,
    )
    return len(data)


def get_object(conn: sqlite3.Connection, entity_id: int, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM objects WHERE entity_id=? AND path=?",
        (entity_id, path),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["props"] = json.loads(d.pop("props_json") or "{}")
    return d


def mark_embedded(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    conn.executemany("UPDATE objects SET embed_done=1 WHERE id=?", [(i,) for i in ids])


def reset_embed_flags(conn: sqlite3.Connection, entity_id: int) -> None:
    conn.execute("UPDATE objects SET embed_done=0 WHERE entity_id=?", (entity_id,))


def reset_method_embed_flags(conn: sqlite3.Connection, entity_id: int | None = None) -> int:
    """Mark Procedure/Function rows for re-embed after BSL embed mode change."""
    if entity_id is None:
        cur = conn.execute(
            "UPDATE objects SET embed_done=0 WHERE kind IN ('Procedure','Function')"
        )
    else:
        cur = conn.execute(
            "UPDATE objects SET embed_done=0 WHERE entity_id=? AND kind IN ('Procedure','Function')",
            (entity_id,),
        )
    return int(cur.rowcount or 0)


def get_links(
    conn: sqlite3.Connection,
    entity_id: int,
    path: str,
    direction: str = "both",
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"outgoing": [], "incoming": []}
    if direction in ("out", "both", "outgoing"):
        rows = conn.execute(
            "SELECT from_path, to_ref, link_type FROM links WHERE entity_id=? AND from_path=?",
            (entity_id, path),
        ).fetchall()
        out["outgoing"] = [dict(r) for r in rows]
    if direction in ("in", "both", "incoming"):
        rows = conn.execute(
            """
            SELECT from_path, to_ref, link_type FROM links
            WHERE entity_id=? AND (to_ref=? OR to_ref LIKE ?)
            """,
            (entity_id, path, f"%{path.split('.', 1)[-1] if '.' in path else path}"),
        ).fetchall()
        name = path.split(".")[-1]
        rows2 = conn.execute(
            "SELECT from_path, to_ref, link_type FROM links WHERE entity_id=? AND to_ref LIKE ?",
            (entity_id, f"%.{name}"),
        ).fetchall()
        seen = set()
        merged = []
        for r in list(rows) + list(rows2):
            key = (r["from_path"], r["to_ref"], r["link_type"])
            if key not in seen:
                seen.add(key)
                merged.append(dict(r))
        out["incoming"] = merged
    return out


def embedding_text(obj: dict[str, Any]) -> str:
    """Legacy single-passage text (meta only). Prefer bsl_embed.passages_for_object."""
    from app.services.bsl_embed import meta_text

    return meta_text(obj)