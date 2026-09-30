"""Tunable knobs, grouped by concern. Every default is overridable per engine."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BudgetConfig(BaseModel):
    """Working-context budget for one agent run.

    ``available`` is what the transcript may occupy: the model window minus the reply
    reserve and the tool-schema overhead. Thresholds are fractions of ``available``.
    """

    window_tokens: int = 32_000
    reserve_output_tokens: int = 4_096
    tool_schema_tokens: int = 1_500
    mask_ratio: float = 0.5          # start clearing old tool results
    soft_ratio: float = 0.7          # start summarising (skip if impossible)
    hard_ratio: float = 0.9          # must condense before the next call
    keep_recent_ratio: float = 0.3   # verbatim tail kept by summarisation
    max_tool_result_tokens: int = 2_000   # clip single tool outputs above this
    keep_last_tool_results: int = 3       # never mask the N most recent results
    min_maskable_tokens: int = 150        # results smaller than this are not worth masking
    summary_max_tokens: int = 1_200

    @property
    def available(self) -> int:
        return max(1_000, self.window_tokens - self.reserve_output_tokens - self.tool_schema_tokens)

    @property
    def mask_trigger(self) -> int:
        return int(self.available * self.mask_ratio)

    @property
    def soft_limit(self) -> int:
        return int(self.available * self.soft_ratio)

    @property
    def hard_limit(self) -> int:
        return int(self.available * self.hard_ratio)

    @property
    def keep_recent_tokens(self) -> int:
        return int(self.available * self.keep_recent_ratio)


class RetrievalWeights(BaseModel):
    text: float = 1.0
    entity: float = 0.8
    importance: float = 0.5
    recency: float = 0.3
    scope: float = 0.6
    vector: float = 1.0
    sprint: float = 0.1


class ContextConfig(BaseModel):
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    retrieval: RetrievalWeights = Field(default_factory=RetrievalWeights)
    brief_tokens: int = 6_000
    candidate_pool: int = 60
    curators: tuple[str, ...] = ("scrum-master", "human")
    half_life_hours: dict[str, float] = Field(default_factory=dict)  # per-kind overrides
    near_dup_threshold: float = 0.8
