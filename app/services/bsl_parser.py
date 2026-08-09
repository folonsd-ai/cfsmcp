"""Extract procedure/function headers, doc-comments and optional bodies from 1C BSL modules."""

from __future__ import annotations

import re
from dataclasses import dataclass


_METHOD_RE = re.compile(
    r"(?P<kw>Процедура|Функция|Procedure|Function)\s+"
    r"(?P<name>[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?P<rest>[^\n]*)",
    re.MULTILINE,
)

_EXPORT_RE = re.compile(r"\b(Экспорт|Export)\b", re.IGNORECASE)

_END_PROC = re.compile(r"(?m)^\s*(КонецПроцедуры|EndProcedure)\b")
_END_FUNC = re.compile(r"(?m)^\s*(КонецФункции|EndFunction)\b")

BSL_LOAD_MODES = ("signatures", "code", "full")


@dataclass(frozen=True)
class BslMethod:
    name: str
    kind: str  # Procedure | Function
    signature: str
    doc: str
    body: str
    export: bool
    line: int  # 1-based


def _is_comment_line(line: str) -> bool:
    s = line.lstrip("\ufeff \t")
    return s.startswith("//")


def _strip_comment_marker(line: str) -> str:
    s = line.lstrip("\ufeff \t")
    if s.startswith("//"):
        s = s[2:]
        if s.startswith(" "):
            s = s[1:]
    return s.rstrip("\r\n")


def _collect_doc(lines: list[str], before_idx: int) -> str:
    """Collect contiguous // comments immediately above method (blank lines allowed in between)."""
    i = before_idx - 1
    block: list[str] = []
    while i >= 0:
        raw = lines[i]
        if not raw.strip():
            i -= 1
            continue
        if _is_comment_line(raw):
            block.append(_strip_comment_marker(raw))
            i -= 1
            continue
        break
    block.reverse()
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block).strip()


def _extract_body(text: str, sig_end: int, kind: str) -> str:
    """Body between signature and matching EndProcedure/EndFunction (exclusive)."""
    end_re = _END_FUNC if kind == "Function" else _END_PROC
    m = end_re.search(text, sig_end)
    if not m:
        return ""
    body = text[sig_end : m.start()]
    if body.startswith("\n"):
        body = body[1:]
    return body.rstrip("\r\n")


def parse_bsl_methods(
    source: str,
    *,
    include_doc: bool = True,
    include_body: bool = False,
) -> list[BslMethod]:
    """Parse BSL text into methods. Body/doc are optional by flags."""
    if source.startswith("\ufeff"):
        source = source[1:]
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out: list[BslMethod] = []
    seen: set[str] = set()

    for m in _METHOD_RE.finditer(text):
        kw = m.group("kw")
        name = m.group("name")
        params = (m.group("params") or "").strip()
        rest = (m.group("rest") or "").strip()
        if "//" in rest:
            rest = rest.split("//", 1)[0].rstrip()
        export_m = _EXPORT_RE.search(rest)
        export = bool(export_m)
        export_token = export_m.group(1) if export_m else ""
        kind = "Function" if kw.lower() in {"функция", "function"} else "Procedure"
        sig_core = f"{'Функция' if kind == 'Function' else 'Процедура'} {name}({params})"
        if export_token:
            signature = f"{sig_core} {export_token}"
        else:
            signature = sig_core
        line_no = text.count("\n", 0, m.start()) + 1
        doc = _collect_doc(lines, line_no - 1) if include_doc else ""
        body = _extract_body(text, m.end(), kind) if include_body else ""
        key = f"{kind}:{name}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            BslMethod(
                name=name,
                kind=kind,
                signature=signature,
                doc=doc,
                body=body,
                export=export,
                line=line_no,
            )
        )
    return out


def mode_flags(mode: str) -> tuple[bool, bool]:
    """Return (include_doc, include_body) for a load mode."""
    m = (mode or "signatures").strip().lower()
    if m == "full":
        return True, True
    if m == "code":
        return False, True
    return False, False
