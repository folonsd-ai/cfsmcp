from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.schemas.entities import EntityCopy, EntityOut, EntityPatch, ReindexResponse
from app.services import jobs
from app.services.pipeline import (
    collection_path,
    clear_entity_bsl,
    delete_entity_upload_files,
    entity_report_path,
    ingest_bsl_members,
    ingest_bsl_zip,
    iter_clone_entity,
    parse_entity,
    reindex_entity,
    stage_pending_bsl_zip,
)
from app.services.parser import peek_report_meta
from app.services.zvec_store import zvec_store
from app.services import runtime_settings

router = APIRouter(prefix="/api/entities", tags=["entities"])

_SAFE = re.compile(r"[^\w\-\.]+", re.UNICODE)


def _bsl_mode_from_form(form) -> str | None:
    raw = form.get("mode")
    if raw is None:
        return None
    mode = str(raw).strip().lower()
    return mode or None


def _bsl_embed_mode_from_form(form) -> str | None:
    raw = form.get("embed_mode")
    if raw is None:
        return None
    mode = str(raw).strip().lower()
    return mode or None


def _effective_bsl_load_mode(row: dict) -> str:
    """Empty mode means signatures (legacy / default). Only expose when BSL is loaded."""
    mode = str(row.get("bsl_load_mode") or "").strip().lower()
    if mode in {"signatures", "code", "full"}:
        return mode
    if int(row.get("bsl_method_count") or 0) > 0:
        return "signatures"
    return ""


def _effective_bsl_embed_mode(row: dict) -> str:
    """Empty embed mode means meta. Only expose when BSL is loaded."""
    from app.services.bsl_embed import ALLOWED_BSL_EMBED_MODES

    mode = str(row.get("bsl_embed_mode") or "").strip().lower()
    if mode in ALLOWED_BSL_EMBED_MODES:
        return mode
    if int(row.get("bsl_method_count") or 0) > 0:
        return "meta"
    return ""


def _row_to_out(row: dict) -> EntityOut:
    return EntityOut(
        id=row["id"],
        name=row["name"],
        synonym=row.get("synonym") or "",
        comment=row.get("comment") or "",
        entity_type=row.get("entity_type") or "configuration",
        version=row.get("version") or "",
        enabled=bool(row["enabled"]),
        bsl_enabled=bool(row["bsl_enabled"]) if row.get("bsl_enabled") is not None else True,
        bsl_method_count=int(row.get("bsl_method_count") or 0),
        bsl_load_mode=_effective_bsl_load_mode(row),
        bsl_embed_mode=_effective_bsl_embed_mode(row),
        tag_ids=[int(t) for t in (row.get("tag_ids") or [])],
        model=row["model"],
        status=row["status"],
        name_locked=bool(row.get("name_locked")),
        object_count=row.get("object_count") or 0,
        link_count=row.get("link_count") or 0,
        indexed_count=row.get("indexed_count") or 0,
        index_target=row.get("index_target") or 0,
        index_started_at=float(row.get("index_started_at") or 0),
        index_scope=str(row.get("index_scope") or ""),
        parse_added=row.get("parse_added") or 0,
        parse_changed=row.get("parse_changed") or 0,
        parse_deleted=row.get("parse_deleted") or 0,
        parse_unchanged=row.get("parse_unchanged") or 0,
        error_message=row.get("error_message") or "",
        usage_calls=int(row.get("usage_calls") or 0),
        usage_share_pct=int(row.get("usage_share_pct") or 0),
        usage_bar_pct=int(row.get("usage_bar_pct") or 0),
        usage_window_sec=int(row.get("usage_window_sec") or 600),
    )


def _attach_usage(rows: list[dict]) -> None:
    """Fill usage_* fields from in-memory MCP stats (configured window)."""
    from app.services.usage_stats import usage_stats

    snap = usage_stats.mcp_usage_by_context()
    by_name = snap.get("by_name") or {}
    total = int(snap.get("total") or 0)
    max_calls = int(snap.get("max_calls") or 0)
    window_sec = int(snap.get("window_sec") or 600)
    for r in rows:
        name = str(r.get("name") or "")
        calls = int(by_name.get(name) or 0)
        share = int(round(100.0 * calls / total)) if total > 0 and calls > 0 else 0
        bar = int(round(100.0 * calls / max_calls)) if max_calls > 0 and calls > 0 else 0
        r["usage_calls"] = calls
        r["usage_share_pct"] = min(100, share)
        r["usage_bar_pct"] = min(100, bar)
        r["usage_window_sec"] = window_sec


