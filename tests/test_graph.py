"""
Graph tests. The LLM is replaced by scripted fakes so routing is
deterministic; PostgreSQL/HypoPG are real (costs are measured, not faked).

Live Groq smoke test: RUN_LLM_TESTS=1 pytest tests/test_graph.py -k live
"""

import os
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from querydoctor.agent import nodes
from querydoctor.agent.graph import build_graph, initial_state, run_config
from querydoctor.agent.state import IndexCandidate, Proposal
from querydoctor.report import index_name, migration_statement


pytestmark = pytest.mark.db


# Measured on TPC-H SF1 (docs/demo_queries.md):
WEAK_Q14 = "CREATE INDEX ON lineitem (l_shipdate)"   # ~25.9%, used
STRONG_Q14 = ("CREATE INDEX ON lineitem (l_shipdate) "
              "INCLUDE (l_partkey, l_extendedprice, l_discount)")   # ~93%
UNUSED_Q14 = "CREATE INDEX ON part (p_type)"


class FakeReply:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return FakeReply("Fake explanation of the bottleneck.")


class FakeStructured:
    """Returns scripted proposals in order and records prompts."""

    def __init__(self, rounds: list[list[str]]):
        self.rounds = list(rounds)
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        sqls = self.rounds.pop(0)
        return Proposal(candidates=[
            IndexCandidate(sql=s, rationale="test", targets=["test"])
            for s in sqls
        ])


@pytest.fixture
def fake_llm(monkeypatch):

    def install(rounds):
        llm = FakeLLM()
        structured = FakeStructured(rounds)
        monkeypatch.setattr(nodes, "get_llm", lambda: llm)
        monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: structured)
        return llm, structured

    return install


def start(query="q14", **kwargs):
    graph = build_graph(MemorySaver())
    config = run_config(uuid.uuid4().hex)
    graph.invoke(initial_state(query, **kwargs), config)
    return graph, config


def interrupt_payload(graph, config):
    tasks = graph.get_state(config).tasks
    interrupts = [i for t in tasks for i in t.interrupts]
    return interrupts[0].value if interrupts else None


def test_accept_interrupt_approve_report(conn, fake_llm):
    fake_llm([[STRONG_Q14]])

    graph, config = start("q14")

    payload = interrupt_payload(graph, config)
    assert payload["type"] == "approval"
    assert payload["best"]["reduction_pct"] >= 30

    state = graph.get_state(config).values
    assert state["critique"]["verdict"] == "accept"
    assert state["iteration"] == 1
    assert state["stats"]["calls"] >= 1           # measured runtime present

    graph.invoke(Command(resume={"approved": True}), config)

    report = graph.get_state(config).values["report"]
    assert report["outcome"] == "approved"
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS qd_lineitem_l_shipdate" in report["migration_sql"]
    assert "DROP INDEX CONCURRENTLY IF EXISTS qd_lineitem_l_shipdate" in report["rollback_sql"]
    assert "Estimated planner cost reduction" in report["markdown"]
    assert "Not measured" in report["markdown"]     # no fake runtime claims


def test_retry_then_accept(conn, fake_llm):
    _, structured = fake_llm([[WEAK_Q14], [STRONG_Q14]])

    graph, config = start("q14")

    state = graph.get_state(config).values
    assert interrupt_payload(graph, config) is not None
    assert state["iteration"] == 2
    assert [h["verdict"] for h in state["history"]] == ["retry", "accept"]

    weak = state["history"][0]["candidates"][0]
    assert weak["index_used"] and weak["reduction_pct"] < 30

    # The second proposal saw the measured result of the first.
    second_prompt = structured.prompts[1][1][1]
    assert "CREATE INDEX ON lineitem(l_shipdate)" in second_prompt
    assert "Fake explanation" in second_prompt   # critic feedback fed back


def test_give_up_at_max_iterations(conn, fake_llm):
    fake_llm([[WEAK_Q14], [UNUSED_Q14]])

    graph, config = start("q14", max_iterations=2)

    assert interrupt_payload(graph, config) is None
    state = graph.get_state(config).values
    assert state["critique"]["verdict"] == "give_up"
    assert "lineitem(l_shipdate)" in state["critique"]["feedback"]   # best overall
    assert state["report"]["outcome"] == "no_fix"
    assert state["report"]["migration_sql"] is None


def test_unsafe_and_repeated_candidates_are_rejected(conn, fake_llm):
    fake_llm([
        ["DROP TABLE lineitem", WEAK_Q14],
        [WEAK_Q14, "CREATE INDEX ON lineitem (l_bogus)"],
    ])

    graph, config = start("q14", max_iterations=2)

    history = graph.get_state(config).values["history"]
    first, second = history[0]["candidates"], history[1]["candidates"]

    assert not first[0]["valid"] and "Only CREATE INDEX" in first[0]["error"]
    assert first[1]["valid"]
    assert not second[0]["valid"] and "Already tested" in second[0]["error"]
    assert not second[1]["valid"] and "Unknown column" in second[1]["error"]


def test_human_rejection_with_feedback_reproposes(conn, fake_llm):
    _, structured = fake_llm([
        [STRONG_Q14],
        ["CREATE INDEX ON lineitem (l_shipdate) INCLUDE (l_partkey, l_extendedprice, l_discount, l_quantity)"],
    ])

    graph, config = start("q14")
    assert interrupt_payload(graph, config) is not None

    graph.invoke(
        Command(resume={"approved": False, "feedback": "Use a different INCLUDE list"}),
        config,
    )

    # Back at approval with a new candidate; feedback reached the proposer.
    assert interrupt_payload(graph, config) is not None
    assert "Use a different INCLUDE list" in structured.prompts[1][1][1]
    state = graph.get_state(config).values
    assert state["iteration"] == 2
    assert "l_quantity" in state["best"]["sql"]

    graph.invoke(Command(resume={"approved": True}), config)
    assert graph.get_state(config).values["report"]["outcome"] == "approved"


def test_human_rejection_without_feedback_ends(conn, fake_llm):
    fake_llm([[STRONG_Q14]])

    graph, config = start("q14")
    graph.invoke(Command(resume={"approved": False}), config)

    report = graph.get_state(config).values["report"]
    assert report["outcome"] == "rejected"
    assert report["migration_sql"] is None


def test_index_name_and_migration_statement():
    columns = {"lineitem": {"l_shipdate", "l_partkey", "l_discount"}}

    create, name = migration_statement(
        "CREATE INDEX ON lineitem (l_shipdate) INCLUDE (l_partkey, l_discount)",
        columns,
    )
    assert name == "qd_lineitem_l_shipdate"
    assert create == (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS qd_lineitem_l_shipdate "
        "ON lineitem(l_shipdate) INCLUDE (l_partkey, l_discount)"
    )

    long_name = index_name("lineitem", ["a_very_long_column_name"] * 4)
    assert len(long_name) <= 63

    with pytest.raises(ValueError):
        migration_statement("DROP TABLE lineitem", columns)


@pytest.mark.skipif(not os.getenv("RUN_LLM_TESTS"), reason="set RUN_LLM_TESTS=1")
def test_live_llm_reaches_approval_or_report(conn):
    graph, config = start("q14")
    state = graph.get_state(config).values
    assert state["history"], "at least one proposal round ran"
    assert interrupt_payload(graph, config) is not None or state.get("report")
