from __future__ import annotations

import gc
import logging
import re
import shutil
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import zvec

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import objects as obj_repo
from app.services.embeddings import EmbeddingClient, LmStudioUnavailable
from app.services.parser import (
    TextDecodeStats,
    decode_bytes,
    extract_links,
    iter_report_nodes,
    meta_from_first_node,
    node_fields,
)
from app.services import runtime_settings
from app.services.usage_stats import usage_stats
from app.services.zvec_store import zvec_store

log = logging.getLogger("cfsmcp.pipeline")

_TEMP_UPLOAD_RE = re.compile(r"^\.upload_[0-9a-f]+\.part$", re.I)
_TEMP_MODULES_FULL_RE = re.compile(r"^\.modules_full_[0-9a-f]+\.zip$", re.I)
_TEMP_MODULES_ENTITY_RE = re.compile(r"^\.modules_(\d+)_[0-9a-f]+\.zip$", re.I)
_ENTITY_REPORT_RE = re.compile(r"^e(\d+)\.txt$", re.I)
_PENDING_BSL_RE = re.compile(r"^e(\d+)\.bsl\.pending\.zip$", re.I)

# Ignore in-flight uploads younger than this (seconds) on startup sweep.
_METADATA_TEMP_GRACE_SEC = 3600.0


def collection_path(entity_id: int, model: str) -> Path:
    """Filesystem-safe path by entity id (avoid Cyrillic / special chars on Windows)."""
    safe_model = model.replace("/", "_").replace(":", "_").replace("@", "_")
    return settings.zvec_dir / f"e{int(entity_id)}__{safe_model}"


def entity_report_path(entity_id: int) -> Path:
    """Canonical on-disk report path — unique per entity."""
    return settings.metadata_dir / f"e{int(entity_id)}.txt"


def pending_bsl_zip_path(entity_id: int) -> Path:
    """Zip of modules to ingest after parse+index (full import / deferred upload)."""
    return settings.metadata_dir / f"e{int(entity_id)}.bsl.pending.zip"


def delete_entity_upload_files(
    entity_id: int,
    *,
    extra_paths: list[Path] | None = None,
) -> int:
    """Remove report / pending BSL / module temps for one entity. Returns count removed."""
    eid = int(entity_id)
    root = settings.metadata_dir
    candidates: list[Path] = [
        entity_report_path(eid),
        pending_bsl_zip_path(eid),
    ]
    if root.is_dir():
        for p in root.iterdir():
            if not p.is_file():
                continue
            m = _TEMP_MODULES_ENTITY_RE.match(p.name)
            if m and int(m.group(1)) == eid:
                candidates.append(p)
    if extra_paths:
        for p in extra_paths:
            if p is not None:
                candidates.append(Path(p))

    removed = 0
    seen: set[Path] = set()
    for p in candidates:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        try:
            if not p.exists() or not p.is_file():
                continue
            p.unlink(missing_ok=True)
            removed += 1
            log.info("removed entity upload file entity=%s path=%s", eid, p.name)
        except OSError:
            log.debug("failed removing entity upload file %s", p, exc_info=True)
    return removed


def _file_age_sec(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def _safe_unlink_metadata(path: Path, *, reason: str) -> bool:
    try:
        if not path.is_file():
            return False
        path.unlink(missing_ok=True)
        log.info("metadata cleanup: removed %s (%s)", path.name, reason)
        return True
    except OSError:
        log.debug("metadata cleanup: failed %s", path, exc_info=True)
        return False


def backfill_entity_types() -> dict[str, int]:
    """Detect CF/CFE from on-disk reports and fix ``entities.entity_type``.

    Runs on startup so older rows (defaulted to ``extension``) get corrected.
    """
    from app.services.parser import peek_report_meta

    stats = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}
    conn = connect(settings.db_path)
    try:
        rows = ent_repo.list_entities(conn)
        for row in rows:
            stats["checked"] += 1
            eid = int(row["id"])
            path = Path(row["file_path"]) if row.get("file_path") else entity_report_path(eid)
            if not path.is_file():
                stats["skipped"] += 1
                continue
            try:
                meta = peek_report_meta(path, path.stem)
                detected = meta.entity_type or "configuration"
                current = (row.get("entity_type") or "").strip() or "configuration"
                if detected == current:
                    stats["skipped"] += 1
                    continue
                ent_repo.set_entity_type(conn, eid, detected)
                stats["updated"] += 1
            except Exception:
                stats["errors"] += 1
                log.debug("entity_type backfill failed for #%s", eid, exc_info=True)
        conn.commit()
    finally:
        conn.close()
    return stats


def cleanup_metadata_orphans(*, grace_sec: float = _METADATA_TEMP_GRACE_SEC) -> dict[str, int]:
    """Remove leftover upload temps and files for deleted / unknown entities.

    Safe to run after ``recover_orphaned_jobs``: keeps ``e{id}.*`` for live ids
    and any path still referenced by ``entities.file_path``. Temps younger than
    ``grace_sec`` are left alone (in-flight uploads).
    """
    stats = {
        "scanned": 0,
        "removed_temp": 0,
        "removed_orphan_report": 0,
        "removed_orphan_pending": 0,
        "removed_orphan_other": 0,
        "kept": 0,
    }
    root = Path(settings.metadata_dir)
    if not root.is_dir():
        return stats

    conn = connect(settings.db_path)
    try:
        rows = ent_repo.list_entities(conn)
        live_ids = {int(r["id"]) for r in rows}
        keep: set[Path] = set()
        for r in rows:
            eid = int(r["id"])
            for p in (entity_report_path(eid), pending_bsl_zip_path(eid)):
                try:
                    keep.add(p.resolve())
                except OSError:
                    keep.add(p)
            fp = (r.get("file_path") or "").strip()
            if fp:
                try:
                    keep.add(Path(fp).resolve())
                except OSError:
                    keep.add(Path(fp))
    finally:
        conn.close()

    grace = max(0.0, float(grace_sec))
    for path in root.iterdir():
        if not path.is_file():
            continue
        stats["scanned"] += 1
        name = path.name
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path

        if resolved in keep:
            stats["kept"] += 1
            continue

        age = _file_age_sec(path)

        if _TEMP_UPLOAD_RE.match(name) or _TEMP_MODULES_FULL_RE.match(name):
            if age >= grace and _safe_unlink_metadata(path, reason="stale temp"):
                stats["removed_temp"] += 1
            else:
                stats["kept"] += 1
            continue

        m_mod = _TEMP_MODULES_ENTITY_RE.match(name)
        if m_mod:
            eid = int(m_mod.group(1))
            if eid not in live_ids:
                if _safe_unlink_metadata(path, reason=f"modules temp for deleted entity {eid}"):
                    stats["removed_temp"] += 1
            elif age >= grace and _safe_unlink_metadata(path, reason="stale modules temp"):
                stats["removed_temp"] += 1
            else:
                stats["kept"] += 1
            continue

        m_rep = _ENTITY_REPORT_RE.match(name)
        if m_rep:
            eid = int(m_rep.group(1))
            if eid not in live_ids and _safe_unlink_metadata(
                path, reason=f"orphan report e{eid}.txt"
            ):
                stats["removed_orphan_report"] += 1
            else:
                stats["kept"] += 1
            continue

        m_pending = _PENDING_BSL_RE.match(name)
        if m_pending:
            eid = int(m_pending.group(1))
            if eid not in live_ids and _safe_unlink_metadata(
                path, reason=f"orphan pending BSL e{eid}"
            ):
                stats["removed_orphan_pending"] += 1
            else:
                # Live entity: keep for recover / in-flight ingest
                stats["kept"] += 1
            continue

        # Legacy / unknown files not referenced by any entity
        if age >= grace and _safe_unlink_metadata(path, reason="unreferenced metadata file"):
            stats["removed_orphan_other"] += 1
        else:
            stats["kept"] += 1

    return stats


