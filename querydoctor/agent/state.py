from operator import add
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


# ---------- LLM structured output ----------

class IndexCandidate(BaseModel):
    sql: str = Field(
        description="Exactly one PostgreSQL CREATE INDEX statement "
                    "(no CONCURRENTLY, no UNIQUE, no trailing commands)"
    )
    rationale: str = Field(description="Why this index helps, 1-2 sentences")
    targets: list[str] = Field(
        description="Plan bottlenecks this index addresses, "
                    "e.g. 'Seq Scan on lineitem filtering l_shipdate'"
    )


class Proposal(BaseModel):
    # The schema says "at most 3"; the propose node enforces it in code.
    candidates: list[IndexCandidate] = Field(
        description="One to three candidate indexes, best first"
    )
    rewrite_sql: str | None = Field(
        default=None,
        description="Optional semantically equivalent rewrite of the query",
    )
    rewrite_rationale: str | None = None


# ---------- Deterministic results ----------

class CandidateResult(BaseModel):
    sql: str
    rationale: str | None = None
    baseline_cost: float | None = None
    new_cost: float | None = None
    reduction_pct: float | None = None   # percent, 0-100
    index_used: bool = False
    est_size_bytes: int | None = None
    valid: bool = True
    error: str | None = None


class Critique(BaseModel):
    verdict: Literal["accept", "retry", "give_up"]
    feedback: str


# ---------- Graph state ----------

class AgentState(TypedDict, total=False):
    run_id: str
    query_key: str                 # "q14" or a queryid, as given by the caller
    query_id: str | None
    query_name: str | None
    sql: str
    stats: dict | None             # measured pg_stat_statements numbers
    threshold: float               # fraction, e.g. 0.30
    max_iterations: int

    plan_summary: dict
    table_info: list[dict]
    diagnosis: str

    proposal: dict
    candidates: list[dict]         # proposal after safety check
    results: list[dict]            # CandidateResult dicts, this iteration
    combined: dict | None
    best: dict | None
    critique: dict
    iteration: int

    history: Annotated[list[dict], add]   # every attempt, fed back to proposer
    trace: Annotated[list[dict], add]     # UI event log {"node","message","data"}

    approved: bool | None
    human_feedback: str | None
    report: dict | None
