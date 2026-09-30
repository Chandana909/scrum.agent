# 07 — References

## Host project

* aamt — https://github.com/Krishil-Parikh/scrum-master-agent at `516a4be` (integration target)
* Architecture brief — `multi_agent_software_development_architecture (1).pdf` (in this folder)

## Source code studied (commit read)

| Project | Repository | Commit | Files that matter |
|---|---|---|---|
| pi (Earendil Works) | https://github.com/badlogic/pi-mono | `02eed88` | `packages/coding-agent/docs/compaction.md`, `docs/session-format.md`, `src/core/compaction/compaction.ts`, `src/core/compaction/utils.ts` |
| OpenHands SDK | https://github.com/OpenHands/software-agent-sdk | `eee8c88` | `openhands-sdk/openhands/sdk/context/condenser/{README.md,base.py,llm_summarizing_condenser.py,pipeline_condenser.py,prompts/}`, `context/view/{view.py,manipulation_indices.py,properties/}`, `event/condenser.py` |
| OpenHands | https://github.com/OpenHands/OpenHands | `da8f701` | (cloned for orientation; condensers now live in the SDK) |
| MetaGPT | https://github.com/FoundationAgents/MetaGPT | `11cdf46` | `metagpt/environment/base_env.py`, `metagpt/roles/role.py`, `metagpt/memory/{memory.py,role_zero_memory.py,brain_memory.py}`, `metagpt/const.py`, `metagpt/roles/engineer.py` |
| AG2 (v1) | https://github.com/ag2ai/ag2 | `a467af6` | `ag2/{assembly.py,compact.py,aggregate.py,history.py}`, `ag2/policies/{working_memory.py,episodic_memory.py,token_budget.py}`, `ag2/knowledge/{base.py,constants.py}`, `ag2/network/hub/{core.py,layout.py}` |
| Agent Zero | https://github.com/agent0ai/agent-zero | `e3051fb` | `helpers/history.py`, `plugins/_memory/helpers/memory_consolidation.py`, `plugins/_memory/prompts/memory.consolidation.sys.md` |
| mem0 | https://github.com/mem0ai/mem0 | `94c3fe9` | `mem0/memory/main.py` (`add`, `_add_to_vector_store`), `mem0/configs/prompts.py` (`ADDITIVE_EXTRACTION_PROMPT`, `DEFAULT_UPDATE_MEMORY_PROMPT`) |
| Letta | https://github.com/letta-ai/letta-code | `d4d1db7` | `src/agent/subagents/builtin/reflection-v2.md`, `src/agent/memory-*.ts`, `src/agent/shared-memory-skills.ts` (`letta-ai/letta` main is a landing page; its archived V1 server was not used) |
| Graphiti | https://github.com/getzep/graphiti | `852ca40` | `graphiti_core/edges.py` (`valid_at`, `invalid_at`, `expired_at`, `episodes`), `graphiti_core/prompts/dedupe_edges.py` |
| LangMem | https://github.com/langchain-ai/langmem | `9d033b4` | `src/langmem/short_term/summarization.py` (`RunningSummary`), `src/langmem/knowledge/extraction.py` (`MemoryManager`) |
| LangGraph / LangChain | installed packages | langgraph 1.2.12, langgraph-prebuilt 1.1.0, langgraph-checkpoint-sqlite 3.1.1, langchain 1.4.3, langchain-core 1.6.6 | `langgraph/prebuilt/chat_agent_executor.py` (`create_react_agent`, `pre_model_hook`, deprecation), `langgraph/checkpoint/sqlite/__init__.py` (`put`), `langgraph/store/base/__init__.py`, `langchain/agents/middleware/{types.py,summarization.py,context_editing.py}` |

## Documentation and articles

* Claude Code — memory (CLAUDE.md, rules, auto memory): https://code.claude.com/docs/en/memory
* Claude Code — context window and what survives compaction: https://code.claude.com/docs/en/context-window
* Claude API — context editing: https://platform.claude.com/docs/en/build-with-claude/context-editing
* Claude API — compaction: https://platform.claude.com/docs/en/build-with-claude/compaction
* Claude API — memory tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
* Anthropic — Effective context engineering for AI agents (Sep 29, 2025):
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
* Anthropic — How we built our multi-agent research system (Jun 13, 2025):
  https://www.anthropic.com/engineering/multi-agent-research-system
* Manus — Context Engineering for AI Agents: Lessons from Building Manus (Yichao "Peak" Ji, Jul 18, 2025):
  https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
* Cognition — Don't Build Multi-Agents (Walden Yan, Jun 12, 2025):
  https://cognition.com/blog/dont-build-multi-agents

## Background (cited from general knowledge, not re-read for this study)

Generative Agents (memory stream scoring), Reflexion (verbal self-reflection), CoALA (memory taxonomy),
Aider (repo map), SWE-agent (observation history processors), CrewAI (memory types), ChatDev
(experiential co-learning), AG2 classic (`GroupChat`, `TransformMessages`, nested chats), OpenAI
Agents SDK (sessions, handoffs).
