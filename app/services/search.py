from __future__ import annotations

import threading
import time

import sqlite3
import zvec

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import methods as method_repo
from app.repositories import objects as obj_repo
from app.services.embeddings import EmbeddingClient
from app.services.pipeline import collection_path
from app.services.zvec_store import zvec_store

# Short TTL cache for resolved MCP contexts (name → entity row).
_ENTITY_TTL_SEC = 5.0
_entity_cache: dict[str, tuple[float, dict]] = {}
_entity_cache_lock = threading.Lock()

TAG_PREFIX = "tag:"


def invalidate_entity_cache(name: str | None = None) -> None:
    """Drop cached context lookup. Pass name or None to clear all."""
    with _entity_cache_lock:
        if name is None:
            _entity_cache.clear()
        else:
            _entity_cache.pop(name, None)


def parse_context_ref(raw: str) -> tuple[str, str]:
    """Return ('name', entity_name) or ('tag', tag_name)."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("Context must not be empty")
    if s.lower().startswith(TAG_PREFIX):
        tag = s[len(TAG_PREFIX) :].strip()
        if not tag:
            raise ValueError("Empty tag after 'tag:'. Example: tag:КА2")
        return "tag", tag
    return "name", s


def _open_collection(entity: dict):
    path = collection_path(entity["id"], entity["model"])
    if not path.exists():
        raise FileNotFoundError(
            f"Index for context '{entity['name']}' not found at {path}. Run reindex in UI."
        )
    return zvec_store.get(path, read_only=True)


def _dedupe_hits_by_path(hits: list[dict]) -> list[dict]:
    """Keep best (first) hit per path — chunk mode may return several docs for one method."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        path = str(h.get("path") or "")
        if path in seen:
            continue
        seen.add(path)
        out.append(h)
    return out


def _dedupe_hits_by_context_path(hits: list[dict]) -> list[dict]:
    """Keep best hit per (context, path) for federated search."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for h in hits:
        key = (str(h.get("context") or ""), str(h.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _filter_expr(
    *,
    kind: str | None,
    include_borrowed: bool,
    exclude_methods: bool = False,
) -> str | None:
    parts = []
    if kind:
        parts.append(f"kind = '{kind}'")
    if not include_borrowed:
        parts.append("belong = 'Own'")
    if exclude_methods:
        parts.append("kind != 'Procedure'")
        parts.append("kind != 'Function'")
    return " AND ".join(parts) if parts else None


def _bsl_enabled(entity: dict) -> bool:
    return bool(entity["bsl_enabled"]) if entity.get("bsl_enabled") is not None else True


def _require_bsl(entity: dict) -> None:
    if not _bsl_enabled(entity):
        raise ValueError(
            f"BSL methods are disabled for context '{entity['name']}'. Enable BSL in the UI."
        )


def _validate_ready_entity(entity: dict, context: str) -> dict:
    if not entity["enabled"]:
        raise ValueError(f"Context '{context}' is disabled. Enable it in the UI.")
    if entity["status"] != "ready":
        raise ValueError(
            f"Context '{context}' status is '{entity['status']}', need 'ready'. Run reindex."
        )
    return dict(entity)


def _doc_hit(d, *, context_name: str) -> dict:
    return {
        "context": context_name,
        "path": d.fields.get("path"),
        "kind": d.fields.get("kind"),
        "belong": d.fields.get("belong"),
        "name": d.fields.get("name"),
        "synonym": d.fields.get("synonym"),
        "score": d.score,
    }


def resolve_context(
    context: str,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Load a single ready+enabled entity by name. Rejects tag: refs (use for get_* tools)."""
    kind, value = parse_context_ref(context)
    if kind == "tag":
        raise ValueError(
            f"'{context}' is a tag group, not a single context. "
            "Use an entity name from list_contexts / list_context_groups "
            "or from a search hit's 'context' field. "
            "For group search pass tag:… to search_metadata / semantic_search / find_methods."
        )
    return _resolve_context_by_name(value, conn)


def _resolve_context_by_name(
    name: str,
    conn: sqlite3.Connection | None = None,
) -> dict:
    now = time.monotonic()
    with _entity_cache_lock:
        hit = _entity_cache.get(name)
        if hit is not None and (now - hit[0]) < _ENTITY_TTL_SEC:
            return _validate_ready_entity(hit[1], name)

    own_conn = conn is None
    if own_conn:
        conn = connect(settings.db_path)
    assert conn is not None
    try:
        entity = ent_repo.get_entity_by_name(conn, name)
        if not entity:
            raise ValueError(
                f"Unknown context '{name}'. Call list_contexts or list_context_groups "
                f"(tag:Name for a group)."
            )
        validated = _validate_ready_entity(dict(entity), name)
        with _entity_cache_lock:
            _entity_cache[name] = (time.monotonic(), dict(validated))
        return validated
    finally:
        if own_conn:
            conn.close()


