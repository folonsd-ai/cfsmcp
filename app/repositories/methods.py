"""Query helpers for BSL method objects (Procedure/Function)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

def list_code_modules(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    kind: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """Parents that have at least one Procedure/Function child."""
    rows = conn.execute(
        """
        SELECT props_json FROM objects
        WHERE entity_id=? AND kind IN ('Procedure','Function')
        """,
        (entity_id,),
    ).fetchall()
    counts: dict[str, int] = {}
    roles: dict[str, set[str]] = {}
    for r in rows:
        try:
            props = json.loads(r["props_json"] or "{}")
        except json.JSONDecodeError:
            props = {}
        parent = props.get("parent_path") or ""
        if not parent:
            continue
        counts[parent] = counts.get(parent, 0) + 1
        roles.setdefault(parent, set()).add(str(props.get("module_role") or ""))

    if not counts:
        return []

    parents = list(counts.keys())
    by_path: dict[str, dict] = {}
    chunk = 400
    for i in range(0, len(parents), chunk):
        part = parents[i : i + chunk]
        ph = ",".join("?" * len(part))
        for r in conn.execute(
            f"SELECT path, kind, name, synonym FROM objects WHERE entity_id=? AND path IN ({ph})",
            (entity_id, *part),
        ).fetchall():
            by_path[r["path"]] = dict(r)

    qn = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for parent, cnt in sorted(counts.items(), key=lambda x: x[0].lower()):
        meta = by_path.get(parent) or {
            "path": parent,
            "kind": "",
            "name": parent.rsplit(".", 1)[-1],
            "synonym": "",
        }
        if kind and meta.get("kind") != kind:
            continue
        if qn and qn not in parent.lower() and qn not in (meta.get("name") or "").lower():
            continue
        out.append(
            {
                "path": parent,
                "kind": meta.get("kind") or "",
                "name": meta.get("name") or "",
                "methods_count": cnt,
                "module_roles": sorted(r for r in roles.get(parent, set()) if r),
            }
        )
    return out


def list_methods(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    parent_path: str | None = None,
    q: str | None = None,
    export_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT path, kind, name, synonym, comment, props_json
        FROM objects
        WHERE entity_id=? AND kind IN ('Procedure','Function')
        ORDER BY path
        """,
        (entity_id,),
    ).fetchall()
    qn = (q or "").strip().lower()
    parent_f = (parent_path or "").strip()
    items: list[dict[str, Any]] = []
    for r in rows:
        try:
            props = json.loads(r["props_json"] or "{}")
        except json.JSONDecodeError:
            props = {}
        parent = props.get("parent_path") or ""
        if parent_f and parent != parent_f:
            continue
        export = bool(props.get("export"))
        if export_only and not export:
            continue
        name = r["name"] or ""
        sig = props.get("signature") or r["synonym"] or ""
        doc = r["comment"] or ""
        if qn and not (
            qn in name.lower()
            or qn in sig.lower()
            or qn in doc.lower()
            or qn in (r["path"] or "").lower()
        ):
            continue
        preview = doc.replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:197] + "..."
        items.append(
            {
                "path": r["path"],
                "parent_path": parent,
                "name": name,
                "kind": r["kind"],
                "export": export,
                "signature": sig,
                "doc_preview": preview,
                "module_role": props.get("module_role") or "",
            }
        )
        if len(items) >= limit:
            break
    return items


def _row_to_method(r: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
    kind = r["kind"]
    if kind not in {"Procedure", "Function"}:
        return None
    try:
        props = json.loads(r["props_json"] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError):
        props = {}
    return {
        "path": r["path"],
        "parent_path": props.get("parent_path") or "",
        "name": r["name"] or "",
        "kind": kind,
        "export": bool(props.get("export")),
        "signature": props.get("signature") or r["synonym"] or "",
        "doc": r["comment"] or "",
        "body": props.get("body") or "",
        "load_mode": props.get("load_mode") or "signatures",
        "module_role": props.get("module_role") or "",
        "source_file": props.get("source_file") or "",
        "line": props.get("line"),
    }


def get_method(conn: sqlite3.Connection, entity_id: int, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT path, kind, name, synonym, comment, props_json
        FROM objects
        WHERE entity_id=? AND path=?
        """,
        (entity_id, path),
    ).fetchone()
    if not row:
        return None
    return _row_to_method(row)


def get_methods_by_paths(
    conn: sqlite3.Connection,
    entity_id: int,
    paths: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-load Procedure/Function rows keyed by path (order of paths ignored)."""
    uniq = [p for p in dict.fromkeys(paths) if p]
    if not uniq:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk = 400
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        ph = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT path, kind, name, synonym, comment, props_json
            FROM objects
            WHERE entity_id=? AND path IN ({ph}) AND kind IN ('Procedure','Function')
            """,
            (entity_id, *part),
        ).fetchall()
        for r in rows:
            method = _row_to_method(r)
            if method:
                out[method["path"]] = method
    return out
