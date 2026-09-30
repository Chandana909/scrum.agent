# 09 — Where to get each piece: shared memory and context management

This page covers only your two parts. For each piece it gives:

* what to adopt, and where to download it;
* which class to use;
* where it plugs into this package.

All links were checked on 2026-09-30. The versions are the ones tested next to aamt at commit
`516a4be`. Test scripts are in [`spikes/`](spikes/). The reasoning behind each choice is in
[08](08-adopt-adapt-keep.md) §3.2 to §3.6.

## At a glance

| Your part | Piece | Get it from | Already installed with aamt? | Licence | Tested version |
|---|---|---|---|---|---|
| Context management | Summarise old turns, save the full history to a file, "compact now" tool | **DeepAgents** | no | MIT | 0.7.20 |
| Context management | Clear old tool outputs | **LangChain** | yes | MIT | 1.4.3 |
| Context management | Token counting | **langchain-core** | yes | MIT | 1.6.6 |
| Shared memory | Search by meaning inside the same SQLite file | **sqlite-vec** | yes | MIT or Apache-2.0 | 0.1.9 |
| Shared memory | Local embeddings (no text leaves the machine) | **fastembed** + the **bge-small-en-v1.5** model | no | Apache-2.0 (model: MIT) | 0.8.1 |

Only two things need downloading. Run this in aamt's environment:

```bash
uv add "deepagents==0.7.20" "fastembed==0.8.1"
```

In this package, keep them as optional extras so the core stays dependency-free:

```toml
[project.optional-dependencies]
context = ["deepagents==0.7.20"]
embeddings = ["fastembed==0.8.1"]
```

---

## 1. Context management

### 1.1 DeepAgents: summarisation that keeps the full history

