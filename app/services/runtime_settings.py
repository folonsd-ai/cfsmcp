from __future__ import annotations

from app.core.config import settings
from app.core.database import connect

KEY_LM_URL = "lm_studio_url"
KEY_DEFAULT_MODEL = "default_embedding_model"
KEY_EMBEDDING_WORKERS = "embedding_workers"
KEY_BSL_EMBED_MODE = "bsl_embed_mode"
KEY_UI_POLL_INTERVAL_SEC = "ui_poll_interval_sec"
KEY_UI_SHOW_OBJECTS_COL = "ui_show_objects_col"
KEY_UI_SHOW_MODEL_COL = "ui_show_model_col"
KEY_STATS_WINDOW_SEC = "stats_window_sec"
KEY_BSL_PASSAGE_MAX_CHARS = "bsl_passage_max_chars"
KEY_BSL_CHUNK_SIZE = "bsl_chunk_size"
KEY_BSL_CHUNK_OVERLAP = "bsl_chunk_overlap"
KEY_BSL_MIN_BODY_CHARS = "bsl_min_body_chars"
KEY_BSL_MAX_CHUNKS = "bsl_max_chunks"
ALLOWED_EMBEDDING_WORKERS = frozenset({1, 2, 4})
# 0 = off; otherwise seconds between entity list polls
ALLOWED_UI_POLL_INTERVAL_SEC = frozenset({0, 2, 3, 5, 10, 15, 30, 60})
DEFAULT_UI_POLL_INTERVAL_SEC = 2
# In-memory usage stats retention (seconds): 1 min … 1 hour
ALLOWED_STATS_WINDOW_SEC = frozenset({60, 120, 300, 600, 900, 1800, 3600})
DEFAULT_STATS_WINDOW_SEC = 600


