import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from querydoctor.agent import nodes
from querydoctor.agent.graph import build_graph, initial_state, run_config
from querydoctor.agent.state import IndexCandidate, Proposal
from querydoctor.config import get_settings
from querydoctor.savings import estimate_monthly_savings, savings_for_state
from querydoctor.tools.query_map import get_query_sql
from querydoctor.tools.real_validate import (
    RealValidationDisabled,
    real_validate,
)


Q14_INDEX = ("CREATE INDEX ON lineitem (l_shipdate) "
             "INCLUDE (l_partkey, l_extendedprice, l_discount)")


# ---------------------------------------------------------------- savings

def test_savings_formula():
    s = estimate_monthly_savings(
        before_ms=300, after_ms=50, calls_per_day=10_000,
        index_size_bytes=1024 ** 3, vcpu_hour_usd=0.04,
        storage_gb_month_usd=0.10,
    )
    # 0.25 s × 10,000 × 30 / 3600 = 20.83 vCPU-hours
    assert s["monthly_cpu_hours_saved"] == pytest.approx(20.8333, rel=1e-3)
    assert s["monthly_compute_savings_usd"] == pytest.approx(0.8333, rel=1e-3)
    assert s["monthly_storage_cost_usd"] == pytest.approx(0.10)
    assert s["monthly_net_savings_usd"] == pytest.approx(0.7333, rel=1e-3)


def test_savings_never_negative_time():
    s = estimate_monthly_savings(50, 80, 1000, vcpu_hour_usd=0.04,
                                 storage_gb_month_usd=0.1)
    assert s["saved_ms_per_call"] == 0


def test_no_savings_without_measured_runtime():
    state = {"best": {"sql": "x", "reduction_pct": 90.0}, "calls_per_day": 1000}
    assert savings_for_state(state) is None


# ---------------------------------------------------------------- guard

def test_real_validation_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_real_validation", False)
    with pytest.raises(RealValidationDisabled):
        real_validate("SELECT 1", Q14_INDEX)


def test_real_validation_refuses_non_local_db(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_real_validation", True)
    monkeypatch.setattr(get_settings(), "admin_database_url",
                        "postgresql://u:p@prod.example.com:5432/db")
    with pytest.raises(RealValidationDisabled, match="not local"):
        real_validate("SELECT 1", Q14_INDEX)


def test_real_validation_refuses_unsafe_sql(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_real_validation", True)
    with pytest.raises(ValueError, match="Refusing"):
        real_validate("SELECT 1", "DROP TABLE lineitem")


# ---------------------------------------------------------------- real run

@pytest.mark.db
def test_real_validation_q14_measures_and_rolls_back(conn, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_real_validation", True)

    r = real_validate(get_query_sql("q14"), Q14_INDEX, runs=1)

    assert r["index_used"] is True
    assert r["after_ms"] < r["before_ms"]
    assert r["actual_size_bytes"] > 50 * 1024 * 1024
    assert r["index_name"].startswith("qd_rv_")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'qd_rv_%'")
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------- graph path

FAKE_ACTUAL = {
    "index_sql": "x", "index_name": "qd_rv_test", "runs": 3,
    "before_ms": 300.0, "after_ms": 50.0,
    "before_runs_ms": [300.0] * 3, "after_runs_ms": [50.0] * 3,
    "speedup": 6.0, "runtime_reduction_pct": 83.3, "index_used": True,
    "actual_size_bytes": 200 * 1024 * 1024, "build_seconds": 5.0,
    "method": "fake",
}


@pytest.mark.db
def test_graph_real_validate_then_report_with_savings(conn, monkeypatch):

    class FakeLLM:
        def invoke(self, messages):
            return type("R", (), {"content": "Fake explanation."})()

    class FakeStructured:
        def invoke(self, messages):
            return Proposal(candidates=[
                IndexCandidate(sql=Q14_INDEX, rationale="t", targets=["t"])
            ])

    monkeypatch.setattr(nodes, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: FakeStructured())
    monkeypatch.setattr(nodes, "run_real_validation",
                        lambda sql, index_sql, runs=3: FAKE_ACTUAL)

    graph = build_graph(MemorySaver())
    config = run_config(uuid.uuid4().hex)
    graph.invoke(initial_state("q14", real_validate=True, calls_per_day=10_000), config)
    graph.invoke(Command(resume={"approved": True}), config)

    state = graph.get_state(config).values
    assert state["best"]["actual"]["after_ms"] == 50.0
    assert any(t["node"] == "real_validate" for t in state["trace"])

    report = state["report"]
    assert report["savings"]["calls_per_day"] == 10_000
    assert report["savings"]["monthly_compute_savings_usd"] > 0
    assert "6.0× faster" in report["markdown"]
    assert "Measured locally: 300 ms -> 50 ms" in report["migration_sql"]


@pytest.mark.db
def test_graph_real_validation_failure_still_reports(conn, monkeypatch):

    class FakeLLM:
        def invoke(self, messages):
            return type("R", (), {"content": "Fake explanation."})()

    class FakeStructured:
        def invoke(self, messages):
            return Proposal(candidates=[
                IndexCandidate(sql=Q14_INDEX, rationale="t", targets=["t"])
            ])

    def boom(*args, **kwargs):
        raise RealValidationDisabled("Set ENABLE_REAL_VALIDATION=1")

    monkeypatch.setattr(nodes, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: FakeStructured())
    monkeypatch.setattr(nodes, "run_real_validation", boom)

    graph = build_graph(MemorySaver())
    config = run_config(uuid.uuid4().hex)
    graph.invoke(initial_state("q14", real_validate=True), config)
    graph.invoke(Command(resume={"approved": True}), config)

    report = graph.get_state(config).values["report"]
    assert report["outcome"] == "approved"
    assert report["savings"] is None
    assert "ENABLE_REAL_VALIDATION" in report["markdown"]
