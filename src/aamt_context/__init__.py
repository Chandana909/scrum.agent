"""aamt-context: context management and shared memory for a multi-agent Scrum team.

Start with :class:`ContextEngine`; see README.md and docs/ for the design.
"""

from .assembly import AssembledContext, ContextAssembler, Item, ListSection, TextSection, Tier
from .attempts import AttemptSummary, summarize_attempt
from .briefs import HandoffReport, TaskBrief, derive_child_brief
from .channels import ChannelHub, Channels
from .clipper import ToolOutputClipper
from .codemap import CodeMap
from .condensers import CondenserPipeline, SummarizingCondenser, ToolResultMasker, default_pipeline
from .config import BudgetConfig, ContextConfig, RetrievalWeights
from .engine import ContextEngine, ReaderContext
from .harness import AgentLoop, LoopResult, ModelTurn, Tool, ToolSpec, tool
from .lessons import LessonConsolidator
from .llm import FunctionLLM, TextLLM
from .meetings import Meeting, MeetingKind, MeetingMinutes, MeetingRoom, MinutesExtractor
from .memory import DefaultWritePolicy, Embedder, SharedMemory, WritePolicy
from .session import ContextSession
from .store import SqliteMemoryStore, VersionConflict
from .tokens import HeuristicTokenCounter, TokenCounter
from .types import (
    ChannelMessage,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Scope,
    ScoredRecord,
    Trust,
    Visibility,
    WriteAction,
    WriteResult,
)
from .worklog import Entry, EntryKind, ToolCall, WorkingLog

__version__ = "0.1.0"

__all__ = [
    "AgentLoop", "AssembledContext", "AttemptSummary", "BudgetConfig", "ChannelHub", "ChannelMessage",
    "Channels", "CodeMap", "CondenserPipeline", "ContextAssembler", "ContextConfig", "ContextEngine",
    "ContextSession", "DefaultWritePolicy", "Embedder", "Entry", "EntryKind", "FunctionLLM",
    "HandoffReport", "HeuristicTokenCounter", "Item", "LessonConsolidator", "ListSection", "LoopResult",
    "Meeting", "MeetingKind", "MeetingMinutes", "MeetingRoom", "MemoryKind", "MemoryRecord",
    "MemoryStatus", "MinutesExtractor", "ModelTurn", "ReaderContext", "RetrievalWeights", "Scope",
    "ScoredRecord", "SharedMemory", "SqliteMemoryStore", "SummarizingCondenser", "TaskBrief",
    "TextLLM", "TextSection", "Tier", "TokenCounter", "Tool", "ToolCall", "ToolOutputClipper",
    "ToolResultMasker", "ToolSpec", "Trust", "VersionConflict", "Visibility", "WorkingLog",
    "WriteAction", "WritePolicy", "WriteResult", "default_pipeline", "derive_child_brief",
    "summarize_attempt", "tool",
]
