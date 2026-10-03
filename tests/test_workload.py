"""
Workload mode tests: deterministic engine + graph with a scripted fake LLM.
HypoPG/PostgreSQL are real (planner costs are measured, not faked).
"""

import re
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from querydoctor.agent import workload_graph as wg
from querydoctor.agent.state import IndexCandidate, Proposal
from querydoctor.workload import (
    build_candidate_pool,
    collect_workload,
    evaluate_matrix,
    greedy_select,
)
from querydoctor.workload_report import _unique_migrations


pytestmark = pytest.mark.db

SHARED = "CREATE INDEX ON lineitem (l_orderkey) INCLUDE (l_quantity)"        # q18, q03, q10
SHARED_TWIN = "CREATE INDEX ON lineitem (l_orderkey, l_quantity)"            # same benefit
Q12_ONLY = ("CREATE INDEX ON lineitem (l_shipmode, l_receiptdate) "
            "INCLUDE (l_orderkey, l_commitdate, l_shipdate)")
USELESS = "CREATE INDEX ON supplier (s_nationkey)"


@pytest.fixture(scope="module")
def workload(conn):
    queries = collect_workload(10, conn=conn)
    names = {q["name"] for q in queries}
    needed = {"q18", "q03", "q10", "q12"}
    if not needed <= names:
        pytest.skip(f"workload stats missing {needed - names}; run scripts.reset_demo")
    return [q for q in queries if q["name"] in needed]


def pool_for(sqls_by_query):
    pool, rejected = build_candidate_pool(
        {q: [{"sql": s} for s in sqls] for q, sqls in sqls_by_query.items()})
    return pool, rejected


def test_pool_dedups_and_rejects(conn):
    pool, rejected = pool_for({
        "q18": [SHARED, "DROP TABLE lineitem"],
        "q03": ["create index on lineitem (l_orderkey) include (l_quantity);"],
    })
    assert len(pool) == 1
    assert pool[0]["sources"] == ["q18", "q03"]
    assert len(rejected) == 1 and "Only CREATE INDEX" in rejected[0]["error"]


def test_matrix_marks_shared_index_helping_several_queries(conn, workload):
    pool, _ = pool_for({"q18": [SHARED], "q12": [Q12_ONLY], "q03": [USELESS]})
    evaluate_matrix(workload, pool, conn)
    by_sql = {c["sql"]: c for c in pool}
    shared = next(c for s, c in by_sql.items() if "INCLUDE (l_quantity)" in s)
    assert len(shared["queries_helped"]) >= 2
    useless = next(c for s, c in by_sql.items() if "supplier" in s)
    assert useless["queries_helped"] == [] and useless["benefit"] == 0


def test_greedy_skips_redundant_twin_and_respects_max(conn, workload):
    pool, _ = pool_for({"q18": [SHARED, SHARED_TWIN], "q12": [Q12_ONLY]})
    evaluate_matrix(workload, pool, conn)

    r = greedy_select(workload, pool, conn, max_indexes=3, min_gain_pct=2.0)
    chosen = [s["sql"] for s in r["selected"]]

    # Only one of the two l_orderkey twins is selected: once one exists the
    # other's marginal gain is ~0.
    assert sum("l_orderkey" in s and "l_shipmode" not in s for s in chosen) == 1
    assert any("l_shipmode" in s for s in chosen)
    assert len(chosen) <= 3
    assert r["workload_reduction_pct"] > 0
    assert r["final_workload_cost"] < r["baseline_workload_cost"]

    twin_reason = [n["reason"] for n in r["not_selected"]
                   if "l_orderkey" in n["sql"]]
    assert twin_reason and "redundant" in twin_reason[0]


