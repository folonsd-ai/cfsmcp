from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import entities as entities_api
from app.api import health as health_api
from app.api import settings as settings_api
from app.api import stats as stats_api
from app.core.config import settings
from app.core.database import init_db
from app.mcp.server import mcp
from app.services.usage_stats import usage_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cfsmcp")

mcp_app = mcp.http_app(path="/", transport="streamable-http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.metadata_dir.mkdir(parents=True, exist_ok=True)
    settings.zvec_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    async with mcp_app.lifespan(app):
        try:
            from app.services.pipeline import (
                backfill_entity_types,
                cleanup_metadata_orphans,
                recover_orphaned_jobs,
            )

            resumed = recover_orphaned_jobs()
            if any(resumed.values()):
                log.info("resumed orphaned jobs after restart: %s", resumed)
            cleaned = cleanup_metadata_orphans()
            removed = (
                cleaned.get("removed_temp", 0)
                + cleaned.get("removed_orphan_report", 0)
                + cleaned.get("removed_orphan_pending", 0)
                + cleaned.get("removed_orphan_other", 0)
            )
            if removed:
                log.info("cleaned metadata orphans on startup: %s", cleaned)
            typed = backfill_entity_types()
            if typed.get("updated"):
                log.info("backfilled entity_type on startup: %s", typed)
        except Exception:
            log.exception("failed to recover orphaned jobs / cleanup metadata")
        log.info("cfsmcp ready on %s:%s mcp=/mcp", settings.host, settings.port)
        yield


def create_app() -> FastAPI:
    api = FastAPI(title="cfsmcp", version="0.1.0", lifespan=lifespan)
    api.include_router(health_api.router)
    api.include_router(entities_api.router)
    api.include_router(settings_api.router)
    api.include_router(stats_api.router)

    from app.api import tags as tags_api

    api.include_router(tags_api.router)

    @api.middleware("http")
    async def mcp_trailing_slash(request: Request, call_next):
        # Cursor MCP client does not follow 307 /mcp -> /mcp/
        if request.scope.get("path") == "/mcp":
            request.scope["path"] = "/mcp/"
        return await call_next(request)

    @api.middleware("http")
    async def track_api_usage(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path.startswith("/api/stats"):
            return await call_next(request)
        # Normalize entity id paths: /api/entities/12/reindex -> /api/entities/{id}/reindex
        name_path = path
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "api" and parts[2] == "entities" and parts[3].isdigit():
            parts[3] = "{id}"
            name_path = "/".join(parts)
        t0 = time.perf_counter()
        ok = True
        detail = ""
        try:
            response = await call_next(request)
            ok = response.status_code < 400
            if not ok:
                detail = f"status={response.status_code}"
            return response
        except Exception as exc:
            ok = False
            detail = str(exc)[:200]
            raise
        finally:
            usage_stats.record(
                kind="api",
                name=f"{request.method} {name_path}",
                ok=ok,
                duration_ms=(time.perf_counter() - t0) * 1000,
                detail=detail,
            )

    static_dir = Path(__file__).parent / "static"
    api.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @api.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    api.mount("/mcp", mcp_app)
    return api


app = create_app()


def run() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()