@router.get("")
def list_entities() -> list[EntityOut]:
    conn = connect(settings.db_path)
    try:
        from app.repositories import tags as tag_repo

        rows = ent_repo.list_entities(conn)
        by_tags = tag_repo.tags_by_entity_ids(conn, [int(r["id"]) for r in rows])
        for r in rows:
            r["tag_ids"] = by_tags.get(int(r["id"]), [])
        _attach_usage(rows)
        return [_row_to_out(r) for r in rows]
    finally:
        conn.close()


@router.post("/upload")
async def upload_entity(
    file: Annotated[UploadFile, File()],
    model: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
    merge: Annotated[str | None, Form()] = None,
    entity_id: Annotated[str | None, Form()] = None,
) -> EntityOut:
    """Upload report. Without Name override uses report ``Имя``; on clash asks UI to merge or override.

    Pass ``entity_id`` to replace the report of that row (merge/update); keeps its MCP name.
    """
    settings.metadata_dir.mkdir(parents=True, exist_ok=True)
    original = file.filename or "report.txt"
    safe = _SAFE.sub("_", original).strip("._") or "report.txt"
    name_override = (name or "").strip()
    name_locked = bool(name_override)
    merge_flag = str(merge or "").strip().lower() in {"1", "true", "yes", "on"}
    target_id: int | None = None
    if entity_id is not None and str(entity_id).strip() != "":
        try:
            target_id = int(str(entity_id).strip())
        except ValueError as exc:
            raise HTTPException(400, "Invalid entity_id") from exc
    use_model = model or runtime_settings.get_default_embedding_model()
    file_stem = Path(safe).stem

    tmp = settings.metadata_dir / f".upload_{uuid.uuid4().hex}.part"
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        meta = peek_report_meta(tmp, file_stem)
        report_name = (meta.config_name or "").strip() or file_stem
        tentative_name = name_override if name_locked else report_name

        conn = connect(settings.db_path)
        try:
            if target_id is not None:
                existing = ent_repo.get_entity(conn, target_id)
                if not existing:
                    raise HTTPException(404, "Entity not found")
                use_model = model or existing.get("model") or use_model
                old_path = Path(existing["file_path"]) if existing.get("file_path") else None
                entity_id_out = ent_repo.refresh_entity_file(
                    conn,
                    target_id,
                    synonym=meta.config_synonym or existing.get("synonym") or "",
                    version=meta.version or existing.get("version") or "",
                    file_path=str(tmp.resolve()),
                    model=use_model,
                    entity_type=meta.entity_type or "configuration",
                )
            else:
                existing = ent_repo.get_entity_by_name(conn, tentative_name)
                if existing and not name_locked and not merge_flag:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "name_conflict",
                            "message": (
                                f"Configuration '{report_name}' is already loaded. "
                                "Confirm merge into the existing entity, or upload again with a different Name override."
                            ),
                            "report_name": report_name,
                            "report_synonym": meta.config_synonym or "",
                            "report_version": meta.version or "",
                            "existing_id": int(existing["id"]),
                            "existing_name": existing["name"],
                        },
                    )

                old_path = Path(existing["file_path"]) if existing and existing.get("file_path") else None

                entity_id_out = ent_repo.upsert_entity(
                    conn,
                    name=tentative_name,
                    synonym=meta.config_synonym or "",
                    version=meta.version or "",
                    file_path=str(tmp.resolve()),
                    model=use_model,
                    entity_type=meta.entity_type or "configuration",
                    name_locked=name_locked,
                )

            dest = entity_report_path(entity_id_out)

            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            tmp.replace(dest)
            tmp = None  # moved

            conn.execute(
                "UPDATE entities SET file_path=?, updated_at=datetime('now') WHERE id=?",
                (str(dest.resolve()), entity_id_out),
            )
            conn.commit()
            row = ent_repo.get_entity(conn, entity_id_out)

            if old_path is not None:
                try:
                    meta_root = settings.metadata_dir.resolve()
                    op = old_path.resolve()
                    if (
                        op != dest.resolve()
                        and op.exists()
                        and op.parent == meta_root
                        and not op.name.startswith(".upload_")
                    ):
                        op.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            conn.close()
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)

    jobs.submit(parse_entity, entity_id_out)
    return _row_to_out(row)