def stage_pending_bsl_zip(
    entity_id: int,
    zip_path: Path,
    *,
    mode: str | None = None,
    embed_mode: str | None = None,
) -> Path:
    """Move/copy zip into pending slot and optionally remember load/embed modes."""
    dest = pending_bsl_zip_path(entity_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink(missing_ok=True)
    src = Path(zip_path)
    if src.resolve() != dest.resolve():
        try:
            src.replace(dest)
        except OSError:
            shutil.copy2(src, dest)
            src.unlink(missing_ok=True)
    conn = connect(settings.db_path)
    try:
        if mode:
            ent_repo.set_bsl_load_mode(conn, entity_id, str(mode).strip().lower())
        if embed_mode:
            ent_repo.set_bsl_embed_mode(conn, entity_id, str(embed_mode).strip().lower())
        conn.commit()
    finally:
        conn.close()
    return dest


def continue_pending_bsl_after_index(entity_id: int) -> bool:
    """If a pending modules zip exists, ingest it then reindex. Returns True if ran."""
    path = pending_bsl_zip_path(entity_id)
    if not path.exists():
        return False
    conn = connect(settings.db_path)
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return False
        mode = str(entity.get("bsl_load_mode") or "").strip().lower() or None
        embed_mode = str(entity.get("bsl_embed_mode") or "").strip().lower() or None
        ent_repo.set_status(conn, entity_id, "loading_modules", error_message="")
        conn.commit()
    finally:
        conn.close()
    try:
        ingest_bsl_zip(
            entity_id,
            path,
            mode=mode,
            embed_mode=embed_mode,
            queue_reindex=False,
        )
        path.unlink(missing_ok=True)
        reindex_entity(entity_id, scope="bsl")
        return True
    except Exception as exc:
        log.exception("pending BSL ingest failed entity=%s", entity_id)
        path.unlink(missing_ok=True)
        try:
            conn = connect(settings.db_path)
            try:
                ent_repo.set_status(
                    conn,
                    entity_id,
                    "modules_error",
                    error_message=str(exc)[:500],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            log.exception("failed to persist modules_error entity=%s", entity_id)
        return True


def _close_collection(collection) -> None:
    if collection is None:
        return
    try:
        collection.flush()
    except Exception:
        log.debug("flush failed", exc_info=True)
    try:
        if hasattr(collection, "close"):
            collection.close()
    except Exception:
        log.debug("close failed", exc_info=True)
    try:
        del collection
    except Exception:
        pass
    gc.collect()


def _destroy_path(path: Path) -> None:
    """Close any open handle and remove a zvec collection directory."""
    zvec_store.release(path)
    if not path.exists():
        return
    try:
        coll = zvec.open(str(path))
        try:
            coll.destroy()
        finally:
            del coll
            gc.collect()
    except Exception:
        log.debug("destroy via open failed for %s", path, exc_info=True)
    for attempt in range(8):
        if not path.exists():
            return
        shutil.rmtree(path, ignore_errors=True)
        gc.collect()
        if not path.exists():
            return
        time.sleep(0.1 * (attempt + 1))
    if path.exists():
        log.warning("could not fully remove zvec path %s", path)


def _is_zvec_residue_name(name: str) -> bool:
    n = name.lower()
    return (
        n.endswith(".tmp")
        or n.endswith(".tmp.partial")
        or n.endswith(".__tmp__")
        or ".tmp." in n
        or n.endswith(".proxima.bak")
        or n.endswith(".ipc.bak")
    )


def cleanup_zvec_crash_residue(coll_path: Path | None = None) -> int:
    """Remove crash/temp leftovers next to or inside zvec collections.

    After abrupt stop zvec may leave ``N.tmp/`` dirs and stale segment files;
    clearing them before reopen avoids noisy WARN and copy→rebuild fallbacks.
    If ``coll_path`` is None, scan all collections under ``ZVEC_DIR``.
    """
    removed = 0
    roots: list[Path] = []
    if coll_path is not None:
        roots.append(Path(coll_path))
    else:
        zdir = Path(settings.zvec_dir)
        if zdir.is_dir():
            roots.extend(p for p in zdir.iterdir() if p.is_dir())

    for root in roots:
        zvec_store.release(root)
        for sibling in (
            Path(str(root) + ".__tmp__"),
            Path(str(root) + ".tmp"),
            Path(str(root) + ".bak"),
        ):
            if not sibling.exists():
                continue
            try:
                if sibling.is_dir():
                    _destroy_path(sibling)
                else:
                    sibling.unlink(missing_ok=True)
                removed += 1
                log.info("removed zvec residue %s", sibling)
            except Exception:
                log.debug("failed removing residue %s", sibling, exc_info=True)

        if not root.is_dir():
            continue
        # Deepest paths first so nested temps go before parents
        candidates = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
        for p in candidates:
            if not _is_zvec_residue_name(p.name):
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink(missing_ok=True)
                removed += 1
                log.info("removed zvec residue %s", p)
            except Exception:
                log.debug("failed removing residue %s", p, exc_info=True)
    return removed


def _reindex_full(
    conn,
    client: EmbeddingClient,
    entity_id: int,
    model: str,
    coll_path: Path,
    dims: int,
) -> int:
    """Build a fresh zvec collection in place (no temp→rename: fails on Win bind mounts)."""
    obj_repo.reset_embed_flags(conn, entity_id)
    conn.commit()
    # Leftovers from older temp+replace attempts
    _destroy_path(Path(str(coll_path) + ".__tmp__"))
    _destroy_path(coll_path)

    collection = None
    try:
        collection = zvec.create_and_open(str(coll_path), build_schema(dims))
        indexed = _embed_pending_into(
            conn, client, entity_id, model, collection, use_upsert=False
        )
        try:
            collection.flush()
        except Exception:
            log.debug("flush after full reindex failed", exc_info=True)
        try:
            if hasattr(collection, "optimize"):
                collection.optimize()
        except Exception:
            log.debug("optimize after full reindex failed", exc_info=True)
    finally:
        _close_collection(collection)
    obj_repo.clear_pending_zvec_deletes(conn, entity_id)
    conn.commit()
    return indexed


def build_schema(dims: int) -> zvec.CollectionSchema:
    fts = zvec.FtsIndexParam(
        tokenizer_name="standard",
        filters=["lowercase", "stemmer"],
        extra_params='{"stemmer_lang":"russian"}',
    )
    return zvec.CollectionSchema(
        name="meta",
        fields=[
            zvec.FieldSchema(name="path", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(
                name="kind",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                name="belong",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(name="name", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name="synonym", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(
                name="text",
                data_type=zvec.DataType.STRING,
                index_param=fts,
            ),
        ],
        vectors=[
            zvec.VectorSchema(
                name="embedding",
                data_type=zvec.DataType.VECTOR_FP32,
                dimension=dims,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
        ],
    )


def _apply_report_meta(conn, entity_id: int, entity: dict, meta) -> tuple[int, dict, Path]:
    """Update synonym/version/comment; rename from report Имя when not name_locked.

    Comment defaults to report Имя + Версия; custom comments are kept.
    Name conflicts are resolved at upload (merge confirm or Name override) — not here.
    """
    path = Path(entity["file_path"])
    locked = bool(entity.get("name_locked"))
    target_name = (meta.config_name or "").strip() or entity["name"]
    auto_comment = ent_repo.comment_from_name_version(target_name, meta.version or "")
    old_auto = ent_repo.comment_from_name_version(
        entity.get("name") or "", entity.get("version") or ""
    )
    prev_comment = (entity.get("comment") or "").strip()
    new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment

    if locked:
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (meta.config_synonym, meta.version, new_comment, meta.entity_type or "configuration", entity_id),
        )
        row = ent_repo.get_entity(conn, entity_id) or entity
        return entity_id, row, path

    if target_name and target_name != entity["name"]:
        other = ent_repo.get_entity_by_name(conn, target_name)
        if other is not None and int(other["id"]) != int(entity_id):
            raise ValueError(
                f"Report name '{target_name}' already used by entity #{other['id']}. "
                "Re-upload with merge confirmation or a different Name override."
            )
        conn.execute(
            """
            UPDATE entities SET name=?, synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (
                target_name,
                meta.config_synonym,
                meta.version,
                new_comment,
                meta.entity_type or "configuration",
                entity_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (meta.config_synonym, meta.version, new_comment, meta.entity_type or "configuration", entity_id),
        )
    row = ent_repo.get_entity(conn, entity_id) or entity
    return entity_id, row, path


def _flush_merge_batches(
    conn,
    entity_id: int,
    *,
    new_rows: list[dict],
    changed_rows: list[dict],
    touch_ids: list[int],
    parse_gen: int,
) -> None:
    if new_rows:
        obj_repo.insert_new_objects(conn, entity_id, new_rows)
        new_rows.clear()
    if changed_rows:
        obj_repo.update_changed_objects(conn, changed_rows)
        changed_rows.clear()
    if touch_ids:
        obj_repo.touch_unchanged_objects(conn, touch_ids, parse_gen)
        touch_ids.clear()


def _dedupe_fields_by_path(fields_batch: list[dict]) -> list[dict]:
    """Keep last occurrence per path (reports may emit the same path more than once)."""
    by_path: dict[str, dict] = {}
    for fields in fields_batch:
        by_path[fields["path"]] = fields
    return list(by_path.values())


def _classify_and_queue(
    fields_batch: list[dict],
    existing: dict[str, tuple[int, str]],
    *,
    new_rows: list[dict],
    changed_rows: list[dict],
    touch_ids: list[int],
) -> tuple[int, int, int]:
    """Split a fields batch into new/changed/unchanged queues. Returns added, changed, unchanged."""
    added = changed = unchanged = 0
    # Paths already queued as new in this classify pass (within-batch / cross-lookup gap)
    queued_new: dict[str, int] = {}
    for fields in fields_batch:
        path = fields["path"]
        prev = existing.get(path)
        if prev is None and path not in queued_new:
            new_rows.append(fields)
            queued_new[path] = len(new_rows) - 1
            added += 1
        elif prev is None:
            # Duplicate path in same batch — replace earlier "new" row
            new_rows[queued_new[path]] = fields
        else:
            obj_id, old_hash = prev
            if old_hash == fields["content_hash"] and old_hash:
                touch_ids.append(obj_id)
                unchanged += 1
            else:
                fields["id"] = obj_id
                changed_rows.append(fields)
                changed += 1
    return added, changed, unchanged


OBJECT_FLUSH = 500
LINK_FLUSH = 1000



def iter_clone_entity(source_id: int, new_name: str) -> Iterator[dict[str, Any]]:
    """Deep-copy a ready entity; yield progress events ``{pct, step, detail[, new_id]}``."""
    name = (new_name or "").strip()
    if not name:
        raise ValueError("New name is required")
    conn = connect(settings.db_path)
    new_id: int | None = None
    src: dict | None = None
    try:
        yield {"pct": 3, "step": "prepare", "detail": name}
        src = ent_repo.get_entity(conn, source_id)
        if not src:
            raise ValueError("Entity not found")
        if src.get("status") != "ready":
            raise ValueError("Only fully indexed (ready) entities can be copied")
        if ent_repo.get_entity_by_name(conn, name):
            raise ValueError(f"Name already exists: {name}")

        model = src["model"] or settings.default_embedding_model
        src_file = Path(src["file_path"])
        if not src_file.exists():
            raise ValueError("Source report file is missing")

        cur = conn.execute(
            """
            INSERT INTO entities(
              name, name_locked, synonym, comment, entity_type, version, file_path, enabled, bsl_enabled, bsl_load_mode, bsl_embed_mode, model, status,
              object_count, link_count, indexed_count, index_target, index_started_at,
              parse_gen, parse_added, parse_changed, parse_deleted, parse_unchanged, error_message
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'ready', ?,?,?,0,0,?,?,?,?,?, '')
            """,
            (
                name,
                1,
                src.get("synonym") or "",
                src.get("comment")
                or ent_repo.comment_from_name_version(name, src.get("version") or ""),
                src.get("entity_type") or "configuration",
                src.get("version") or "",
                "",  # set after we know id
                int(src.get("enabled") or 1),
                int(src["bsl_enabled"]) if src.get("bsl_enabled") is not None else 1,
                str(src.get("bsl_load_mode") or ""),
                str(src.get("bsl_embed_mode") or ""),
                model,
                int(src.get("object_count") or 0),
                int(src.get("link_count") or 0),
                int(src.get("indexed_count") or src.get("object_count") or 0),
                int(src.get("parse_gen") or 0),
                int(src.get("parse_added") or 0),
                int(src.get("parse_changed") or 0),
                int(src.get("parse_deleted") or 0),
                int(src.get("parse_unchanged") or 0),
            ),
        )
        new_id = int(cur.lastrowid)
        yield {"pct": 8, "step": "copy_file", "detail": name}
        dest_file = settings.metadata_dir / f"e{new_id}.txt"
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # copy2 can fail on Windows Docker bind mounts (errno 1); raw bytes is reliable
        dest_file.write_bytes(src_file.read_bytes())
        conn.execute(
            "UPDATE entities SET file_path=?, updated_at=datetime('now') WHERE id=?",
            (str(dest_file.resolve()), new_id),
        )

        id_map: dict[int, int] = {}
        rows = conn.execute(
            """
            SELECT id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
                   props_json, content_hash, parse_gen, embed_done
            FROM objects WHERE entity_id=?
            ORDER BY id
            """,
            (source_id,),
        ).fetchall()
        total_obj = len(rows)
        yield {
            "pct": 12,
            "step": "copy_objects",
            "detail": f"0 / {total_obj}",
        }
        for idx, r in enumerate(rows, start=1):
            cur = conn.execute(
                """
                INSERT INTO objects(
                  entity_id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
                  props_json, content_hash, parse_gen, embed_done
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id
                """,
                (
                    new_id,
                    r["path"],
                    r["kind_ru"],
                    r["kind"],
                    r["name"],
                    r["synonym"] or "",
                    r["comment"] or "",
                    r["belong"] or "Own",
                    r["base_object"] or "",
                    r["props_json"] or "{}",
                    r["content_hash"] or "",
                    int(r["parse_gen"] or 0),
                    int(r["embed_done"] or 0),
                ),
            )
            id_map[int(r["id"])] = int(cur.fetchone()[0])
            if idx == total_obj or idx % 250 == 0:
                pct = 12 + int(38 * idx / max(total_obj, 1))
                yield {
                    "pct": pct,
                    "step": "copy_objects",
                    "detail": f"{idx} / {total_obj}",
                }

        yield {"pct": 55, "step": "copy_links", "detail": name}
        conn.execute(
            """
            INSERT INTO links(entity_id, from_path, to_ref, link_type)
            SELECT ?, from_path, to_ref, link_type FROM links WHERE entity_id=?
            """,
            (new_id, source_id),
        )
        from app.repositories import tags as tag_repo

        yield {"pct": 60, "step": "copy_tags", "detail": name}
        tag_repo.copy_entity_tags(conn, source_id, new_id)
        conn.commit()

        src_coll = collection_path(source_id, model)
        dst_coll = collection_path(new_id, model)
        if not src_coll.exists() or not id_map:
            ent_repo.set_status(conn, new_id, "parsed", indexed_count=0, index_target=0)
            conn.commit()
            log.warning("clone %s->%s: no zvec source, left as parsed", source_id, new_id)
            yield {
                "pct": 100,
                "step": "done",
                "detail": name,
                "new_id": new_id,
                "needs_reindex": True,
            }
            return

        yield {"pct": 65, "step": "copy_index", "detail": name}
        zvec_store.release(src_coll)
        zvec_store.release(dst_coll)
        _destroy_path(dst_coll)

        src_z = zvec.open(str(src_coll), option=zvec.CollectionOption(read_only=True))
        # probe dimension
        sample_old = next(iter(id_map.keys()))
        sample = src_z.fetch(str(sample_old), include_vector=True)
        sample_doc = next(iter(sample.values()))
        dims = len(sample_doc.vectors["embedding"])
        dst_z = zvec.create_and_open(str(dst_coll), build_schema(dims))
        try:
            old_ids = list(id_map.keys())
            chunk = 100
            total_ids = len(old_ids)
            for i in range(0, total_ids, chunk):
                part = old_ids[i : i + chunk]
                from app.services.bsl_embed import zvec_doc_ids_for_object

                fetch_ids: list[str] = []
                for x in part:
                    fetch_ids.extend(zvec_doc_ids_for_object(x))
                fetched = src_z.fetch(fetch_ids, include_vector=True)
                docs = []
                for oid_s, doc in fetched.items():
                    base_s, _, suffix = str(oid_s).partition("#")
                    try:
                        base_id = int(base_s)
                    except ValueError:
                        continue
                    nid = id_map.get(base_id)
                    if nid is None:
                        continue
                    new_doc_id = str(nid) if not suffix else f"{nid}#{suffix}"
                    docs.append(
                        zvec.Doc(
                            id=new_doc_id,
                            fields=dict(doc.fields),
                            vectors={"embedding": list(doc.vectors["embedding"])},
                        )
                    )
                if docs:
                    dst_z.insert(docs)
                done = min(i + chunk, total_ids)
                pct = 65 + int(30 * done / max(total_ids, 1))
                yield {
                    "pct": pct,
                    "step": "copy_index",
                    "detail": f"{done} / {total_ids}",
                }
            try:
                dst_z.flush()
            except Exception:
                log.debug("flush after clone failed", exc_info=True)
        finally:
            _close_collection(dst_z)
            del src_z
            gc.collect()

        yield {"pct": 97, "step": "finalize", "detail": name}
        ent_repo.set_status(
            conn,
            new_id,
            "ready",
            indexed_count=int(src.get("object_count") or len(id_map)),
            index_target=int(src.get("object_count") or len(id_map)),
            error_message="",
        )
        conn.commit()
        log.info("cloned entity %s -> %s name=%s objects=%s", source_id, new_id, name, len(id_map))
        yield {
            "pct": 100,
            "step": "done",
            "detail": name,
            "new_id": new_id,
            "needs_reindex": False,
        }
    except Exception:
        if new_id is not None:
            try:
                # best-effort cleanup of partial clone
                coll = collection_path(
                    new_id,
                    (src or {}).get("model") or settings.default_embedding_model,
                )
                zvec_store.release(coll)
                _destroy_path(coll)
                delete_entity_upload_files(new_id)
                ent_repo.delete_entity(conn, new_id)
                conn.commit()
            except Exception:
                log.exception("cleanup after failed clone %s", new_id)
        raise
    finally:
        conn.close()


def clone_entity(
    source_id: int,
    new_name: str,
    on_progress: Callable[[int, str, str], None] | None = None,
) -> int:
    """Deep-copy a ready entity under a new locked name (SQLite + report file + zvec)."""
    new_id: int | None = None
    for ev in iter_clone_entity(source_id, new_name):
        if on_progress:
            on_progress(int(ev.get("pct") or 0), str(ev.get("step") or ""), str(ev.get("detail") or ""))
        if ev.get("step") == "done":
            new_id = int(ev["new_id"])
    if new_id is None:
        raise RuntimeError("Clone finished without new entity id")
    return new_id



def parse_entity(entity_id: int) -> None:
    """Stream report nodes → SQLite in batches (no full node list / no full path-index in RAM)."""
    parsed_ok = False
    conn = connect(settings.db_path)
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return
        ent_repo.set_status(conn, entity_id, "parsing", error_message="")
        conn.commit()
        path = Path(entity["file_path"])

        parse_gen = int(entity.get("parse_gen") or 0) + 1

        conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
        conn.commit()

        fields_batch: list[dict] = []
        new_rows: list[dict] = []
        changed_rows: list[dict] = []
        touch_ids: list[int] = []
        links_batch: list[tuple[str, str, str]] = []
        added = changed = unchanged = 0
        total_links = 0
        total_obj = 0
        meta_applied = False
        first_node = True
        decode_stats = TextDecodeStats()

        def flush_objects() -> None:
            nonlocal added, changed, unchanged
            if not fields_batch:
                return
            unique_batch = _dedupe_fields_by_path(fields_batch)
            existing = obj_repo.lookup_paths(
                conn, entity_id, [f["path"] for f in unique_batch]
            )
            a, c, u = _classify_and_queue(
                unique_batch,
                existing,
                new_rows=new_rows,
                changed_rows=changed_rows,
                touch_ids=touch_ids,
            )
            added += a
            changed += c
            unchanged += u
            fields_batch.clear()
            _flush_merge_batches(
                conn,
                entity_id,
                new_rows=new_rows,
                changed_rows=changed_rows,
                touch_ids=touch_ids,
                parse_gen=parse_gen,
            )
            conn.commit()

        def flush_links() -> None:
            nonlocal total_links
            if not links_batch:
                return
            total_links += obj_repo.insert_links_batch(conn, entity_id, links_batch)
            links_batch.clear()
            conn.commit()

        for node in iter_report_nodes(path, decode_stats):
            if first_node:
                # Fallback = tentative upload name (file stem), not e{id}.txt
                meta = meta_from_first_node(node, entity["name"])
                entity_id, entity, path = _apply_report_meta(conn, entity_id, entity, meta)
                parse_gen = int(entity.get("parse_gen") or 0) + 1
                conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
                conn.commit()
                meta_applied = True
                first_node = False

            fields = node_fields(node)
            fields["content_hash"] = obj_repo.content_hash(fields)
            fields["parse_gen"] = parse_gen
            fields_batch.append(fields)
            links_batch.extend(extract_links(node))
            total_obj += 1

            if len(fields_batch) >= OBJECT_FLUSH:
                flush_objects()
            if len(links_batch) >= LINK_FLUSH:
                flush_links()
            if total_obj % OBJECT_FLUSH == 0:
                ent_repo.set_status(
                    conn,
                    entity_id,
                    "parsing",
                    object_count=total_obj,
                    link_count=total_links,
                    parse_added=added,
                    parse_changed=changed,
                    parse_unchanged=unchanged,
                )
                conn.commit()

        if not meta_applied:
            meta = meta_from_first_node(None, entity["name"])
            entity_id, entity, path = _apply_report_meta(conn, entity_id, entity, meta)
            parse_gen = int(entity.get("parse_gen") or 0) + 1
            conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
            conn.commit()

        flush_objects()
        flush_links()

        deleted_ids = obj_repo.delete_stale_objects(
            conn,
            entity_id,
            parse_gen,
            exclude_kinds=("Procedure", "Function"),
        )
        # BSL methods whose parent metadata object was just removed (or already missing)
        orphan_ids = obj_repo.delete_orphan_method_objects(conn, entity_id)
        deleted = len(deleted_ids) + len(orphan_ids)
        if orphan_ids:
            log.info(
                "parse entity %s: removed %s orphan BSL methods after parent delete",
                entity_id,
                len(orphan_ids),
            )

        total_stored = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=?",
                (entity_id,),
            ).fetchone()["c"]
        )

        enc_note = ""
        if decode_stats.replacement_count:
            enc_note = (
                f"encoding={decode_stats.encoding} "
                f"replacements={decode_stats.replacement_count}"
            )
            log.warning(
                "parsed entity %s with decode replacements: %s",
                entity_id,
                enc_note,
            )
        ent_repo.set_status(
            conn,
            entity_id,
            "parsed",
            object_count=total_stored,
            link_count=total_links,
            parse_gen=parse_gen,
            parse_added=added,
            parse_changed=changed,
            parse_deleted=deleted,
            parse_unchanged=unchanged,
            # Soft UI signal when � appeared; kept across reindex until clean re-parse.
            error_message=enc_note,
        )
        conn.commit()
        log.info(
            "parsed entity %s objects=%s links=%s +%s ~%s -%s =%s encoding=%s replacements=%s",
            entity_id,
            total_obj,
            total_links,
            added,
            changed,
            deleted,
            unchanged,
            decode_stats.encoding,
            decode_stats.replacement_count,
        )
        usage_stats.record(
            kind="index",
            name="parse",
            ok=True,
            detail=(
                f"objects={total_obj} +{added}~{changed}-{deleted}={unchanged} "
                f"enc={decode_stats.encoding} repl={decode_stats.replacement_count}"
            ),
        )
        parsed_ok = True
    except Exception as exc:
        log.exception("parse failed entity=%s", entity_id)
        try:
            ent_repo.set_status(conn, entity_id, "parse_error", error_message=str(exc))
            conn.commit()
        except Exception:
            log.exception("failed to persist parse_error for entity=%s", entity_id)
        usage_stats.record(kind="index", name="parse", ok=False, detail=str(exc)[:200])
    finally:
        conn.close()

    if parsed_ok:
        log.info("auto-reindex after parse entity=%s", entity_id)
        reindex_entity(entity_id)


def _make_docs(passages: list[dict], vectors: list) -> list:
    docs = []
    for p, vec in zip(passages, vectors):
        o = p["obj"]
        docs.append(
            zvec.Doc(
                id=str(p["doc_id"]),
                fields={
                    "path": o["path"],
                    "kind": o["kind"],
                    "belong": o["belong"],
                    "name": o["name"],
                    "synonym": o["synonym"],
                    "text": p["text"],
                },
                vectors={"embedding": vec},
            )
        )
    return docs


def _embed_chunk(
    chunk_rows: list,
    model: str,
    base_url: str | None,
    mode: str,
) -> tuple[list[dict], list[dict], list[list[float]]]:
    """Embed one batch in a worker thread (own EmbeddingClient)."""
    from app.services.bsl_embed import passages_for_object

    objs = [dict(r) for r in chunk_rows]
    passages: list[dict] = []
    for o in objs:
        for doc_id, text in passages_for_object(o, mode):
            passages.append({"obj": o, "doc_id": doc_id, "text": text})
    if not passages:
        return objs, [], []
    client = EmbeddingClient(base_url=base_url)
    vectors = client.embed([p["text"] for p in passages], model, for_query=False)
    return objs, passages, vectors


def _embed_pending_into(
    conn,
    client: EmbeddingClient,
    entity_id: int,
    model: str,
    collection,
    *,
    use_upsert: bool,
) -> int:
    """Embed pending objects; parallel LM Studio calls, serial zvec/SQLite writes."""
    from app.services.bsl_embed import zvec_doc_ids_for_object

    indexed = 0
    batch_size = max(1, int(settings.embedding_batch_size))
    workers = max(1, int(runtime_settings.get_embedding_workers()))
    mode = runtime_settings.get_bsl_embed_mode()
    entity_row = ent_repo.get_entity(conn, entity_id)
    if entity_row:
        from app.services.bsl_embed import normalize_bsl_embed_mode

        mode = normalize_bsl_embed_mode(
            str(entity_row.get("bsl_embed_mode") or "") or mode
        )
    base_url = client.base_url

    while True:
        rows = conn.execute(
            """
            SELECT id, path, kind, name, synonym, comment, belong, props_json
            FROM objects WHERE entity_id=? AND embed_done=0
            ORDER BY id LIMIT ?
            """,
            (entity_id, batch_size * workers),
        ).fetchall()
        if not rows:
            break

        chunks = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
        if len(chunks) == 1 or workers == 1:
            results = [_embed_chunk(c, model, base_url, mode) for c in chunks]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
                futures = [
                    pool.submit(_embed_chunk, chunk, model, base_url, mode)
                    for chunk in chunks
                ]
                for fut in as_completed(futures):
                    results.append(fut.result())

        for objs, passages, vectors in results:
            if use_upsert and objs:
                clear_ids = [
                    doc_id
                    for o in objs
                    for doc_id in zvec_doc_ids_for_object(o["id"])
                ]
                for i in range(0, len(clear_ids), 500):
                    try:
                        collection.delete(clear_ids[i : i + 500])
                    except Exception:
                        log.debug("zvec clear before upsert failed", exc_info=True)
            docs = _make_docs(passages, vectors)
            if not docs:
                obj_repo.mark_embedded(conn, [o["id"] for o in objs])
                continue
            if use_upsert:
                collection.upsert(docs)
            else:
                collection.insert(docs)
            obj_repo.mark_embedded(conn, [o["id"] for o in objs])
            indexed += len(objs)
            ent_repo.set_status(conn, entity_id, "indexing", indexed_count=indexed)
            conn.commit()
    return indexed


def _reindex_incremental(
    conn,
    client: EmbeddingClient,
    entity_id: int,
    model: str,
    coll_path: Path,
    pending_deletes: list[str],
) -> int:
    """Upsert changed docs and delete removed ids in the existing collection."""
    zvec_store.release(coll_path)
    try:
        cleanup_zvec_crash_residue(coll_path)
    except Exception:
        log.debug("zvec residue cleanup before incremental failed", exc_info=True)
    collection = zvec_store.get(coll_path, read_only=False)
    try:
        if pending_deletes:
            chunk = 500
            for i in range(0, len(pending_deletes), chunk):
                collection.delete(pending_deletes[i : i + chunk])
            obj_repo.clear_pending_zvec_deletes(conn, entity_id)
            conn.commit()
        indexed = _embed_pending_into(
            conn, client, entity_id, model, collection, use_upsert=True
        )
        try:
            collection.flush()
        except Exception:
            log.debug("flush after incremental reindex failed", exc_info=True)
        try:
            if hasattr(collection, "optimize"):
                collection.optimize()
        except Exception:
            log.debug("optimize after incremental reindex failed", exc_info=True)
        return indexed
    finally:
        zvec_store.release(coll_path)


def recover_orphaned_jobs() -> dict:
    """After process restart, resume jobs left in busy statuses (thread pool is gone)."""
    from app.services import jobs

    try:
        n = cleanup_zvec_crash_residue()
        if n:
            log.info("cleaned %s zvec crash residue path(s) on startup", n)
    except Exception:
        log.exception("zvec residue cleanup failed")

    conn = connect(settings.db_path)
    resumed = {"indexing": 0, "parsing": 0, "uploaded": 0, "loading_modules": 0, "pending_bsl": 0}
    try:
        rows = ent_repo.list_entities(conn)
        for row in rows:
            eid = int(row["id"])
            st = row.get("status") or ""
            if st == "indexing":
                model = row.get("model") or settings.default_embedding_model
                try:
                    cleanup_zvec_crash_residue(collection_path(eid, model))
                except Exception:
                    log.debug("per-entity zvec cleanup failed entity=%s", eid, exc_info=True)
                # Clear sticky lock so reindex_entity can start again
                ent_repo.set_status(
                    conn,
                    eid,
                    "parsed",
                    error_message="",
                    index_started_at=0.0,
                )
                conn.commit()
                jobs.submit(reindex_entity, eid)
                resumed["indexing"] += 1
                log.warning("recovered orphaned indexing entity=%s — reindex resumed", eid)
            elif st == "parsing":
                ent_repo.set_status(conn, eid, "uploaded", error_message="")
                conn.commit()
                jobs.submit(parse_entity, eid)
                resumed["parsing"] += 1
                log.warning("recovered orphaned parsing entity=%s — parse resumed", eid)
            elif st == "uploaded":
                jobs.submit(parse_entity, eid)
                resumed["uploaded"] += 1
                log.warning("recovered uploaded entity=%s — parse queued", eid)
            elif st == "loading_modules":
                if pending_bsl_zip_path(eid).exists():
                    ent_repo.set_status(conn, eid, "ready", error_message="")
                    conn.commit()
                    jobs.submit(continue_pending_bsl_after_index, eid)
                    resumed["loading_modules"] += 1
                    log.warning(
                        "recovered orphaned loading_modules entity=%s — BSL ingest resumed",
                        eid,
                    )
                else:
                    ent_repo.set_status(conn, eid, "ready", error_message="")
                    conn.commit()
            elif st == "ready" and pending_bsl_zip_path(eid).exists():
                jobs.submit(continue_pending_bsl_after_index, eid)
                resumed["pending_bsl"] += 1
                log.warning(
                    "recovered pending BSL zip for ready entity=%s — ingest queued",
                    eid,
                )
    finally:
        conn.close()
    return resumed


def _normalize_index_scope(scope: str | None) -> str:
    s = (scope or "").strip().lower()
    if s in {"report", "bsl", "all"}:
        return s
    return ""


def _infer_index_scope(
    conn,
    entity_id: int,
    *,
    full_rebuild: bool,
) -> str:
    """report = metadata objects, bsl = methods, all = both."""
    if full_rebuild:
        meta, methods = obj_repo.count_objects_split(conn, entity_id)
    else:
        meta, methods = obj_repo.count_embed_pending_split(conn, entity_id)
    if methods and meta:
        return "all"
    if methods:
        return "bsl"
    return "report"


def reindex_entity(entity_id: int, *, scope: str | None = None) -> None:
    conn = connect(settings.db_path)
    client = EmbeddingClient()
    reindex_ok = False
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return
        if entity["status"] == "indexing":
            return
        if entity["object_count"] <= 0:
            ent_repo.set_status(conn, entity_id, "index_error", error_message="Parse first")
            conn.commit()
            return

        model = entity["model"] or settings.default_embedding_model
        coll_path = collection_path(entity_id, model)
        try:
            cleanup_zvec_crash_residue(coll_path)
        except Exception:
            log.debug("zvec residue cleanup before reindex failed entity=%s", entity_id, exc_info=True)
        pending_deletes = obj_repo.list_pending_zvec_deletes(conn, entity_id)
        pending_embeds = obj_repo.count_embed_pending(conn, entity_id)
        full_rebuild = not coll_path.exists()

        if full_rebuild:
            pending_embeds = int(entity["object_count"] or 0)

        index_target = (
            max(pending_embeds, 1)
            if (pending_embeds or pending_deletes or full_rebuild)
            else 0
        )
        index_scope = _normalize_index_scope(scope) or _infer_index_scope(
            conn, entity_id, full_rebuild=full_rebuild
        )
        ent_repo.set_status(
            conn,
            entity_id,
            "indexing",
            error_message="",
            indexed_count=0,
            index_target=index_target,
            index_started_at=time.time(),
            index_scope=index_scope,
        )
        conn.commit()

        dims = client.dims(model)
        settings.zvec_dir.mkdir(parents=True, exist_ok=True)
        indexed = 0
        mode = "full" if full_rebuild else "incr"

        if full_rebuild:
            indexed = _reindex_full(conn, client, entity_id, model, coll_path, dims)
        else:
            try:
                indexed = _reindex_incremental(
                    conn, client, entity_id, model, coll_path, pending_deletes
                )
            except Exception:
                log.exception(
                    "incremental reindex failed entity=%s — falling back to full rebuild",
                    entity_id,
                )
                mode = "full-fallback"
                pending_embeds = obj_repo.count_embed_pending(conn, entity_id)
                ent_repo.set_status(
                    conn,
                    entity_id,
                    "indexing",
                    indexed_count=0,
                    index_target=max(pending_embeds, int(entity["object_count"] or 0)),
                    index_started_at=time.time(),
                    index_scope=index_scope or _infer_index_scope(
                        conn, entity_id, full_rebuild=True
                    ),
                )
                conn.commit()
                indexed = _reindex_full(conn, client, entity_id, model, coll_path, dims)

        total = int(entity["object_count"] or 0)
        pending = obj_repo.count_embed_pending(conn, entity_id)
        done_count = total if pending == 0 else max(0, total - pending)
        # Keep soft encoding warnings from parse; clear other leftover messages.
        prev_err = str(entity.get("error_message") or "")
        keep_err = prev_err if prev_err.startswith("encoding=") else ""
        ent_repo.set_status(
            conn,
            entity_id,
            "ready",
            indexed_count=done_count,
            index_target=total,
            index_started_at=0.0,
            index_scope="",
            error_message=keep_err,
        )
        conn.commit()
        log.info(
            "reindex done entity=%s mode=%s upserted=%s deletes=%s",
            entity_id,
            mode,
            indexed,
            len(pending_deletes) if mode == "incr" else 0,
        )
        usage_stats.record(
            kind="index",
            name="reindex",
            ok=True,
            detail=f"mode={mode} upserted={indexed}",
        )
        reindex_ok = True
    except Exception as exc:
        reindex_ok = False
        log.exception("reindex failed entity=%s", entity_id)
        msg = str(exc)
        ent_repo.set_status(
            conn,
            entity_id,
            "index_error",
            error_message=msg[:500],
            index_started_at=0.0,
            index_scope="",
        )
        conn.commit()
        usage_stats.record(kind="index", name="reindex", ok=False, detail=msg[:200])
        try:
            entity = ent_repo.get_entity(conn, entity_id)
            model = (entity or {}).get("model") or settings.default_embedding_model
            broken = collection_path(entity_id, model)
            # Always drop incomplete temp copy; keep final collection on LM outage
            # so embed_done + zvec resume still work after LM is back.
            _destroy_path(Path(str(broken) + ".__tmp__"))
            if not isinstance(exc, LmStudioUnavailable):
                _destroy_path(broken)
        except Exception:
            pass
    finally:
        conn.close()

    if reindex_ok:
        continue_pending_bsl_after_index(entity_id)


# Cap examples returned in upload-modules stats (UI alert + ingest log).
_BSL_LOG_SAMPLE_LIMIT = 25


def _push_bsl_log_sample(bucket: list[str], line: str, *, limit: int = _BSL_LOG_SAMPLE_LIMIT) -> None:
    if len(bucket) >= limit:
        return
    bucket.append(line)


def _parent_missing_hint(expected: str, parent_paths: set[str]) -> str:
    """Describe how close expected_parent is to objects already in the report."""
    bits = expected.split(".")
    nearest = ""
    for n in range(len(bits) - 1, 0, -1):
        prefix = ".".join(bits[:n])
        if prefix in parent_paths:
            nearest = prefix
            break
    if nearest:
        depth = expected.count(".")
        siblings = sorted(
            p
            for p in parent_paths
            if p.startswith(nearest + ".") and p.count(".") == depth
        )[:6]
        out = f"nearest_existing={nearest}"
        if siblings:
            out += f" | same_depth_siblings={siblings}"
        elif nearest.count(".") + 1 < depth:
            child_forms = sorted(
                p for p in parent_paths if p.startswith(nearest + ".Формы.")
            )[:6]
            if child_forms:
                out += f" | forms_under_object={child_forms}"
            else:
                out += " | hint=object_exists_but_form_or_deeper_path_missing"
        return out
    kind = bits[0] if bits else ""
    if kind and any(p == kind or p.startswith(kind + ".") for p in parent_paths):
        return f"kind_present={kind} | hint=object_name_or_form_not_in_report"
    return "hint=no_prefix_of_expected_parent_in_report"


def _collect_module_members(
    raw_members: dict[str, bytes],
) -> tuple[dict[str, bytes], dict[str, str], dict]:
    """Normalize dump paths; keep *.bsl; convert Ext/Form.bin → Ext/Form/Module.bsl.

    Returns (bsl_members, source_override, meta). source_override maps synthetic
    Module.bsl path → original Form.bin path for provenance.
    """
    from app.services.bsl_paths import normalize_zip_member, strip_common_root
    from app.services.form_bin import (
        FormBinEmpty,
        FormBinError,
        form_bin_to_module_path,
        extract_form_module_bsl,
        is_form_bin_path,
    )

    _root, _rels = strip_common_root(list(raw_members.keys()))
    bsl: dict[str, bytes] = {}
    form_bins: list[tuple[str, bytes]] = []
    ignored = 0
    for key, raw in raw_members.items():
        norm = normalize_zip_member(key)
        if _root and norm.startswith(_root + "/"):
            rel = norm[len(_root) + 1 :]
        else:
            rel = norm
        if rel.lower().endswith(".bsl"):
            bsl[rel] = raw
        elif is_form_bin_path(rel):
            form_bins.append((rel, raw))
        else:
            ignored += 1

    source_override: dict[str, str] = {}
    extracted = 0
    failed = 0
    empty = 0
    skipped_managed = 0
    sample_form_fail: list[str] = []
    bsl_lower = {k.lower(): k for k in bsl}
    for rel, raw in form_bins:
        try:
            synth = form_bin_to_module_path(rel)
        except ValueError as exc:
            failed += 1
            _push_bsl_log_sample(
                sample_form_fail,
                f"file={rel} | reason=bad_form_bin_path | error={exc}",
            )
            continue
        # Prefer real managed Module.bsl when both exist
        if synth.lower() in bsl_lower:
            skipped_managed += 1
            continue
        try:
            text = extract_form_module_bsl(raw)
        except FormBinEmpty:
            empty += 1
            continue
        except FormBinError as exc:
            failed += 1
            _push_bsl_log_sample(
                sample_form_fail,
                f"file={rel} | reason=extract_failed | error={exc}",
            )
            continue
        bsl[synth] = text.encode("utf-8")
        bsl_lower[synth.lower()] = synth
        source_override[synth] = rel
        extracted += 1

    meta = {
        "files_ignored": ignored,
        "files_form_bin": len(form_bins),
        "form_bin_extracted": extracted,
        "form_bin_empty": empty,
        "form_bin_failed": failed,
        "form_bin_skipped_managed": skipped_managed,
        "sample_form_bin_failed": sample_form_fail,
    }
    return bsl, source_override, meta


def ingest_bsl_zip(
    entity_id: int,
    zip_path: Path,
    *,
    mode: str | None = None,
    embed_mode: str | None = None,
    queue_reindex: bool = True,
) -> dict:
    """Parse *.bsl (+ Form.bin ordinary forms) from a zip dump, merge methods, reindex."""
    import zipfile

    from app.services.bsl_paths import normalize_zip_member, strip_common_root
    from app.services.form_bin import is_form_bin_path

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        _root, _relative = strip_common_root(names)
        members: dict[str, bytes] = {}
        for member in names:
            norm = normalize_zip_member(member)
            if _root and norm.startswith(_root + "/"):
                rel = norm[len(_root) + 1 :]
            else:
                rel = norm
            if not (rel.lower().endswith(".bsl") or is_form_bin_path(rel)):
                continue
            try:
                members[rel] = zf.read(member)
            except Exception:
                continue
    return ingest_bsl_members(
        entity_id,
        members,
        mode=mode,
        embed_mode=embed_mode,
        queue_reindex=queue_reindex,
    )


def ingest_bsl_members(
    entity_id: int,
    members: dict[str, bytes],
    *,
    mode: str | None = None,
    embed_mode: str | None = None,
    queue_reindex: bool = True,
) -> dict:
    """Merge methods from dump-relative path → BSL bytes. Non-mapped / no-parent skipped."""
    from app.services.bsl_embed import (
        BSL_EMBED_META,
        normalize_bsl_embed_mode,
    )
    from app.services.bsl_parser import BSL_LOAD_MODES, mode_flags, parse_bsl_methods
    from app.services.bsl_paths import (
        BslModuleRef,
        explain_unresolved_bsl_path,
        method_object_path,
        resolve_bsl_module,
    )
    import unicodedata

    normalized, source_override, collect_meta = _collect_module_members(members)

    sample_form_fail = list(collect_meta.get("sample_form_bin_failed") or [])
    sample_no_parent: list[str] = []
    sample_unresolved: list[str] = []
    entity_name = ""

    conn = connect(settings.db_path)
    stats = {
        "files_bsl": 0,
        "files_ignored": int(collect_meta.get("files_ignored") or 0),
        "files_form_bin": int(collect_meta.get("files_form_bin") or 0),
        "form_bin_extracted": int(collect_meta.get("form_bin_extracted") or 0),
        "form_bin_empty": int(collect_meta.get("form_bin_empty") or 0),
        "form_bin_failed": int(collect_meta.get("form_bin_failed") or 0),
        "form_bin_skipped_managed": int(collect_meta.get("form_bin_skipped_managed") or 0),
        "methods_loaded": 0,
        "methods_skipped_no_parent": 0,
        "bsl_non_utf8": 0,
        "bsl_replacements": 0,
        "methods_added": 0,
        "methods_changed": 0,
        "methods_unchanged": 0,
        "methods_deleted": 0,
        "modules_unresolved": 0,
        "bsl_load_mode": "",
        "bsl_embed_mode": "",
        "embed_mode_coerced": False,
        "embed_mode_requested": "",
        "sample_form_bin_failed": sample_form_fail,
        "sample_no_parent": sample_no_parent,
        "sample_unresolved": sample_unresolved,
    }
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            raise ValueError("Entity not found")
        entity_name = str(entity.get("name") or "")
        if entity["status"] in {"parsing", "indexing", "uploaded"}:
            raise ValueError(f"Entity is busy (status={entity['status']})")

        saved = str(entity.get("bsl_load_mode") or "").strip().lower()
        chosen = (mode or "").strip().lower() or saved or "signatures"
        if chosen not in BSL_LOAD_MODES:
            raise ValueError(f"Unknown BSL load mode: {chosen}")
        include_doc, include_body = mode_flags(chosen)
        stats["bsl_load_mode"] = chosen
        ent_repo.set_bsl_load_mode(conn, entity_id, chosen)

        embed_saved = str(entity.get("bsl_embed_mode") or "").strip().lower()
        embed_requested = normalize_bsl_embed_mode(
            (embed_mode or "").strip().lower()
            or embed_saved
            or runtime_settings.get_bsl_embed_mode()
        )
        embed_chosen = embed_requested
        if chosen == "signatures" and embed_requested != BSL_EMBED_META:
            stats["embed_mode_coerced"] = True
            stats["embed_mode_requested"] = embed_requested
            log.warning(
                "entity %s: embed_mode=%s incompatible with load_mode=signatures; using meta",
                entity_id,
                embed_requested,
            )
            embed_chosen = BSL_EMBED_META
        stats["bsl_embed_mode"] = embed_chosen
        ent_repo.set_bsl_embed_mode(conn, entity_id, embed_chosen)

        # Keep UI row in loading_modules while parsing methods (full import).
        if entity["status"] != "loading_modules":
            ent_repo.set_status(conn, entity_id, "loading_modules", error_message="")
            conn.commit()

        parent_paths = {
            unicodedata.normalize("NFC", r["path"])
            for r in conn.execute(
                "SELECT path FROM objects WHERE entity_id=? AND kind NOT IN ('Procedure','Function')",
                (entity_id,),
            ).fetchall()
        }
        config_parents = sorted(
            p for p in parent_paths if p.startswith("Конфигурации.") and p.count(".") == 1
        )
        config_parent = config_parents[0] if config_parents else ""
        stats["entity_id"] = entity_id
        stats["parent_object_count"] = len(parent_paths)
        stats["object_count"] = int(entity.get("object_count") or 0)
        stats["config_parent"] = config_parent

        methods_gen = int(entity.get("parse_gen") or 0) + 1
        fields_batch: list[dict] = []

        for rel in sorted(normalized.keys()):
            stats["files_bsl"] += 1
            raw = normalized[rel]
            src = source_override.get(rel, rel)
            ref = resolve_bsl_module(rel)
            if not ref:
                stats["modules_unresolved"] += 1
                _push_bsl_log_sample(
                    sample_unresolved,
                    explain_unresolved_bsl_path(src),
                )
                continue
            parent_path = ref.parent_path
            if parent_path == "__CONFIGURATION__":
                if not config_parent:
                    stats["modules_unresolved"] += 1
                    _push_bsl_log_sample(
                        sample_unresolved,
                        f"file={src} | role={ref.module_role} | "
                        f"reason=no_configuration_node_in_report",
                    )
                    continue
                parent_path = config_parent
                ref = BslModuleRef(
                    parent_path=parent_path,
                    module_role=ref.module_role,
                    source_file=ref.source_file,
                )
            decoded = decode_bytes(raw)
            text = decoded.text
            if decoded.encoding != "utf-8" and decoded.encoding != "utf-8-sig":
                stats["bsl_non_utf8"] = int(stats.get("bsl_non_utf8") or 0) + 1
            if decoded.replacement_count:
                stats["bsl_replacements"] = int(stats.get("bsl_replacements") or 0) + int(
                    decoded.replacement_count
                )
                log.warning(
                    "BSL decode replacements entity=%s file=%s encoding=%s count=%s",
                    entity_id,
                    rel,
                    decoded.encoding,
                    decoded.replacement_count,
                )
            methods = parse_bsl_methods(
                text, include_doc=include_doc, include_body=include_body
            )
            if ref.parent_path not in parent_paths:
                stats["methods_skipped_no_parent"] += len(methods)
                if methods:
                    method_names = ",".join(m.name for m in methods[:8])
                    if len(methods) > 8:
                        method_names += f",…(+{len(methods) - 8})"
                    hint = _parent_missing_hint(ref.parent_path, parent_paths)
                    _push_bsl_log_sample(
                        sample_no_parent,
                        f"file={src} | expected_parent={ref.parent_path} | "
                        f"role={ref.module_role} | methods={len(methods)} | "
                        f"method_names={method_names} | "
                        f"reason=parent_missing_in_metadata_report | {hint}",
                    )
                continue
            for method in methods:
                path = method_object_path(ref.parent_path, ref.module_role, method.name)
                kind_ru = "Функции" if method.kind == "Function" else "Процедуры"
                props = {
                    "source": "bsl",
                    "signature": method.signature,
                    "export": method.export,
                    "parent_path": ref.parent_path,
                    "module_role": ref.module_role,
                    "source_file": source_override.get(rel, ref.source_file),
                    "line": method.line,
                    "load_mode": chosen,
                }
                if method.body:
                    props["body"] = method.body
                if rel in source_override:
                    props["source_kind"] = "form_bin"
                fields = {
                    "path": path,
                    "kind_ru": kind_ru,
                    "kind": method.kind,
                    "name": method.name,
                    "synonym": method.signature,
                    "comment": method.doc,
                    "belong": "Own",
                    "base_object": "",
                    "props": props,
                    "parse_gen": methods_gen,
                }
                fields["content_hash"] = obj_repo.content_hash(fields)
                fields_batch.append(fields)
                stats["methods_loaded"] += 1

        by_path: dict[str, dict] = {}
        for f in fields_batch:
            by_path[f["path"]] = f
        unique = list(by_path.values())

        new_rows: list[dict] = []
        changed_rows: list[dict] = []
        touch_ids: list[int] = []
        existing = obj_repo.lookup_paths(conn, entity_id, [f["path"] for f in unique])
        a, c, u = _classify_and_queue(
            unique,
            existing,
            new_rows=new_rows,
            changed_rows=changed_rows,
            touch_ids=touch_ids,
        )
        stats["methods_added"] = a
        stats["methods_changed"] = c
        stats["methods_unchanged"] = u
        _flush_merge_batches(
            conn,
            entity_id,
            new_rows=new_rows,
            changed_rows=changed_rows,
            touch_ids=touch_ids,
            parse_gen=methods_gen,
        )

        deleted_ids = obj_repo.delete_stale_objects(
            conn,
            entity_id,
            methods_gen,
            kinds_only=("Procedure", "Function"),
        )
        stats["methods_deleted"] = len(deleted_ids)

        total_stored = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=?",
                (entity_id,),
            ).fetchone()["c"]
        )
        ent_repo.set_status(
            conn,
            entity_id,
            "parsed",
            object_count=total_stored,
            parse_gen=methods_gen,
            parse_added=a,
            parse_changed=c,
            parse_deleted=len(deleted_ids),
            parse_unchanged=u,
            error_message="",
        )
        conn.commit()
    finally:
        conn.close()

    jobs_ok = True
    if queue_reindex:
        try:
            from app.services import jobs

            jobs.submit(reindex_entity, entity_id, scope="bsl")
        except Exception:
            jobs_ok = False
            log.exception("failed to submit reindex after bsl ingest entity=%s", entity_id)
    else:
        jobs_ok = False

    stats["reindex_queued"] = jobs_ok
    stats["sample_cap"] = _BSL_LOG_SAMPLE_LIMIT
    usage_stats.record(
        kind="index",
        name="bsl_ingest",
        ok=True,
        context=entity_name,
        detail=(
            f"mode={stats['bsl_load_mode']} embed={stats['bsl_embed_mode']} "
            f"loaded={stats['methods_loaded']} "
            f"skipped={stats['methods_skipped_no_parent']}"
        ),
    )
    usage_stats.record_bsl_ingest(context=entity_name, stats=stats)
    return stats


def clear_entity_bsl(entity_id: int) -> dict:
    """Delete all BSL method objects for an entity and queue zvec cleanup / reindex."""
    conn = connect(settings.db_path)
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            raise ValueError("Entity not found")
        if entity["status"] in {"parsing", "indexing", "uploaded"}:
            raise ValueError(f"Entity is busy (status={entity['status']})")

        deleted_ids = obj_repo.delete_method_objects(conn, entity_id)
        ent_repo.set_bsl_load_mode(conn, entity_id, "")
        ent_repo.set_bsl_embed_mode(conn, entity_id, "")
        total_stored = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=?",
                (entity_id,),
            ).fetchone()["c"]
        )
        ent_repo.set_status(
            conn,
            entity_id,
            entity["status"] if entity["status"] in {"ready", "parsed", "index_error"} else "parsed",
            object_count=total_stored,
            error_message="",
        )
        # If we removed methods from a ready index, refresh zvec
        need_reindex = bool(deleted_ids) and entity["status"] in {"ready", "index_error", "parsed"}
        if need_reindex and entity["status"] == "ready":
            ent_repo.set_status(conn, entity_id, "parsed", object_count=total_stored)
        conn.commit()
    finally:
        conn.close()

    reindex_queued = False
    if deleted_ids:
        try:
            from app.services import jobs

            jobs.submit(reindex_entity, entity_id, scope="report")
            reindex_queued = True
        except Exception:
            log.exception("failed to submit reindex after bsl clear entity=%s", entity_id)

    usage_stats.record(
        kind="index",
        name="bsl_clear",
        ok=True,
        detail=f"deleted={len(deleted_ids)}",
    )
    return {
        "methods_deleted": len(deleted_ids),
        "reindex_queued": reindex_queued,
    }