| | |
|---|---|
| Download (PyPI) | [pypi.org/project/deepagents](https://pypi.org/project/deepagents/) |
| Source (GitHub) | [github.com/langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) |
| The file you're using | [libs/deepagents/deepagents/middleware/summarization.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py) |
| Docs | [docs.langchain.com/oss/python/deepagents/overview](https://docs.langchain.com/oss/python/deepagents/overview) |
| Install | `uv add "deepagents==0.7.20"` |
| Classes | `SummarizationMiddleware`, `SummarizationToolMiddleware` (from `deepagents.middleware.summarization`); `FilesystemBackend` (from `deepagents.backends`) |
| Replaces here | `condensers.py` (`SummarizingCondenser`), `session.py`, the view logic in `worklog.py` |

What it does:

* When the transcript passes a token threshold, it summarises the older turns.
* It saves the full text of those turns to `/conversation_history/<session>.md` on the backend you
  give it.
* It sends the model the summary plus the recent turns, and keeps the full message list in state.
* The tool middleware adds a `compact_conversation` tool so the agent can compact on demand.

```python
from deepagents.backends import FilesystemBackend
from deepagents.middleware.summarization import SummarizationMiddleware, SummarizationToolMiddleware

offload = FilesystemBackend(root_dir=".aamt/runs", virtual_mode=True)   # outside the git workspace
summ = SummarizationMiddleware(summary_model, backend=offload,
                               trigger=("tokens", 24_000), keep=("tokens", 8_000))
middleware = [summ, SummarizationToolMiddleware(summ)]
```

One condition: this runs with LangChain's `create_agent`, not the deprecated `create_react_agent`
that aamt's `base.py` uses today. The switch is a few lines, shown in
[08 §5 step 1](08-adopt-adapt-keep.md#5-integration-plan).

### 1.2 LangChain: clear old tool outputs

| | |
|---|---|
| Download (PyPI) | [pypi.org/project/langchain](https://pypi.org/project/langchain/) (already installed with aamt, nothing to do) |
| Source (GitHub) | [github.com/langchain-ai/langchain](https://github.com/langchain-ai/langchain) |
| The file you're using | [libs/langchain_v1/langchain/agents/middleware/context_editing.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/context_editing.py) |
| Docs | [docs.langchain.com/oss/python/langchain/middleware/built-in](https://docs.langchain.com/oss/python/langchain/middleware/built-in) |
| Classes | `ContextEditingMiddleware`, `ClearToolUsesEdit` (from `langchain.agents.middleware`) |
| Replaces here | `ToolResultMasker` in `condensers.py` |

Once the transcript passes `trigger` tokens, older tool results are replaced with `[cleared]`. The
last `keep` results stay as they are.

```python
from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware

middleware.append(ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=16_000, keep=3)]))
```

### 1.3 langchain-core: token counting

| | |
|---|---|
| Download (PyPI) | [pypi.org/project/langchain-core](https://pypi.org/project/langchain-core/) (already installed with aamt) |
| The file you're using | [libs/core/langchain_core/messages/utils.py](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/messages/utils.py) |
| Function | `count_tokens_approximately(messages, use_usage_metadata_scaling=True)` |
| Replaces here | the chars/4 default in `tokens.py`. Keep the `TokenCounter` interface itself. |

With `use_usage_metadata_scaling=True`, it corrects its estimate using the token counts the model
provider reports back.

### 1.4 What stays yours (nothing to download)

* **The task brief:** which decisions, contracts, handoffs and lessons go into each agent's prompt.
  That's `engine.py`, `assembly.py` and `briefs.py`. No library does this.
* **Attempt memory:** what the previous try did and why it failed. That's `attempts.py`.
* **Big tool outputs:** keep `clipper.py` for now. DeepAgents' `FilesystemMiddleware`
  ([source](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py))
  is the later replacement. It clashes with aamt's own `read_file` tool until the team renames or
  replaces that tool; see [08 §3.3](08-adopt-adapt-keep.md#33-oversized-tool-output-test-logs-big-files).

## 2. Shared memory

### 2.1 sqlite-vec: search by meaning in the same SQLite file

| | |
|---|---|
| Download (PyPI) | [pypi.org/project/sqlite-vec](https://pypi.org/project/sqlite-vec/) (already installed with aamt as a dependency of [langgraph-checkpoint-sqlite](https://pypi.org/project/langgraph-checkpoint-sqlite/)) |
| Source (GitHub) | [github.com/asg017/sqlite-vec](https://github.com/asg017/sqlite-vec) |
| Docs (Python) | [alexgarcia.xyz/sqlite-vec/python.html](https://alexgarcia.xyz/sqlite-vec/python.html) |
| Install (only to declare it explicitly) | `uv add "sqlite-vec==0.1.9"` |
| Plugs in here | `store.py`: a `records_vec` table and a `search_vector()` method. `memory.py`: `recall()` gains a third source of candidates, next to full-text and entity matches. |

Why you need it: today `recall()` only scores embeddings for records that the keyword search already
found. So "how do users sign in?" finds nothing, even though a decision says "Authentication uses JWT
bearer tokens". With sqlite-vec that decision is the top result (tested in
[`spikes/spike_vectors.py`](spikes/spike_vectors.py)).

```python
import sqlite_vec

conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_vec "
             "USING vec0(embedding float[384] distance_metric=cosine)")
rows = conn.execute("SELECT rowid, distance FROM records_vec WHERE embedding MATCH ? AND k = ?",
                    (sqlite_vec.serialize_float32(query_vector), 20)).fetchall()
```

Use `k = ?`, not `LIMIT`. The SQLite that ships with Python 3.11 is too old for `LIMIT` on this
kind of table.

### 2.2 fastembed: local embeddings

| | |
|---|---|
| Download (PyPI) | [pypi.org/project/fastembed](https://pypi.org/project/fastembed/) |
| Source (GitHub) | [github.com/qdrant/fastembed](https://github.com/qdrant/fastembed) |
| Docs | [qdrant.github.io/fastembed](https://qdrant.github.io/fastembed/) |
| Install | `uv add "fastembed==0.8.1"` |
| Model (downloaded automatically on first use, about 130 MB) | [huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q), an ONNX export of [huggingface.co/BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) (MIT) |
| Plugs in here | a new `integrations/fastembed.py` implementing the existing `Embedder` interface. Pass it as `embedder=` to `SharedMemory` / `ContextEngine`. |

It runs on the CPU with no PyTorch, about 6 ms per record in the test.

```python
class FastEmbedEmbedder:
    def __init__(self, model="BAAI/bge-small-en-v1.5", cache_dir=None):
        from fastembed import TextEmbedding
        self._m = TextEmbedding(model_name=model, cache_dir=cache_dir)

    def embed(self, texts):
        return [v.tolist() for v in self._m.embed(texts)]
```

To run without internet access, download the model once and point `cache_dir` at that copy.

### 2.3 What stays yours (nothing to download)

The governed part has no open-source equivalent:

* who may make a decision official (proposals, accept/reject);
* the history of which decision replaced which;
* scopes (org, project, team, agent, task);
* safe concurrent updates.

That's `types.py`, `store.py`, `memory.py`, `channels.py`, `meetings.py` and `lessons.py`.

## 3. Looked at but not adopted

The links are here so you can check each one yourself.

| Project | Links | Why not, for your two parts |
|---|---|---|
| mem0 | [github.com/mem0ai/mem0](https://github.com/mem0ai/mem0) · [pypi.org/project/mem0ai](https://pypi.org/project/mem0ai/) · [docs.mem0.ai](https://docs.mem0.ai) | The model decides what gets saved. There's no approval step and no "B replaced A" history. It sends usage data unless `MEM0_TELEMETRY=False`. It's the closest drop-in if you ever want private per-agent notes. |
| Graphiti | [github.com/getzep/graphiti](https://github.com/getzep/graphiti) · [pypi.org/project/graphiti-core](https://pypi.org/project/graphiti-core/) · [docs](https://help.getzep.com/graphiti/getting-started/welcome) | Tracks how facts change over time, but needs a Neo4j or FalkorDB server and several model calls per write. It also sends usage data by default. |
| LangGraph Store | [docs.langchain.com/oss/python/langgraph/persistence](https://docs.langchain.com/oss/python/langgraph/persistence) (already installed) | A shared key-value store with search, but the last write wins: no approval, no history, no full-text search. |
| LangMem | [github.com/langchain-ai/langmem](https://github.com/langchain-ai/langmem) | One release in the last year (0.0.30). Its summariser is superseded by 1.1 above. |
| Letta | [github.com/letta-ai/letta-code](https://github.com/letta-ai/letta-code) | The Python server is archived. Current Letta is TypeScript. |
| MetaGPT | [github.com/FoundationAgents/MetaGPT](https://github.com/FoundationAgents/MetaGPT) | Can't be installed alongside aamt (dependency conflict; needs Python < 3.12). |
| AG2 | [github.com/ag2ai/ag2](https://github.com/ag2ai/ag2) | Its memory and compaction only work inside AG2's own agents. |
| Agent Zero | [github.com/agent0ai/agent-zero](https://github.com/agent0ai/agent-zero) | An application, not an installable library. |
| pi (Earendil) | [github.com/badlogic/pi-mono](https://github.com/badlogic/pi-mono) · [compaction doc](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/compaction.md) | TypeScript. Its design is what DeepAgents' summarisation resembles. |
| OpenHands SDK | [github.com/OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) | Needs Python 3.12+. Its condensers only work on its own event format. |
| Claude Code | [code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory) · [code.claude.com/docs/en/context-window](https://code.claude.com/docs/en/context-window) | Closed source, so ideas only. Don't use the leaked source. |

## 4. Check it on your machine

```bash
uv add "deepagents==0.7.20" "fastembed==0.8.1"
```

```bash
LANGSMITH_TRACING=false python docs/spikes/spike_middleware.py
```

```bash
python docs/spikes/spike_vectors.py
```

The first script shows summarisation, saved history and cleared tool outputs working on a real agent.
The second shows the "how do users sign in?" search that fails today and succeeds with sqlite-vec +
fastembed.
