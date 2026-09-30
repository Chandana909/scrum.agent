"""Spike: aider-style repo map — tree-sitter tags for many languages + personalised PageRank.

Needs an aider checkout for its tag queries (Apache-2.0), e.g.
    git clone --depth 1 --filter=blob:none --sparse https://github.com/Aider-AI/aider.git
    cd aider && git sparse-checkout set aider/queries/tree-sitter-language-pack
Run:
    python spike_repomap.py <path-to-aider-checkout>
Deps: grep-ast, tree-sitter-language-pack (no networkx/scipy: PageRank below is ~15 lines).
Verified 2026-09-29 on Windows / Python 3.11: Python and JavaScript tags extracted, auth.py ranked first
for an auth-flavoured personalisation.
"""

from __future__ import annotations

import sys
from pathlib import Path

from grep_ast import filename_to_lang
from grep_ast.tsl import get_language, get_parser
from tree_sitter import Query

try:  # tree-sitter >= 0.25
    from tree_sitter import QueryCursor
except ImportError:  # pragma: no cover - older bindings
    QueryCursor = None

FILES = {
    "src/auth.py": "import jwt\nclass TokenService:\n    def issue(self, user):\n        return jwt.encode({'sub': user}, KEY)\n"
                   "def login(u, p):\n    return TokenService().issue(u)\n",
    "src/api.py": "from auth import login\ndef post_login(req):\n    return login(req.user, req.pw)\n"
                  "def get_tasks(req):\n    return list_tasks()\n",
    "src/tasks.py": "def list_tasks():\n    return []\n",
    "web/client.js": "export function signIn(u, p) { return fetch('/login', {method: 'POST'}) }\n"
                     "function render() { signIn('a','b') }\n",
}


def tags(queries: Path, fname: str, code: str):
    lang = filename_to_lang(fname)
    tree = get_parser(lang).parse(code.encode())
    query = Query(get_language(lang), (queries / f"{lang}-tags.scm").read_text())
    caps = QueryCursor(query).captures(tree.root_node) if QueryCursor else query.captures(tree.root_node)
    for tag, nodes in caps.items():
        kind = "def" if tag.startswith("name.definition.") else "ref" if tag.startswith("name.reference.") else None
        for node in nodes if kind else []:
            yield kind, node.text.decode()


def pagerank(edges, nodes, personalization, damping=0.85, iters=50):
    out = {n: [b for a, b in edges if a == n] for n in nodes}
    total = sum(personalization.values())
    p = {n: personalization[n] / total for n in nodes}
    rank = dict(p)
    for _ in range(iters):
        nxt = {n: (1 - damping) * p[n] for n in nodes}
        for n in nodes:
            targets = out[n] or nodes
            for t in targets:
                nxt[t] += damping * rank[n] / len(targets)
        rank = nxt
    return rank


def main(aider_checkout: str) -> None:
    queries = Path(aider_checkout) / "aider/queries/tree-sitter-language-pack"
    defs: dict[str, set[str]] = {}
    refs: dict[str, set[str]] = {}
    for fname, code in FILES.items():
        for kind, name in tags(queries, fname, code):
            (defs if kind == "def" else refs).setdefault(name, set()).add(fname)
    edges = [(user, owner) for name, users in refs.items() for user in users
             for owner in defs.get(name, ()) if user != owner]
    rank = pagerank(edges, list(FILES), {f: (1.0 if "auth" in f else 0.1) for f in FILES})
    print("defs:", {k: sorted(v) for k, v in defs.items()})
    print("ranked:", sorted(((f, round(r, 3)) for f, r in rank.items()), key=lambda kv: -kv[1]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "aider")
