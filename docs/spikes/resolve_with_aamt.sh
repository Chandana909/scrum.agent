#!/usr/bin/env bash
# Can candidate X be installed next to aamt (pinned commit) and aamt-context?
# Resolution only (no install). Usage: ./resolve_with_aamt.sh 3.11   (or 3.12)
# Needs uv. Run from this directory; writes req-*.in / lock-*.txt next to it.
set -u
PY=${1:-3.11}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
if command -v cygpath >/dev/null 2>&1; then ROOT_URL="file:///$(cygpath -m "$ROOT")"; else ROOT_URL="file://$ROOT"; fi
BASE="aamt @ git+https://github.com/Krishil-Parikh/scrum-master-agent@516a4be53a56ca8952b3ff46f9e124f1de868e6d
aamt-context @ $ROOT_URL"
declare -A C=(
  [deepagents]="deepagents" [langmem]="langmem" [mem0]="mem0ai" [graphiti]="graphiti-core"
  [openhands]=$'openhands-sdk\nopenhands-tools\nopenhands-workspace' [ag2]="ag2"
  [claude-agent-sdk]="claude-agent-sdk" [embeddings]=$'fastembed\nsqlite-vec'
  [presidio]=$'presidio-analyzer\npresidio-anonymizer' [langfuse]="langfuse" [metagpt]="metagpt"
  [repomap]=$'grep-ast\ntree-sitter-language-pack' [lg-postgres]=$'langgraph-checkpoint-postgres\npsycopg[binary]\npgvector'
  [cognee]="cognee" [aider-chat]="aider-chat"
)
printf '%s\n' "$BASE" > "$HERE/req-base.in"
uv pip compile --quiet --python-version "$PY" "$HERE/req-base.in" -o "$HERE/lock-$PY-base.txt"
echo "base (aamt + aamt-context): $(grep -c '==' "$HERE/lock-$PY-base.txt") packages"
for k in "${!C[@]}"; do
  printf '%s\n%s\n' "$BASE" "${C[$k]}" > "$HERE/req-$k.in"
  if out=$(uv pip compile --quiet --python-version "$PY" "$HERE/req-$k.in" -o "$HERE/lock-$PY-$k.txt" 2>&1); then
    added=$(comm -13 <(grep -o '^[A-Za-z0-9_.-]*==' "$HERE/lock-$PY-base.txt" | sort) \
                     <(grep -o '^[A-Za-z0-9_.-]*==' "$HERE/lock-$PY-$k.txt" | sort) | wc -l)
    echo "OK   py$PY $k (+$added packages)"
  else
    echo "FAIL py$PY $k :: $(echo "$out" | grep -v '^\s*$' | tail -2 | tr '\n' ' ' | cut -c1-240)"
  fi
done | sort
