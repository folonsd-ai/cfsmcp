from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.database import connect
from app.schemas.settings import (
    DbInfoOut,
    ModelsResponse,
    SettingsOut,
    SettingsPatch,
    VacuumResponse,
)
from app.services import lm_studio, runtime_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


@router.get("")
def get_settings() -> SettingsOut:
    data = runtime_settings.get_all()
    return SettingsOut(**data)


@router.patch("")
def patch_settings(body: SettingsPatch) -> SettingsOut:
    from app.services.bsl_embed import ALLOWED_BSL_EMBED_MODES

    if body.lm_studio_url is not None:
        url = body.lm_studio_url.strip().rstrip("/")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(400, "lm_studio_url must start with http:// or https://")
    if body.embedding_workers is not None:
        if body.embedding_workers not in runtime_settings.ALLOWED_EMBEDDING_WORKERS:
            raise HTTPException(400, "embedding_workers must be one of: 1, 2, 4")
    if body.bsl_embed_mode is not None:
        raw = body.bsl_embed_mode.strip().lower()
        if raw not in ALLOWED_BSL_EMBED_MODES:
            raise HTTPException(
                400,
                "bsl_embed_mode must be one of: " + ", ".join(sorted(ALLOWED_BSL_EMBED_MODES)),
            )
        body.bsl_embed_mode = raw
    if body.ui_poll_interval_sec is not None:
        if body.ui_poll_interval_sec not in runtime_settings.ALLOWED_UI_POLL_INTERVAL_SEC:
            raise HTTPException(
                400,
                "ui_poll_interval_sec must be one of: "
                + ", ".join(str(x) for x in sorted(runtime_settings.ALLOWED_UI_POLL_INTERVAL_SEC)),
            )
    if body.stats_window_sec is not None:
        if body.stats_window_sec not in runtime_settings.ALLOWED_STATS_WINDOW_SEC:
            raise HTTPException(
                400,
                "stats_window_sec must be one of: "
                + ", ".join(str(x) for x in sorted(runtime_settings.ALLOWED_STATS_WINDOW_SEC)),
            )
    data = runtime_settings.update(
        lm_studio_url=body.lm_studio_url,
        default_embedding_model=body.default_embedding_model,
        embedding_workers=body.embedding_workers,
        bsl_embed_mode=body.bsl_embed_mode,
        ui_poll_interval_sec=body.ui_poll_interval_sec,
        ui_show_objects_col=body.ui_show_objects_col,
        ui_show_model_col=body.ui_show_model_col,
        stats_window_sec=body.stats_window_sec,
        bsl_passage_max_chars=body.bsl_passage_max_chars,
        bsl_chunk_size=body.bsl_chunk_size,
        bsl_chunk_overlap=body.bsl_chunk_overlap,
        bsl_min_body_chars=body.bsl_min_body_chars,
        bsl_max_chunks=body.bsl_max_chunks,
    )
    return SettingsOut(**data)


@router.post("/bsl-embed-limits/reset")
def reset_bsl_embed_limits() -> SettingsOut:
    """Restore BSL embedding char limits to built-in defaults."""
    runtime_settings.reset_bsl_embed_limits()
    return SettingsOut(**runtime_settings.get_all())


@router.get("/models")
def list_models() -> ModelsResponse:
    result = lm_studio.list_embedding_models()
    return ModelsResponse(
        ok=result["ok"],
        lm_studio_url=result["lm_studio_url"],
        source=result.get("source") or "",
        error=result.get("error"),
        default_embedding_model=runtime_settings.get_default_embedding_model(),
        models=result.get("models") or [],
    )


@router.get("/ping")
def ping_lm() -> dict:
    return lm_studio.ping()


@router.get("/db")
def db_info() -> DbInfoOut:
    db = Path(settings.db_path)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    return DbInfoOut(
        db_path=str(db),
        db_bytes=db.stat().st_size if db.exists() else 0,
        wal_bytes=(wal.stat().st_size if wal.exists() else 0)
        + (shm.stat().st_size if shm.exists() else 0),
        zvec_bytes=_dir_size(Path(settings.zvec_dir)),
    )


@router.post("/vacuum")
def vacuum_db() -> VacuumResponse:
    """Compact SQLite (VACUUM) after WAL checkpoint. Prefer when UI is idle."""
    db = Path(settings.db_path)
    before = db.stat().st_size if db.exists() else 0
    conn = connect(settings.db_path)
    try:
        # VACUUM cannot run inside an open transaction
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.isolation_level = ""
    except Exception as exc:
        raise HTTPException(500, f"VACUUM failed: {exc}") from exc
    finally:
        conn.close()
    after = db.stat().st_size if db.exists() else 0
    saved = max(0, before - after)
    return VacuumResponse(
        ok=True,
        before_bytes=before,
        after_bytes=after,
        db_path=str(db),
        detail=f"freed {saved} bytes" if saved else "no size change",
    )