def resolve_context_group(
    context: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, str | None, list[dict]]:
    """Resolve name or tag:… to a list of ready entities.

    Returns (ref, tag_or_none, entities).
    """
    kind, value = parse_context_ref(context)
    own_conn = conn is None
    if own_conn:
        conn = connect(settings.db_path)
    assert conn is not None
    try:
        if kind == "name":
            ent = _resolve_context_by_name(value, conn)
            return context, None, [ent]
        rows = ent_repo.list_ready_entities_for_tag(conn, value)
        if not rows:
            # Distinguish unknown tag vs empty group
            from app.repositories import tags as tag_repo

            if not tag_repo.get_tag_by_name(conn, value):
                raise ValueError(
                    f"Unknown tag '{value}'. Call list_context_groups to see tags."
                )
            raise ValueError(
                f"Tag '{value}' has no enabled ready contexts. "
                "Enable/index members in the UI or pick another tag."
            )
        entities = [_validate_ready_entity(dict(r), r["name"]) for r in rows]
        return context, value, entities
    finally:
        if own_conn:
            conn.close()


def _merge_scored_hits(
    groups: list[list[dict]],
    *,
    limit: int,
    offset: int = 0,
) -> list[dict]:
    merged: list[dict] = []
    for g in groups:
        merged.extend(g)
    merged.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
    merged = _dedupe_hits_by_context_path(merged)
    return merged[offset : offset + limit]


def fts_search(
    context: str,
    query: str,
    *,
    kind: str | None = None,
    include_borrowed: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    ref, tag, entities = resolve_context_group(context)
    topk = min(max(limit + offset, 1), settings.search_max_limit)
    per_groups: list[list[dict]] = []
    for entity in entities:
        coll = _open_collection(entity)
        q = zvec.Query(field_name="text", fts=zvec.Fts(match_string=query))
        docs = list(
            coll.query(
                queries=q,
                topk=topk,
                filter=_filter_expr(
                    kind=kind,
                    include_borrowed=include_borrowed,
                    exclude_methods=not _bsl_enabled(entity),
                ),
                output_fields=["path", "kind", "belong", "name", "synonym"],
            )
        )
        hits = _dedupe_hits_by_path(
            [_doc_hit(d, context_name=entity["name"]) for d in docs]
        )
        per_groups.append(hits)

    if len(entities) == 1:
        flat = per_groups[0]
        items = flat[offset : offset + limit]
        return {
            "context": ref,
            "contexts": [entities[0]["name"]],
            "tag": tag,
            "total_returned": len(items),
            "offset": offset,
            "limit": limit,
            "has_more": len(flat) > offset + limit,
            "results": items,
        }

    items = _merge_scored_hits(per_groups, limit=limit, offset=offset)
    # rough has_more: if any source had a full topk page
    has_more = any(len(g) >= topk for g in per_groups) or (
        sum(len(g) for g in per_groups) > offset + limit
    )
    return {
        "context": ref,
        "contexts": [e["name"] for e in entities],
        "tag": tag,
        "total_returned": len(items),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "results": items,
    }


def semantic_search(context: str, query: str, *, top_n: int = 20) -> dict:
    ref, tag, entities = resolve_context_group(context)
    client = EmbeddingClient()
    vec_by_model: dict[str, list[float]] = {}
    per_groups: list[list[dict]] = []
    for entity in entities:
        model = entity["model"]
        if model not in vec_by_model:
            vec_by_model[model] = client.embed([query], model, for_query=True)[0]
        vec = vec_by_model[model]
        coll = _open_collection(entity)
        vq = zvec.Query(field_name="embedding", vector=vec)
        fq = zvec.Query(field_name="text", fts=zvec.Fts(match_string=query))
        docs = coll.query(
            queries=[vq, fq],
            topk=top_n,
            reranker=zvec.RrfReRanker(rank_constant=60),
            filter=_filter_expr(
                kind=None,
                include_borrowed=True,
                exclude_methods=not _bsl_enabled(entity),
            ),
            output_fields=["path", "kind", "belong", "name", "synonym"],
        )
        hits = _dedupe_hits_by_path(
            [_doc_hit(d, context_name=entity["name"]) for d in docs]
        )
        per_groups.append(hits)

    if len(entities) == 1:
        results = per_groups[0]
    else:
        results = _merge_scored_hits(per_groups, limit=top_n, offset=0)

    return {
        "context": ref,
        "contexts": [e["name"] for e in entities],
        "tag": tag,
        "results": results,
    }


def get_object(context: str, path: str) -> dict:
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        obj = obj_repo.get_object(conn, entity["id"], path)
        if not obj:
            raise ValueError(f"Object '{path}' not found in context '{context}'.")
        if obj.get("kind") in {"Procedure", "Function"} and not _bsl_enabled(entity):
            raise ValueError(
                f"BSL methods are disabled for context '{context}'. Enable BSL in the UI."
            )
        out = dict(obj)
        out["context"] = entity["name"]
        return out
    finally:
        conn.close()


def get_links(context: str, path: str, direction: str = "both") -> dict:
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        return {
            "context": entity["name"],
            "path": path,
            **obj_repo.get_links(conn, entity["id"], path, direction),
        }
    finally:
        conn.close()


def list_code_modules(
    context: str,
    *,
    kind: str | None = None,
    q: str | None = None,
) -> dict:
    ref, tag, entities = resolve_context_group(context)
    conn = connect(settings.db_path)
    try:
        modules: list[dict] = []
        for entity in entities:
            _require_bsl(entity)
            items = method_repo.list_code_modules(conn, entity["id"], kind=kind, q=q)
            for m in items:
                row = dict(m)
                row["context"] = entity["name"]
                modules.append(row)
        modules.sort(key=lambda m: (m.get("context") or "", m.get("path") or ""))
        return {
            "context": ref,
            "contexts": [e["name"] for e in entities],
            "tag": tag,
            "total": len(modules),
            "modules": modules,
        }
    finally:
        conn.close()


def list_methods(
    context: str,
    *,
    parent_path: str | None = None,
    q: str | None = None,
    export_only: bool = False,
    limit: int = 50,
) -> dict:
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity)
        items = method_repo.list_methods(
            conn,
            entity["id"],
            parent_path=parent_path,
            q=q,
            export_only=export_only,
            limit=limit,
        )
        return {
            "context": entity["name"],
            "parent_path": parent_path or "",
            "total": len(items),
            "methods": items,
        }
    finally:
        conn.close()


