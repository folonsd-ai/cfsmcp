from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


WINDOW_SEC = 10 * 60  # default; live value from runtime setting stats_window_sec


def _active_window_sec() -> int:
    try:
        from app.services import runtime_settings

        return int(runtime_settings.get_stats_window_sec())
    except Exception:
        return WINDOW_SEC


@dataclass
class _Event:
    ts: float
    kind: str  # mcp | api | embed | index
    name: str
    ok: bool
    duration_ms: float
    context: str = ""
    detail: str = ""


@dataclass
class _IngestLog:
    ts: float
    context: str
    group: str  # summary | no_parent | form_bin_failed | unresolved | info
    detail: str


@dataclass
class _IngestReport:
    ts: float
    context: str
    text: str


def _fmt_ts(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def build_bsl_ingest_report(*, context: str, stats: dict[str, Any], ts: float | None = None) -> str:
    """Full diagnostic text for one BSL modules upload (UI + file export)."""
    when = _fmt_ts(ts if ts is not None else time.time())
    ctx = (context or "").strip() or "—"
    lines: list[str] = [
        "=== cfsmcp BSL ingest log ===",
        f"time={when}",
        f"context={ctx}",
        f"entity_id={stats.get('entity_id', '')}",
        f"load_mode={stats.get('bsl_load_mode') or ''}",
        f"embed_mode={stats.get('bsl_embed_mode') or ''}",
        (
            f"embed_coerced={bool(stats.get('embed_mode_coerced'))}"
            + (
                f" (requested={stats.get('embed_mode_requested')})"
                if stats.get("embed_mode_coerced")
                else ""
            )
        ),
        f"objects_in_report={stats.get('object_count', '')}",
        f"parent_paths_available={stats.get('parent_object_count', '')}",
        f"reindex_queued={stats.get('reindex_queued', '')}",
        "",
        "--- counts ---",
        (
            f"methods_loaded={int(stats.get('methods_loaded') or 0)} "
            f"(+{int(stats.get('methods_added') or 0)} "
            f"~{int(stats.get('methods_changed') or 0)} "
            f"-{int(stats.get('methods_deleted') or 0)} "
            f"unchanged={int(stats.get('methods_unchanged') or 0)})"
        ),
        f"methods_skipped_no_parent={int(stats.get('methods_skipped_no_parent') or 0)}",
        f"modules_unresolved={int(stats.get('modules_unresolved') or 0)}",
        f"files_bsl={int(stats.get('files_bsl') or 0)}",
        (
            f"files_form_bin={int(stats.get('files_form_bin') or 0)} "
            f"(extracted={int(stats.get('form_bin_extracted') or 0)}, "
            f"empty={int(stats.get('form_bin_empty') or 0)}, "
            f"failed={int(stats.get('form_bin_failed') or 0)}, "
            f"skipped_managed={int(stats.get('form_bin_skipped_managed') or 0)})"
        ),
        f"files_ignored={int(stats.get('files_ignored') or 0)}",
        f"bsl_non_utf8={int(stats.get('bsl_non_utf8') or 0)}",
        f"bsl_replacements={int(stats.get('bsl_replacements') or 0)}",
        "",
        "--- hints ---",
        "no_parent: dump path mapped to expected_parent, but that path is absent in the "
        "metadata report objects. Common on macOS: NFD filenames (й as и+breve) — "
        "server now NFC-normalizes paths; re-upload after update. Also: report/export "
        "mismatch or актив_* obsolete forms.",
        "form_bin_empty: Form.bin without Процедура/Функция (UI-only / empty module) — "
        "not an error; counted separately from form_bin_failed.",
        "form_bin_failed: Ext/Form.bin extract failed (corrupt, unexpected encoding, "
        "trim failure) — see size, keyword probes, head_hex in each sample.",
        "unresolved: path did not match resolve_bsl_module — see reason= and parts=.",
        f"sample_cap_per_group={int(stats.get('sample_cap') or 25)} "
        "(not all errors listed; totals are in counts)",
        "",
    ]

    no_parent = list(stats.get("sample_no_parent") or [])
    lines.append(f"--- samples: no_parent ({len(no_parent)}) ---")
    lines.extend(no_parent or ["(none)"])
    lines.append("")

    form_fail = list(stats.get("sample_form_bin_failed") or [])
    lines.append(f"--- samples: form_bin_failed ({len(form_fail)}) ---")
    lines.extend(form_fail or ["(none)"])
    lines.append("")

    unresolved = list(stats.get("sample_unresolved") or [])
    lines.append(f"--- samples: unresolved ({len(unresolved)}) ---")
    lines.extend(unresolved or ["(none)"])
    lines.append("")
    lines.append("=== end ===")
    return "\n".join(lines)


class UsageStats:
    """In-memory usage stats for the configured window; cleared on process restart."""

    def __init__(
        self,
        window_sec: int = WINDOW_SEC,
        max_events: int = 20000,
        max_ingest_logs: int = 600,
        max_ingest_reports: int = 40,
    ) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.window_sec = window_sec  # fallback only; live value from settings
        self.max_events = max_events
        self.events: deque[_Event] = deque(maxlen=max_events)
        # BSL upload samples/reports: keep by count until reset/restart
        self.ingest_logs: deque[_IngestLog] = deque(maxlen=max_ingest_logs)
        self.ingest_reports: deque[_IngestReport] = deque(maxlen=max_ingest_reports)

    def effective_window_sec(self) -> int:
        return _active_window_sec()

    def _cutoff(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.effective_window_sec()

    def _prune(self, now: float | None = None) -> None:
        cutoff = self._cutoff(now)
        while self.events and self.events[0].ts < cutoff:
            self.events.popleft()

    def record(
        self,
        *,
        kind: str,
        name: str,
        ok: bool = True,
        duration_ms: float = 0.0,
        context: str = "",
        detail: str = "",
    ) -> None:
        now = time.time()
        ev = _Event(
            ts=now,
            kind=kind,
            name=name,
            ok=ok,
            duration_ms=duration_ms,
            context=context or "",
            detail=detail or "",
        )
        with self._lock:
            self._prune(now)
            self.events.append(ev)

    def reset(self) -> None:
        with self._lock:
            self.started_at = time.time()
            self.events.clear()
            self.ingest_logs.clear()
            self.ingest_reports.clear()

    def record_bsl_ingest(self, *, context: str, stats: dict[str, Any]) -> None:
        """Store summary samples + full diagnostic report from a modules upload."""
        now = time.time()
        ctx = (context or "").strip() or "—"
        report_text = build_bsl_ingest_report(context=ctx, stats=stats, ts=now)
        summary = (
            f"methods={int(stats.get('methods_loaded') or 0)} "
            f"(+{int(stats.get('methods_added') or 0)} "
            f"~{int(stats.get('methods_changed') or 0)} "
            f"-{int(stats.get('methods_deleted') or 0)}); "
            f"no_parent={int(stats.get('methods_skipped_no_parent') or 0)}; "
            f"unresolved={int(stats.get('modules_unresolved') or 0)}; "
            f"bsl={int(stats.get('files_bsl') or 0)}; "
            f"Form.bin ok={int(stats.get('form_bin_extracted') or 0)} "
            f"empty={int(stats.get('form_bin_empty') or 0)} "
            f"fail={int(stats.get('form_bin_failed') or 0)}; "
            f"parents={stats.get('parent_object_count', '')}; "
            f"mode={stats.get('bsl_load_mode') or ''}/{stats.get('bsl_embed_mode') or ''}"
        )
        rows: list[_IngestLog] = [
            _IngestLog(ts=now, context=ctx, group="summary", detail=summary)
        ]
        for line in list(stats.get("sample_no_parent") or [])[:30]:
            rows.append(
                _IngestLog(ts=now, context=ctx, group="no_parent", detail=str(line)[:2000])
            )
        for line in list(stats.get("sample_form_bin_failed") or [])[:30]:
            rows.append(
                _IngestLog(
                    ts=now, context=ctx, group="form_bin_failed", detail=str(line)[:2000]
                )
            )
        for line in list(stats.get("sample_unresolved") or [])[:30]:
            rows.append(
                _IngestLog(ts=now, context=ctx, group="unresolved", detail=str(line)[:2000])
            )
        with self._lock:
            self.ingest_reports.append(
                _IngestReport(ts=now, context=ctx, text=report_text)
            )
            for row in rows:
                self.ingest_logs.append(row)

    def export_ingest_log_text(self) -> str:
        with self._lock:
            reports = list(self.ingest_reports)
        if not reports:
            return (
                "=== cfsmcp BSL ingest log ===\n"
                "(empty — upload modules first; logs live in memory until reset/restart)\n"
            )
        blocks = [r.text for r in reports]
        header = (
            f"=== cfsmcp ingest export ===\n"
            f"exported_at={_fmt_ts(time.time())}\n"
            f"reports={len(blocks)}\n\n"
        )
        return header + "\n\n".join(blocks) + "\n"

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._prune(now)
            events = list(self.events)
            ingest_logs = list(self.ingest_logs)
            ingest_reports = list(self.ingest_reports)
            started = self.started_at
            window_sec = self.effective_window_sec()

        totals: dict[str, int] = defaultdict(int)
        errors: dict[str, int] = defaultdict(int)
        dur_sum: dict[str, float] = defaultdict(float)
        dur_cnt: dict[str, int] = defaultdict(int)
        by_context: dict[str, int] = defaultdict(int)

        for ev in events:
            key = f"{ev.kind}:{ev.name}"
            totals[key] += 1
            if not ev.ok:
                errors[key] += 1
            if ev.duration_ms > 0:
                dur_sum[key] += ev.duration_ms
                dur_cnt[key] += 1
            if ev.context:
                by_context[ev.context] += 1

        by_tool: dict[str, dict[str, Any]] = {}
        for key, count in sorted(totals.items(), key=lambda x: -x[1]):
            kind, name = key.split(":", 1)
            avg = (dur_sum.get(key, 0.0) / dur_cnt[key]) if dur_cnt.get(key) else 0.0
            by_tool[key] = {
                "kind": kind,
                "name": name,
                "calls": count,
                "errors": errors.get(key, 0),
                "avg_ms": round(avg, 1),
            }

        # per-minute buckets for the retention window
        bucket_sec = 60
        window = max(1, window_sec // bucket_sec)
        start_bucket = int(now // bucket_sec) - (window - 1)
        timeline = []
        for i in range(window):
            b = start_bucket + i
            timeline.append(
                {
                    "t": datetime.fromtimestamp(b * bucket_sec, tz=timezone.utc)
                    .astimezone()
                    .strftime("%H:%M"),
                    "ts": b * bucket_sec,
                    "mcp": 0,
                    "api": 0,
                    "embed": 0,
                    "index": 0,
                    "errors": 0,
                }
            )
        idx = {row["ts"]: row for row in timeline}
        for ev in events:
            b = int(ev.ts // bucket_sec) * bucket_sec
            row = idx.get(b)
            if row is None:
                continue
            if ev.kind in row:
                row[ev.kind] += 1
            if not ev.ok:
                row["errors"] += 1

        mcp_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "mcp")
        api_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "api")
        embed_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "embed")
        index_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "index")
        err_total = sum(errors.values())

        recent = [
            {
                "ts": datetime.fromtimestamp(e.ts, tz=timezone.utc)
                .astimezone()
                .strftime("%H:%M:%S"),
                "kind": e.kind,
                "name": e.name,
                "ok": e.ok,
                "ms": round(e.duration_ms, 1),
                "context": e.context,
                "detail": e.detail[:120],
            }
            for e in list(reversed(events))[:25]
        ]

        ingest = [
            {
                "ts": datetime.fromtimestamp(e.ts, tz=timezone.utc)
                .astimezone()
                .strftime("%H:%M:%S"),
                "context": e.context,
                "group": e.group,
                "detail": e.detail,
            }
            for e in list(reversed(ingest_logs))[:250]
        ]

        reports = [
            {
                "ts": datetime.fromtimestamp(e.ts, tz=timezone.utc)
                .astimezone()
                .strftime("%H:%M:%S"),
                "context": e.context,
                "text": e.text,
            }
            for e in list(reversed(ingest_reports))
        ]

        return {
            "started_at": datetime.fromtimestamp(started, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            "uptime_sec": int(now - started),
            "window_sec": window_sec,
            "ephemeral": True,
            "summary": {
                "total_events": len(events),
                "mcp_calls": mcp_calls,
                "api_calls": api_calls,
                "embed_calls": embed_calls,
                "index_ops": index_calls,
                "errors": err_total,
            },
            "by_name": list(by_tool.values()),
            "by_context": [
                {"context": k, "calls": v}
                for k, v in sorted(by_context.items(), key=lambda x: -x[1])[:20]
            ],
            "timeline": timeline,
            "recent": recent,
            "ingest_logs": ingest,
            "ingest_reports": reports,
        }

    def mcp_usage_by_context(self) -> dict[str, Any]:
        """MCP call counts per context name within the active retention window."""
        now = time.time()
        with self._lock:
            self._prune(now)
            events = list(self.events)
            window_sec = self.effective_window_sec()

        by_name: dict[str, int] = defaultdict(int)
        for ev in events:
            if ev.kind != "mcp":
                continue
            ctx = (ev.context or "").strip()
            if not ctx:
                continue
            by_name[ctx] += 1
        total = sum(by_name.values())
        max_calls = max(by_name.values()) if by_name else 0
        return {
            "by_name": dict(by_name),
            "total": total,
            "max_calls": max_calls,
            "window_sec": window_sec,
        }


usage_stats = UsageStats()