def get_setting(key: str, default: str) -> str:
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if row and str(row["value"]).strip():
            return str(row["value"]).strip()
        return default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = connect(settings.db_path)
    try:
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
            """,
            (key, value.strip()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_setting(key: str) -> None:
    conn = connect(settings.db_path)
    try:
        conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def _as_bool(raw: str, default: bool) -> bool:
    s = (raw or "").strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(raw: str, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)


def get_lm_studio_url() -> str:
    return get_setting(KEY_LM_URL, str(settings.lm_studio_url)).rstrip("/")


def get_default_embedding_model() -> str:
    return get_setting(KEY_DEFAULT_MODEL, settings.default_embedding_model)


def get_embedding_workers() -> int:
    raw = get_setting(KEY_EMBEDDING_WORKERS, str(settings.embedding_workers))
    try:
        n = int(raw)
    except ValueError:
        n = int(settings.embedding_workers)
    if n not in ALLOWED_EMBEDDING_WORKERS:
        return 2 if 2 in ALLOWED_EMBEDDING_WORKERS else min(ALLOWED_EMBEDDING_WORKERS)
    return n


def get_bsl_embed_mode() -> str:
    from app.services.bsl_embed import DEFAULT_BSL_EMBED_MODE, normalize_bsl_embed_mode

    return normalize_bsl_embed_mode(get_setting(KEY_BSL_EMBED_MODE, DEFAULT_BSL_EMBED_MODE))


def get_ui_poll_interval_sec() -> int:
    raw = get_setting(KEY_UI_POLL_INTERVAL_SEC, str(DEFAULT_UI_POLL_INTERVAL_SEC))
    try:
        n = int(raw)
    except ValueError:
        n = DEFAULT_UI_POLL_INTERVAL_SEC
    if n not in ALLOWED_UI_POLL_INTERVAL_SEC:
        return DEFAULT_UI_POLL_INTERVAL_SEC
    return n


def get_ui_show_objects_col() -> bool:
    return _as_bool(get_setting(KEY_UI_SHOW_OBJECTS_COL, "1"), True)


def get_ui_show_model_col() -> bool:
    return _as_bool(get_setting(KEY_UI_SHOW_MODEL_COL, "0"), False)


def get_stats_window_sec() -> int:
    raw = get_setting(KEY_STATS_WINDOW_SEC, str(DEFAULT_STATS_WINDOW_SEC))
    n = _as_int(raw, DEFAULT_STATS_WINDOW_SEC)
    if n not in ALLOWED_STATS_WINDOW_SEC:
        return DEFAULT_STATS_WINDOW_SEC
    return n


def get_bsl_embed_limits() -> dict[str, int]:
    from app.services.bsl_embed import default_bsl_embed_limits, normalize_bsl_embed_limits

    d = default_bsl_embed_limits()
    return normalize_bsl_embed_limits(
        passage_max_chars=_as_int(
            get_setting(KEY_BSL_PASSAGE_MAX_CHARS, str(d["passage_max_chars"])),
            d["passage_max_chars"],
        ),
        chunk_size=_as_int(
            get_setting(KEY_BSL_CHUNK_SIZE, str(d["chunk_size"])),
            d["chunk_size"],
        ),
        chunk_overlap=_as_int(
            get_setting(KEY_BSL_CHUNK_OVERLAP, str(d["chunk_overlap"])),
            d["chunk_overlap"],
        ),
        min_body_chars=_as_int(
            get_setting(KEY_BSL_MIN_BODY_CHARS, str(d["min_body_chars"])),
            d["min_body_chars"],
        ),
        max_chunks=_as_int(
            get_setting(KEY_BSL_MAX_CHUNKS, str(d["max_chunks"])),
            d["max_chunks"],
        ),
    )


def set_bsl_embed_limits(
    *,
    passage_max_chars: int | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    min_body_chars: int | None = None,
    max_chunks: int | None = None,
) -> dict[str, int]:
    from app.services.bsl_embed import normalize_bsl_embed_limits

    lim = normalize_bsl_embed_limits(
        passage_max_chars=passage_max_chars,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        min_body_chars=min_body_chars,
        max_chunks=max_chunks,
        base=get_bsl_embed_limits(),
    )
    set_setting(KEY_BSL_PASSAGE_MAX_CHARS, str(lim["passage_max_chars"]))
    set_setting(KEY_BSL_CHUNK_SIZE, str(lim["chunk_size"]))
    set_setting(KEY_BSL_CHUNK_OVERLAP, str(lim["chunk_overlap"]))
    set_setting(KEY_BSL_MIN_BODY_CHARS, str(lim["min_body_chars"]))
    set_setting(KEY_BSL_MAX_CHUNKS, str(lim["max_chunks"]))
    return lim


def reset_bsl_embed_limits() -> dict[str, int]:
    """Remove overrides so getters fall back to code defaults."""
    for key in (
        KEY_BSL_PASSAGE_MAX_CHARS,
        KEY_BSL_CHUNK_SIZE,
        KEY_BSL_CHUNK_OVERLAP,
        KEY_BSL_MIN_BODY_CHARS,
        KEY_BSL_MAX_CHUNKS,
    ):
        delete_setting(key)
    return get_bsl_embed_limits()


def get_all() -> dict:
    lim = get_bsl_embed_limits()
    from app.services.bsl_embed import (
        bsl_embed_limits_bounds,
        bsl_embed_window_presets,
        default_bsl_embed_limits,
    )

    return {
        "lm_studio_url": get_lm_studio_url(),
        "default_embedding_model": get_default_embedding_model(),
        "embedding_workers": get_embedding_workers(),
        "bsl_embed_mode": get_bsl_embed_mode(),
        "ui_poll_interval_sec": get_ui_poll_interval_sec(),
        "ui_show_objects_col": get_ui_show_objects_col(),
        "ui_show_model_col": get_ui_show_model_col(),
        "stats_window_sec": get_stats_window_sec(),
        "bsl_passage_max_chars": lim["passage_max_chars"],
        "bsl_chunk_size": lim["chunk_size"],
        "bsl_chunk_overlap": lim["chunk_overlap"],
        "bsl_min_body_chars": lim["min_body_chars"],
        "bsl_max_chunks": lim["max_chunks"],
        "bsl_embed_limits_defaults": default_bsl_embed_limits(),
        "bsl_embed_limits_bounds": bsl_embed_limits_bounds(),
        "bsl_embed_window_presets": bsl_embed_window_presets(),
    }


def update(
    *,
    lm_studio_url: str | None = None,
    default_embedding_model: str | None = None,
    embedding_workers: int | None = None,
    bsl_embed_mode: str | None = None,
    ui_poll_interval_sec: int | None = None,
    ui_show_objects_col: bool | None = None,
    ui_show_model_col: bool | None = None,
    stats_window_sec: int | None = None,
    bsl_passage_max_chars: int | None = None,
    bsl_chunk_size: int | None = None,
    bsl_chunk_overlap: int | None = None,
    bsl_min_body_chars: int | None = None,
    bsl_max_chunks: int | None = None,
) -> dict:
    if lm_studio_url is not None:
        set_setting(KEY_LM_URL, lm_studio_url.rstrip("/"))
        from app.services.embeddings import clear_query_embedding_cache

        clear_query_embedding_cache()
    if default_embedding_model is not None:
        set_setting(KEY_DEFAULT_MODEL, default_embedding_model)
    if embedding_workers is not None:
        set_setting(KEY_EMBEDDING_WORKERS, str(int(embedding_workers)))
    if bsl_embed_mode is not None:
        from app.services.bsl_embed import normalize_bsl_embed_mode

        set_setting(KEY_BSL_EMBED_MODE, normalize_bsl_embed_mode(bsl_embed_mode))
    if ui_poll_interval_sec is not None:
        set_setting(KEY_UI_POLL_INTERVAL_SEC, str(int(ui_poll_interval_sec)))
    if ui_show_objects_col is not None:
        set_setting(KEY_UI_SHOW_OBJECTS_COL, "1" if ui_show_objects_col else "0")
    if ui_show_model_col is not None:
        set_setting(KEY_UI_SHOW_MODEL_COL, "1" if ui_show_model_col else "0")
    if stats_window_sec is not None:
        set_setting(KEY_STATS_WINDOW_SEC, str(int(stats_window_sec)))
    if any(
        v is not None
        for v in (
            bsl_passage_max_chars,
            bsl_chunk_size,
            bsl_chunk_overlap,
            bsl_min_body_chars,
            bsl_max_chunks,
        )
    ):
        set_bsl_embed_limits(
            passage_max_chars=bsl_passage_max_chars,
            chunk_size=bsl_chunk_size,
            chunk_overlap=bsl_chunk_overlap,
            min_body_chars=bsl_min_body_chars,
            max_chunks=bsl_max_chunks,
        )
    return get_all()