def test_greedy_max_indexes_one_picks_biggest_marginal(conn, workload):
    pool, _ = pool_for({"q18": [SHARED], "q12": [Q12_ONLY]})
    evaluate_matrix(workload, pool, conn)
    r = greedy_select(workload, pool, conn, max_indexes=1)
    assert len(r["selected"]) == 1
    assert r["steps"][0]["runners_up"]       # the other candidate was scored
    assert any("max 1 indexes" in n["reason"] for n in r["not_selected"])


def test_unique_migration_names():
    migrations = _unique_migrations([
        "CREATE INDEX ON lineitem (l_orderkey) INCLUDE (l_quantity)",
        "CREATE INDEX ON lineitem (l_orderkey) INCLUDE (l_extendedprice)",
    ])
    names = [n for _, n in migrations]
    assert len(set(names)) == 2
    assert all("CONCURRENTLY IF NOT EXISTS" in c for c, _ in migrations)


# ---------------------------------------------------------------- graph

class FakeStructured:
    def __init__(self, by_query):
        self.by_query = by_query
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        name = re.search(r"Query (q\d\d) to optimize", messages[1][1]).group(1)
        return Proposal(candidates=[
            IndexCandidate(sql=s, rationale="t", targets=["t"])
            for s in self.by_query.get(name, [USELESS])
        ])


def run_graph(monkeypatch, by_query, top_n=4, **kwargs):
    fake = FakeStructured(by_query)
    monkeypatch.setattr(wg, "get_structured_llm", lambda schema: fake)
    graph = wg.build_workload_graph(MemorySaver())
    config = wg.workload_config(uuid.uuid4().hex)
    graph.invoke(wg.initial_workload_state(top_n=top_n, **kwargs), config)
    return graph, config, fake


def pending(graph, config):
    tasks = graph.get_state(config).tasks
    interrupts = [i for t in tasks for i in t.interrupts]
    return interrupts[0].value if interrupts else None


def test_workload_graph_approve_produces_combined_migration(conn, monkeypatch):
    graph, config, fake = run_graph(
        monkeypatch, {"q18": [SHARED], "q12": [Q12_ONLY]}, top_n=10)

    payload = pending(graph, config)
    assert payload["type"] == "workload_approval"
    assert payload["selected"] and payload["workload_reduction_pct"] > 0

    # every query got its own proposal call, with workload context
    assert len(fake.prompts) == 10
    assert "Other slow queries in the workload" in fake.prompts[0][1][1]

    graph.invoke(Command(resume={"approved": True}), config)
    report = graph.get_state(config).values["report"]
    assert report["outcome"] == "approved"
    assert report["migration_sql"].count("CREATE INDEX CONCURRENTLY IF NOT EXISTS") \
        == len(report["index_names"])
    assert report["rollback_sql"].count("DROP INDEX CONCURRENTLY IF EXISTS") \
        == len(report["index_names"])
    assert "Per-query effect" in report["markdown"]
    assert "Not measured" in report["markdown"]


def test_workload_graph_nothing_helps_is_no_fix(conn, monkeypatch):
    graph, config, _ = run_graph(monkeypatch, {}, top_n=3)   # all USELESS
    assert pending(graph, config) is None
    report = graph.get_state(config).values["report"]
    assert report["outcome"] == "no_fix"
    assert report["migration_sql"] is None


def test_workload_graph_reject_with_feedback_reproposes(conn, monkeypatch):
    graph, config, fake = run_graph(monkeypatch, {"q18": [SHARED]}, top_n=3)
    assert pending(graph, config) is not None
    first_calls = len(fake.prompts)

    fake.by_query = {"q18": [SHARED_TWIN]}
    graph.invoke(Command(resume={"approved": False,
                                 "feedback": "avoid INCLUDE columns"}), config)

    assert len(fake.prompts) == 2 * first_calls
    assert "avoid INCLUDE columns" in fake.prompts[-1][1][1]
    assert pending(graph, config) is not None
    state = graph.get_state(config).values
    assert state["iteration"] == 2
    assert "l_quantity)" in state["selection"]["selected"][0]["sql"]
