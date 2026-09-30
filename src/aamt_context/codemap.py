"""A small repository map: the "relevant files" part of a task brief.

aamt's developer agent starts every attempt by calling ``repo_tree``/``read_file`` to
rediscover the codebase. A code map puts a compact outline of the most relevant files
(classes, functions and signatures — not bodies) into the brief, ranked by overlap
with the task text and by files named in upstream handoffs. It is the cheap cousin of
Aider's repo map (tree-sitter + PageRank); Python files use ``ast``, other files show
their first heading/lines. Outlines are cached by mtime.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ._util import clip_chars, terms
from .tokens import TokenCounter, default_counter

DEFAULT_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".toml", ".json", ".yaml", ".yml",
                ".sql", ".html", ".css", ".cfg", ".ini")
DEFAULT_EXCLUDE_DIRS = (".git", "__pycache__", ".venv", "venv", "node_modules", ".aamt",
                        ".pytest_cache", "dist", "build", ".mypy_cache", ".ruff_cache")


@dataclass
class FileOutline:
    path: str
    lines: list[str] = field(default_factory=list)
    words: set[str] = field(default_factory=set)
    mtime: float = 0.0


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(fn.args)
    except Exception:  # noqa: BLE001
        args = "..."
    ret = ""
    if fn.returns is not None:
        try:
            ret = " -> " + ast.unparse(fn.returns)
        except Exception:  # noqa: BLE001
            ret = ""
    prefix = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    return clip_chars(f"{prefix} {fn.name}({args}){ret}", 160)


def outline_python(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"(syntax error at line {exc.lineno})"]
    lines: list[str] = []
    doc = ast.get_docstring(tree)
    if doc:
        lines.append('"""' + clip_chars(doc.strip().splitlines()[0], 100) + '"""')
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and (not n.name.startswith("_") or n.name == "__init__")]
            bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            lines.append(f"class {node.name}({bases})" + (f": {', '.join(methods)}" if methods else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            lines.append(_signature(node))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper() or name in ("app", "router", "db", "engine", "api"):
                lines.append(f"{name} = …")
    return lines


def outline_text(source: str, *, max_lines: int = 3) -> list[str]:
    heads = [ln.strip() for ln in source.splitlines() if ln.strip().startswith("#")][:max_lines]
    if heads:
        return [clip_chars(h, 120) for h in heads]
    return [clip_chars(ln.strip(), 120) for ln in source.splitlines() if ln.strip()][:max_lines]


class CodeMap:
    def __init__(
        self,
        root: str | Path,
        *,
        exts: Sequence[str] = DEFAULT_EXTS,
        exclude_dirs: Sequence[str] = DEFAULT_EXCLUDE_DIRS,
        max_files: int = 3_000,
        max_file_bytes: int = 200_000,
    ):
        self.root = Path(root)
        self.exts = tuple(exts)
        self.exclude_dirs = set(exclude_dirs)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self._cache: dict[str, FileOutline] = {}

    def files(self) -> list[str]:
        out: list[str] = []
        if not self.root.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.exclude_dirs and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.endswith(self.exts):
                    out.append(Path(dirpath, fn).relative_to(self.root).as_posix())
                    if len(out) >= self.max_files:
                        return out
        return out

    def outline(self, rel: str) -> FileOutline:
        path = self.root / rel
        try:
            st = path.stat()
        except OSError:
            return FileOutline(rel)
        cached = self._cache.get(rel)
        if cached and cached.mtime == st.st_mtime:
            return cached
        try:
            source = path.read_bytes()[: self.max_file_bytes].decode("utf-8", errors="replace")
        except OSError:
            source = ""
        lines = outline_python(source) if rel.endswith(".py") else outline_text(source)
        words = set(terms(rel)) | set(terms(" ".join(lines)))
        out = FileOutline(rel, lines, words, st.st_mtime)
        self._cache[rel] = out
        return out

    def rank(self, query: str, *, hints: Iterable[str] = (), limit: int = 12) -> list[tuple[str, float]]:
        q = set(terms(query))
        hint_set = {h.replace("\\", "/").lower() for h in hints}
        wants_tests = bool(q & {"test", "tests", "pytest"})
        scored: list[tuple[str, float]] = []
        for rel in self.files():
            low = rel.lower()
            score = 0.0
            if low in hint_set or any(low.endswith("/" + h) for h in hint_set):
                score += 5.0
            path_terms = set(terms(rel))
            score += 2.0 * len(q & path_terms)
            if score or q:
                score += 1.0 * len(q & (self.outline(rel).words - path_terms))
            if "test" in low and not wants_tests:
                score *= 0.5
            if score > 0:
                scored.append((rel, score))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:limit]

    def render(
        self, query: str, budget_tokens: int, *, counter: TokenCounter | None = None,
        hints: Iterable[str] = (), limit: int = 12,
    ) -> tuple[str, list[str]]:
        counter = counter or default_counter()
        total = len(self.files())
        blocks: list[str] = []
        shown: list[str] = []
        used = 0
        for rel, _score in self.rank(query, hints=hints, limit=limit):
            ol = self.outline(rel)
            block = rel + "".join(f"\n  {ln}" for ln in ol.lines[:12])
            cost = counter.count(block) + 1
            if used + cost > budget_tokens - 20:
                break
            blocks.append(block)
            shown.append(rel)
            used += cost
        if not blocks:
            return "", []
        footer = f"(outlines of {len(shown)} of {total} files; open others with your file tools)"
        return "\n".join(blocks) + "\n" + footer, shown