@router.post("/upload-full")
async def upload_full(
    file: Annotated[UploadFile, File()],
    modules: Annotated[UploadFile | None, File()] = None,
    model: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
    merge: Annotated[str | None, Form()] = None,
    mode: Annotated[str | None, Form()] = None,
    embed_mode: Annotated[str | None, Form()] = None,
) -> EntityOut:
    """Upload report + optional modules zip; process parse→index→BSL in background.

    UI returns immediately; row status shows progress (parsing / indexing / loading_modules).
    """
    settings.metadata_dir.mkdir(parents=True, exist_ok=True)
    original = file.filename or "report.txt"
    safe = _SAFE.sub("_", original).strip("._") or "report.txt"
    name_override = (name or "").strip()
    name_locked = bool(name_override)
    merge_flag = str(merge or "").strip().lower() in {"1", "true", "yes", "on"}
    use_model = model or runtime_settings.get_default_embedding_model()
    file_stem = Path(safe).stem

    modules_tmp: Path | None = None
    if modules is not None and getattr(modules, "filename", None):
        modules_tmp = settings.metadata_dir / f".modules_full_{uuid.uuid4().hex}.zip"
        with modules_tmp.open("wb") as out:
            while True:
                chunk = await modules.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        if modules_tmp.stat().st_size <= 0:
            modules_tmp.unlink(missing_ok=True)
            modules_tmp = None

    tmp = settings.metadata_dir / f".upload_{uuid.uuid4().hex}.part"
    entity_id_out: int | None = None
    row = None
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        meta = peek_report_meta(tmp, file_stem)
        report_name = (meta.config_name or "").strip() or file_stem
        tentative_name = name_override if name_locked else report_name

        conn = connect(settings.db_path)
        try:
            existing = ent_repo.get_entity_by_name(conn, tentative_name)
            if existing and not name_locked and not merge_flag:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "name_conflict",
                        "message": (
                            f"Configuration '{report_name}' is already loaded. "
                            "Confirm merge into the existing entity, or upload again with a different Name override."
                        ),
                        "report_name": report_name,
                        "report_synonym": meta.config_synonym or "",
                        "report_version": meta.version or "",
                        "existing_id": int(existing["id"]),
                        "existing_name": existing["name"],
                    },
                )

            old_path = Path(existing["file_path"]) if existing and existing.get("file_path") else None
            entity_id_out = ent_repo.upsert_entity(
                conn,
                name=tentative_name,
                synonym=meta.config_synonym or "",
                version=meta.version or "",
                file_path=str(tmp.resolve()),
                model=use_model,
                entity_type=meta.entity_type or "configuration",
                name_locked=name_locked,
            )

            dest = entity_report_path(entity_id_out)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            tmp.replace(dest)
            tmp = None

            conn.execute(
                "UPDATE entities SET file_path=?, updated_at=datetime('now') WHERE id=?",
                (str(dest.resolve()), entity_id_out),
            )
            conn.commit()
            row = ent_repo.get_entity(conn, entity_id_out)

            if old_path is not None:
                try:
                    meta_root = settings.metadata_dir.resolve()
                    op = old_path.resolve()
                    if (
                        op != dest.resolve()
                        and op.exists()
                        and op.parent == meta_root
                        and not op.name.startswith(".upload_")
                    ):
                        op.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            conn.close()
    except Exception:
        if modules_tmp is not None and modules_tmp.exists():
            modules_tmp.unlink(missing_ok=True)
        raise
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)

    assert entity_id_out is not None and row is not None
    if modules_tmp is not None:
        try:
            stage_pending_bsl_zip(
                entity_id_out,
                modules_tmp,
                mode=_bsl_mode_from_form_value(mode),
                embed_mode=_bsl_embed_mode_from_form_value(embed_mode),
            )
        except Exception:
            modules_tmp.unlink(missing_ok=True)
            raise

    jobs.submit(parse_entity, entity_id_out)
    return _row_to_out(row)


def _bsl_mode_from_form_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    mode = str(raw).strip().lower()
    return mode or None


def _bsl_embed_mode_from_form_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    mode = str(raw).strip().lower()
    return mode or None


