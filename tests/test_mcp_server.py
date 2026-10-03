"""
MCP server tests through a real in-process MCP client session.
The agent LLM is replaced by scripted fakes; PostgreSQL/HypoPG are real.
"""

import json

import pytest
from mcp.client import Client

from querydoctor import mcp_server
from querydoctor.agent import nodes
from querydoctor.agent.state import IndexCandidate, Proposal


pytestmark = [pytest.mark.db, pytest.mark.anyio]

Q14_INDEX = ("CREATE INDEX ON lineitem (l_shipdate) "
             "INCLUDE (l_partkey, l_extendedprice, l_discount)")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def payload(result) -> dict | list:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    return json.loads(result.content[0].text)


async def test_lists_all_tools(conn):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert tools == {"list_slow_queries", "explain_query", "what_if_index",
                     "index_hygiene", "recommend_indexes", "recommend_workload",
                     "approve_recommendation"}


async def test_slow_queries_and_explain(conn):
    async with Client(mcp_server.mcp) as client:
        rows = payload(await client.call_tool("list_slow_queries", {"limit": 5}))
        assert len(rows) >= 3 and rows[0]["name"].startswith("q")

        plan = payload(await client.call_tool("explain_query", {"query": "q06"}))
        assert "lineitem" in plan["plan_summary"]["large_seq_scans"]


async def test_what_if_index_and_safety(conn):
    async with Client(mcp_server.mcp) as client:
        good = payload(await client.call_tool(
            "what_if_index", {"query": "q14", "index_sql": Q14_INDEX}))
        assert good["index_used_by_planner"] is True
        assert good["estimated_planner_cost_reduction_pct"] > 30

        bad = payload(await client.call_tool(
            "what_if_index", {"query": "q14", "index_sql": "DROP TABLE lineitem"}))
        assert bad["valid"] is False and "Only CREATE INDEX" in bad["error"]


async def test_recommend_then_approve(conn, monkeypatch):

    class FakeLLM:
        def invoke(self, messages):
            return type("R", (), {"content": "Fake explanation."})()

    class FakeStructured:
        def invoke(self, messages):
            return Proposal(candidates=[
                IndexCandidate(sql=Q14_INDEX, rationale="t", targets=["t"])])

    monkeypatch.setattr(nodes, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: FakeStructured())

    async with Client(mcp_server.mcp) as client:
        rec = payload(await client.call_tool("recommend_indexes", {"query": "q14"}))
        assert rec["status"] == "awaiting_approval"
        assert rec["approval_request"]["best"]["reduction_pct"] > 30

        done = payload(await client.call_tool(
            "approve_recommendation",
            {"thread_id": rec["thread_id"], "approved": True}))
        assert done["status"] == "done" and done["outcome"] == "approved"
        assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in done["migration_sql"]

        again = await client.call_tool(
            "approve_recommendation",
            {"thread_id": rec["thread_id"], "approved": True})
        assert again.is_error                      # no longer waiting
