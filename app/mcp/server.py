from __future__ import annotations

import time
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import tags as tag_repo
from app.services import search as search_svc
from app.services.usage_stats import usage_stats

_CTX_DESC = (
    "Entity name from list_contexts, or tag:TagName from list_context_groups "
    "(searches all ready contexts with that tag). "
    "Example: КомплекснаяАвтоматизация or tag:КА2"
)
_CTX_SINGLE_DESC = (
    "Single entity name from list_contexts or from a search hit's 'context' field. "
    "Do not pass tag:… here — resolve the entity first via search / list_context_groups."
)

mcp = FastMCP(
    name="cfsmcp",
    instructions=(
        "Search 1C metadata and BSL method docs by context (configuration/extension name) "
        "or by tag group (tag:Name). "
        "Workflow: list_contexts / list_context_groups → "
        "search_metadata | semantic_search | find_methods (context may be tag:КА2) → "
        "then get_object / get_method / list_methods with the entity name from each hit's "
        "'context' field (never pass tag: to get_*). "
        "Prefer find_methods for natural-language questions about behavior; "
        "use list_methods to enumerate a module's procedures/functions."
    ),
)


def _track(name: str, fn, *, context: str = ""):
    t0 = time.perf_counter()
    ok = True
    detail = ""
    try:
        result = fn()
        if isinstance(result, dict) and result.get("error"):
            ok = False
            detail = str(result.get("error"))[:200]
        return result
    except Exception as exc:
        ok = False
        detail = str(exc)[:200]
        raise
    finally:
        usage_stats.record(
            kind="mcp",
            name=name,
            ok=ok,
            duration_ms=(time.perf_counter() - t0) * 1000,
            context=context,
            detail=detail,
        )