def get_method(context: str, path: str) -> dict:
    conn = connect(settings.db_path)
    try:
        entity = resolve_context(context, conn)
        _require_bsl(entity)
        obj = method_repo.get_method(conn, entity["id"], path)
        if not obj:
            raise ValueError(f"Method '{path}' not found in context '{context}'.")
        return {"context": entity["name"], **obj}
    finally:
        conn.close()


def find_methods(
    context: str,
    query: str,
    *,
    export_only: bool = False,
    top_n: int = 15,
) -> dict:
    """Semantic/FTS search restricted to Procedure/Function kinds."""
    ref, tag, entities = resolve_context_group(context)
    client = EmbeddingClient()
    vec_by_model: dict[str, list[float]] = {}
    # Gather scored path hits per entity
    scored: list[tuple[float, dict, str]] = []  # score, entity, path
    docs_by_entity: dict[int, list] = {}

    for entity in entities:
        _require_bsl(entity)
        model = entity["model"]
        if model not in vec_by_model:
            vec_by_model[model] = client.embed([query], model, for_query=True)[0]
        vec = vec_by_model[model]
        coll = _open_collection(entity)
        vq = zvec.Query(field_name="embedding", vector=vec)
        fq = zvec.Query(field_name="text", fts=zvec.Fts(match_string=query))
        kind_filter = "(kind = 'Procedure' OR kind = 'Function')"
        docs = list(
            coll.query(
                queries=[vq, fq],
                topk=max(top_n * 3, top_n),
                reranker=zvec.RrfReRanker(rank_constant=60),
                filter=kind_filter,
                output_fields=["path", "kind", "belong", "name", "synonym"],
            )
        )
        docs_by_entity[int(entity["id"])] = docs
        for d in docs:
            scored.append((float(d.score or 0), entity, d.fields.get("path") or ""))

    scored.sort(key=lambda x: x[0], reverse=True)

    conn = connect(settings.db_path)
    try:
        results = []
        seen: set[tuple[str, str]] = set()
        # Prefetch method details per entity for paths we might need
        paths_by_eid: dict[int, list[str]] = {}
        for _score, entity, path in scored:
            if not path:
                continue
            paths_by_eid.setdefault(int(entity["id"]), []).append(path)

        details_by_eid: dict[int, dict[str, dict]] = {}
        for eid, paths in paths_by_eid.items():
            # unique preserve order
            uniq: list[str] = []
            seen_p: set[str] = set()
            for p in paths:
                if p not in seen_p:
                    seen_p.add(p)
                    uniq.append(p)
            details_by_eid[eid] = method_repo.get_methods_by_paths(conn, eid, uniq)

        for score, entity, path in scored:
            key = (entity["name"], path)
            if not path or key in seen:
                continue
            detail = (details_by_eid.get(int(entity["id"])) or {}).get(path) or {}
            if export_only and not detail.get("export"):
                continue
            seen.add(key)
            # synonym fallback from zvec doc
            syn = ""
            for d in docs_by_entity.get(int(entity["id"])) or []:
                if (d.fields.get("path") or "") == path:
                    syn = d.fields.get("synonym") or d.fields.get("name") or ""
                    break
            doc = detail.get("doc") or ""
            results.append(
                {
                    "context": entity["name"],
                    "path": path,
                    "parent_path": detail.get("parent_path") or "",
                    "name": detail.get("name") or "",
                    "kind": detail.get("kind") or "",
                    "export": bool(detail.get("export")),
                    "signature": detail.get("signature") or syn or "",
                    "doc_preview": doc[:200],
                    "score": score,
                }
            )
            if len(results) >= top_n:
                break
    finally:
        conn.close()

    return {
        "context": ref,
        "contexts": [e["name"] for e in entities],
        "tag": tag,
        "query": query,
        "results": results,
    }
