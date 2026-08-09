from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from app.services.usage_stats import usage_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
def get_stats():
    return usage_stats.snapshot()


@router.post("/reset")
def reset_stats():
    usage_stats.reset()
    return {"ok": True, **usage_stats.snapshot()}


@router.get("/ingest-log.txt")
def download_ingest_log():
    body = usage_stats.export_ingest_log_text()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="cfsmcp-bsl-ingest-log.txt"',
        },
    )