@router.post("/{entity_id}/upload-modules")
async def upload_modules(entity_id: int, request: Request) -> dict:
    """Upload BSL modules: one .zip (preferred) or many dump files (*.bsl + Ext/Form.bin).

    Parses multipart with a raised max_files/max_fields limit (Starlette default is 1000).
    Prefer a single zip from the UI for large dumps.
    """
    from app.services.form_bin import is_form_bin_path

    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        if row["status"] in {"parsing", "indexing", "uploaded"}:
            raise HTTPException(409, f"Entity is busy (status={row['status']})")
        if int(row.get("object_count") or 0) <= 0:
            raise HTTPException(409, "Parse metadata report first")
    finally:
        conn.close()

    try:
        form = await request.form(max_files=100_000, max_fields=100_000)
    except Exception as exc:
        raise HTTPException(400, f"Invalid multipart: {exc}") from exc

    single = form.get("file")
    file_list = [f for f in form.getlist("files") if getattr(f, "filename", None)]
    path_list = [str(p) for p in form.getlist("paths")]

    try:
        if single is not None and getattr(single, "filename", None) and str(single.filename).lower().endswith(".zip"):
            settings.metadata_dir.mkdir(parents=True, exist_ok=True)
            tmp = settings.metadata_dir / f".modules_{entity_id}_{uuid.uuid4().hex}.zip"
            try:
                with tmp.open("wb") as out:
                    while True:
                        chunk = await single.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                stats = ingest_bsl_zip(
                    entity_id,
                    tmp,
                    mode=_bsl_mode_from_form(form),
                    embed_mode=_bsl_embed_mode_from_form(form),
                )
            finally:
                tmp.unlink(missing_ok=True)
        elif file_list:
            members: dict[str, bytes] = {}
            ignored = 0
            for i, uf in enumerate(file_list):
                rel = ""
                if i < len(path_list):
                    rel = (path_list[i] or "").replace("\\", "/").lstrip("/")
                if not rel:
                    rel = (uf.filename or "").replace("\\", "/").lstrip("/")
                if not (rel.lower().endswith(".bsl") or is_form_bin_path(rel)):
                    ignored += 1
                    await uf.read()
                    continue
                members[rel] = await uf.read()
            if not members:
                raise HTTPException(400, "No *.bsl / Form.bin files found in selection")
            stats = ingest_bsl_members(
                entity_id,
                members,
                mode=_bsl_mode_from_form(form),
                embed_mode=_bsl_embed_mode_from_form(form),
            )
            stats["files_ignored"] = int(stats.get("files_ignored") or 0) + ignored
        else:
            raise HTTPException(
                400,
                "Provide a .zip or dump files (*.bsl / Ext/Form.bin with relative paths)",
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Modules ingest failed: {exc}") from exc
    finally:
        await form.close()

    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        return {"ok": True, "entity": _row_to_out(row) if row else None, "stats": stats}
    finally:
        conn.close()


@router.delete("/{entity_id}/modules")
def delete_entity_modules(entity_id: int) -> dict:
    """Remove all loaded BSL method objects for the entity (metadata report stays)."""
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        if row["status"] in {"parsing", "indexing", "uploaded"}:
            raise HTTPException(409, f"Entity is busy (status={row['status']})")
    finally:
        conn.close()
    try:
        stats = clear_entity_bsl(entity_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Clear BSL failed: {exc}") from exc

    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        # enrich count for UI
        if row:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
                (entity_id,),
            ).fetchone()["c"]
            row = dict(row)
            row["bsl_method_count"] = int(cnt)
        return {"ok": True, "entity": _row_to_out(row) if row else None, "stats": stats}
    finally:
        conn.close()


@router.patch("/{entity_id}")
def patch_entity(entity_id: int, body: EntityPatch) -> EntityOut:
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        if body.enabled is not None:
            if body.enabled:
                # Re-enable BSL when methods already loaded
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
                    (entity_id,),
                ).fetchone()["c"]
                if int(cnt or 0) > 0:
                    ent_repo.set_status(
                        conn, entity_id, row["status"], enabled=1, bsl_enabled=1
                    )
                else:
                    ent_repo.set_status(conn, entity_id, row["status"], enabled=1)
            else:
                # Soft-disable context also disables BSL usage (flags persisted in SQLite)
                ent_repo.set_status(
                    conn, entity_id, row["status"], enabled=0, bsl_enabled=0
                )
        if body.bsl_enabled is not None and body.enabled is not False:
            ent_repo.set_status(
                conn, entity_id, row["status"], bsl_enabled=1 if body.bsl_enabled else 0
            )
        if body.tag_ids is not None:
            from app.repositories import tags as tag_repo

            for tid in body.tag_ids:
                if not tag_repo.get_tag(conn, tid):
                    raise HTTPException(404, f"Tag not found: {tid}")
            tag_repo.set_entity_tags(conn, entity_id, body.tag_ids)
        if body.model is not None and body.model != row["model"]:
            from app.repositories import objects as obj_repo

            # model change requires full reindex into a new collection
            obj_repo.reset_embed_flags(conn, entity_id)
            obj_repo.clear_pending_zvec_deletes(conn, entity_id)
            ent_repo.set_status(
                conn, entity_id, "parsed", model=body.model, indexed_count=0, index_target=0
            )
        if body.comment is not None:
            ent_repo.set_comment(conn, entity_id, body.comment)
        if body.name is not None:
            try:
                ent_repo.rename_entity(conn, entity_id, body.name)
            except KeyError as exc:
                raise HTTPException(404, "Entity not found") from exc
            except ValueError as exc:
                msg = str(exc)
                code = 409 if "busy" in msg.lower() or "already used" in msg.lower() else 400
                raise HTTPException(code, msg) from exc
        conn.commit()
        row = ent_repo.get_entity(conn, entity_id)
        from app.repositories import tags as tag_repo

        if row:
            row = dict(row)
            row["tag_ids"] = tag_repo.list_tag_ids_for_entity(conn, entity_id)
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
                (entity_id,),
            ).fetchone()["c"]
            row["bsl_method_count"] = int(cnt)
        return _row_to_out(row)
    finally:
        conn.close()