@mcp.tool(
    name="list_contexts",
    annotations={
        "title": "List metadata contexts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_contexts() -> list[dict]:
    """List enabled ready contexts. Each item includes tags (e.g. ["КА2"])."""

    def _run():
        conn = connect(settings.db_path)
        try:
            return ent_repo.list_ready_contexts(conn)
        finally:
            conn.close()

    return _track("list_contexts", _run)


@mcp.tool(
    name="list_context_groups",
    annotations={
        "title": "List context groups by tag",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_context_groups() -> list[dict]:
    """List tags that group ready contexts. Use context_ref (tag:Name) in search tools."""

    def _run():
        conn = connect(settings.db_path)
        try:
            return tag_repo.list_context_groups(conn)
        finally:
            conn.close()

    return _track("list_context_groups", _run)


@mcp.tool(
    name="search_metadata",
    annotations={
        "title": "FTS search metadata",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def search_metadata(
    context: Annotated[str, Field(description=_CTX_DESC)],
    query: Annotated[str, Field(description="Full-text search query")],
    kind: Annotated[str | None, Field(description="Optional kind filter, e.g. Catalog, Document")] = None,
    include_borrowed: Annotated[bool, Field(description="Include borrowed objects")] = False,
    limit: Annotated[int, Field(description="Page size", ge=1, le=200)] = 50,
    offset: Annotated[int, Field(description="Offset for pagination", ge=0)] = 0,
) -> dict:
    """Full-text search over metadata (and BSL methods if enabled). tag:… searches all members; each hit has context."""

    def _run():
        try:
            return search_svc.fts_search(
                context,
                query,
                kind=kind,
                include_borrowed=include_borrowed,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track("search_metadata", _run, context=context)


@mcp.tool(
    name="semantic_search",
    annotations={
        "title": "Semantic/hybrid metadata search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def semantic_search(
    context: Annotated[str, Field(description=_CTX_DESC)],
    query: Annotated[str, Field(description="Natural language query")],
    top_n: Annotated[int, Field(description="Number of results", ge=1, le=100)] = 20,
) -> dict:
    """Hybrid vector + FTS search. tag:… fans out across the group; hits include context."""

    def _run():
        try:
            return search_svc.semantic_search(context, query, top_n=top_n)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("semantic_search", _run, context=context)


@mcp.tool(
    name="get_object",
    annotations={
        "title": "Get metadata object",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_object(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[str, Field(description="Full object path, e.g. Catalogs.X.Attributes.Y or Russian path")],
) -> dict:
    """Return object with full properties. Requires a single entity name (not tag:)."""

    def _run():
        try:
            return search_svc.get_object(context, path)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_object", _run, context=context)


@mcp.tool(
    name="get_links",
    annotations={
        "title": "Get metadata links",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_links(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    object: Annotated[str, Field(description="Object path")],
    direction: Annotated[
        Literal["both", "in", "out", "incoming", "outgoing"],
        Field(description="Link direction"),
    ] = "both",
) -> dict:
    """Return incoming/outgoing links for an object. Requires a single entity name (not tag:)."""

    def _run():
        try:
            return search_svc.get_links(context, object, direction)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_links", _run, context=context)


@mcp.tool(
    name="list_code_modules",
    annotations={
        "title": "List modules with BSL methods",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_code_modules(
    context: Annotated[str, Field(description=_CTX_DESC)],
    kind: Annotated[
        str | None,
        Field(description="Optional parent kind filter, e.g. CommonModule, Catalog"),
    ] = None,
    q: Annotated[str | None, Field(description="Optional substring filter on path/name")] = None,
) -> dict:
    """List parents that have BSL methods. tag:… returns modules from all group members (each has context)."""

    def _run():
        try:
            return search_svc.list_code_modules(context, kind=kind, q=q)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("list_code_modules", _run, context=context)


@mcp.tool(
    name="list_methods",
    annotations={
        "title": "List BSL methods",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_methods(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    parent_path: Annotated[
        str | None,
        Field(description="Parent metadata path from list_code_modules, e.g. ОбщиеМодули.X"),
    ] = None,
    q: Annotated[str | None, Field(description="Substring filter on name/signature/doc")] = None,
    export_only: Annotated[bool, Field(description="Only Экспорт methods")] = False,
    limit: Annotated[int, Field(description="Max methods to return", ge=1, le=500)] = 50,
) -> dict:
    """Enumerate procedures/functions. Requires a single entity name (not tag:)."""

    def _run():
        try:
            return search_svc.list_methods(
                context,
                parent_path=parent_path,
                q=q,
                export_only=export_only,
                limit=limit,
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track("list_methods", _run, context=context)


@mcp.tool(
    name="get_method",
    annotations={
        "title": "Get BSL method details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_method(
    context: Annotated[str, Field(description=_CTX_SINGLE_DESC)],
    path: Annotated[
        str,
        Field(description="Method path from list_methods/find_methods, e.g. ОбщиеМодули.X.Методы.Module.Сохранить"),
    ],
) -> dict:
    """Full method description. Requires a single entity name (not tag:)."""

    def _run():
        try:
            return search_svc.get_method(context, path)
        except Exception as exc:
            return {"error": str(exc)}

    return _track("get_method", _run, context=context)


@mcp.tool(
    name="find_methods",
    annotations={
        "title": "Find BSL methods by meaning",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def find_methods(
    context: Annotated[str, Field(description=_CTX_DESC)],
    query: Annotated[str, Field(description="Natural language or keyword query about behavior")],
    export_only: Annotated[bool, Field(description="Only Экспорт methods")] = False,
    top_n: Annotated[int, Field(description="Number of results", ge=1, le=100)] = 15,
) -> dict:
    """Semantic/FTS over procedures/functions. tag:… searches the whole group; hits include context."""

    def _run():
        try:
            return search_svc.find_methods(
                context, query, export_only=export_only, top_n=top_n
            )
        except Exception as exc:
            return {"error": str(exc)}

    return _track("find_methods", _run, context=context)
