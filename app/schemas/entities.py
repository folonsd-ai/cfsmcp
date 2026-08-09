from __future__ import annotations

import time

from pydantic import BaseModel, computed_field


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "…"
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"~{sec}s"
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"~{m}m" if s < 15 else f"~{m}m{s:02d}s"
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"~{h}h{m:02d}m"


class EntityOut(BaseModel):
    id: int
    name: str
    synonym: str = ""
    comment: str = ""
    entity_type: str = "configuration"
    version: str = ""
    enabled: bool
    bsl_enabled: bool = True
    bsl_method_count: int = 0
    bsl_load_mode: str = ""
    bsl_embed_mode: str = ""
    tag_ids: list[int] = []
    model: str
    status: str
    name_locked: bool = False
    object_count: int = 0
    link_count: int = 0
    indexed_count: int = 0
    index_target: int = 0
    index_started_at: float = 0
    index_scope: str = ""
    parse_added: int = 0
    parse_changed: int = 0
    parse_deleted: int = 0
    parse_unchanged: int = 0
    error_message: str = ""
    usage_calls: int = 0
    usage_share_pct: int = 0
    usage_bar_pct: int = 0
    usage_window_sec: int = 600

    @computed_field
    @property
    def progress_pct(self) -> int:
        if self.status == "ready":
            return 100
        if self.status == "indexing":
            target = self.index_target or self.object_count or 0
            if target <= 0:
                return 100
            if self.indexed_count >= target:
                return 100
            return min(99, int(100 * self.indexed_count / target))
        if self.status == "parsing":
            return 0
        if self.status == "loading_modules":
            return 50
        return 0

    @computed_field
    @property
    def eta_sec(self) -> int | None:
        if self.status != "indexing":
            return None
        target = self.index_target or self.object_count or 0
        done = self.indexed_count or 0
        if target <= 0:
            return 0
        remaining = target - done
        if remaining <= 0:
            return 0
        started = float(self.index_started_at or 0)
        if started <= 0 or done <= 0:
            return None
        elapsed = time.time() - started
        if elapsed < 0.8:
            return None
        rate = done / elapsed
        if rate <= 0:
            return None
        return max(0, int(round(remaining / rate)))

    @computed_field
    @property
    def parse_delta(self) -> str:
        return (
            f"+{self.parse_added}~{self.parse_changed}"
            f"-{self.parse_deleted}={self.parse_unchanged}"
        )

    @property
    def _has_parse_stats(self) -> bool:
        return bool(
            self.parse_added
            or self.parse_changed
            or self.parse_deleted
            or self.parse_unchanged
        )

    @computed_field
    @property
    def progress_text(self) -> str:
        if self.status == "indexing":
            target = self.index_target or self.object_count or 0
            eta = format_eta(self.eta_sec)
            left = max(0, target - (self.indexed_count or 0))
            return f"{self.indexed_count}/{target} · {eta} · {left} left"
        if self.status == "parsing":
            return f"{self.object_count} objs · {self.link_count} links"
        if self.status == "loading_modules":
            return "BSL modules…"
        if self.status == "ready":
            if self._has_parse_stats:
                return f"{self.object_count} · {self.parse_delta}"
            return f"{self.object_count} objects"
        if self.status == "parsed":
            if self._has_parse_stats:
                return f"{self.object_count} · {self.parse_delta} · reindex"
            return f"{self.object_count} · reindex"
        if self.status == "uploaded":
            return "waiting"
        if self.status in {"parse_error", "index_error", "modules_error"}:
            return self.error_message or self.status
        return self.status


class EntityPatch(BaseModel):
    enabled: bool | None = None
    bsl_enabled: bool | None = None
    tag_ids: list[int] | None = None
    model: str | None = None
    comment: str | None = None
    name: str | None = None


class EntityCopy(BaseModel):
    name: str


class ReindexResponse(BaseModel):
    id: int
    status: str
    detail: str = ""
    indexed_count: int = 0
    object_count: int = 0
    progress_pct: int = 0