@router.delete("/{entity_id}")
def delete_entity(entity_id: int) -> StreamingResponse:
    """Delete entity; stream NDJSON progress lines for the UI progress modal."""

    def _emit(pct: int, step: str, detail: str = "") -> str:
        return json.dumps(
            {"pct": pct, "step": step, "detail": detail},
            ensure_ascii=False,
        ) + "\n"

    def generate() -> Iterator[str]:
        conn = connect(settings.db_path)
        try:
            row = ent_repo.get_entity(conn, entity_id)
            if not row:
                yield _emit(0, "error", "Entity not found")
                return
            name = row["name"] or f"#{entity_id}"
            yield _emit(5, "prepare", name)

            coll = collection_path(row["id"], row["model"])
            yield _emit(15, "close_index", name)
            zvec_store.release(coll)

            if coll.exists():
                yield _emit(30, "destroy_index", name)
                try:
                    import zvec

                    zvec.open(str(coll)).destroy()
                except Exception:
                    shutil.rmtree(coll, ignore_errors=True)
                yield _emit(65, "destroy_index", name)
            else:
                yield _emit(65, "destroy_index", name)

            yield _emit(75, "delete_file", name)
            extras: list[Path] = []
            if row.get("file_path"):
                extras.append(Path(row["file_path"]))
            delete_entity_upload_files(entity_id, extra_paths=extras)

            yield _emit(90, "delete_db", name)
            ent_repo.delete_entity(conn, entity_id)
            conn.commit()
            yield _emit(100, "done", name)
        except Exception as exc:
            yield _emit(0, "error", str(exc)[:300])
        finally:
            conn.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/{entity_id}/copy")
def copy_entity(entity_id: int, body: EntityCopy) -> StreamingResponse:
    """Copy a ready entity under a new name; stream NDJSON progress for the UI."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        if row.get("status") != "ready":
            raise HTTPException(409, "Only ready (fully indexed) entities can be copied")
        if ent_repo.get_entity_by_name(conn, name):
            raise HTTPException(409, f"Name already exists: {name}")
    finally:
        conn.close()

    def _emit(pct: int, step: str, detail: str = "", **extra) -> str:
        payload = {"pct": pct, "step": step, "detail": detail, **extra}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    def generate() -> Iterator[str]:
        try:
            for ev in iter_clone_entity(entity_id, name):
                step = str(ev.get("step") or "")
                pct = int(ev.get("pct") or 0)
                detail = str(ev.get("detail") or "")
                if step == "done":
                    new_id = int(ev["new_id"])
                    if ev.get("needs_reindex"):
                        jobs.submit(reindex_entity, new_id)
                    yield _emit(pct, step, detail, new_id=new_id)
                else:
                    yield _emit(pct, step, detail)
        except ValueError as exc:
            yield _emit(0, "error", str(exc)[:300])
        except Exception as exc:
            yield _emit(0, "error", ("Copy failed: " + str(exc))[:300])

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/{entity_id}/reindex")
def start_reindex(entity_id: int) -> ReindexResponse:
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        if row["status"] == "indexing":
            raise HTTPException(409, "Already indexing")
        if row["status"] == "parsing":
            raise HTTPException(409, "Still parsing")
        if row["object_count"] <= 0 and row["status"] not in {"parsed", "ready", "index_error"}:
            raise HTTPException(409, "Parse not finished")
    finally:
        conn.close()
    jobs.submit(reindex_entity, entity_id)
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        total = int(row["object_count"] or 0) if row else 0
    finally:
        conn.close()
    return ReindexResponse(
        id=entity_id,
        status="indexing",
        detail="Reindex started",
        indexed_count=0,
        object_count=total,
        progress_pct=0,
    )
