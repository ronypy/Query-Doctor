"""
QueryDoctor MCP server (stdio). Exposes the tools to Claude Code, Claude
Desktop, Cursor, or any MCP client.

    claude mcp add querydoctor -- /abs/path/.venv/bin/python -m querydoctor.mcp_server

Safety is the same as everywhere else: read-only DB role, sqlglot allow-list,
numbers from PostgreSQL only. `recommend_indexes` / `recommend_workload` stop
at the human-approval interrupt and return a thread_id; nothing is approved
until `approve_recommendation` is called — the MCP client's user decides.
No DDL is ever executed, except optional real validation (an index built in a
rolled-back transaction on the local demo DB, ENABLE_REAL_VALIDATION=1).
"""

import uuid

import anyio
from langgraph.types import Command
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from querydoctor.agent.graph import build_graph, get_checkpointer, initial_state, run_config
from querydoctor.agent.workload_graph import (
    build_workload_graph,
    initial_workload_state,
    workload_config,
)
from querydoctor.db import get_conn
from querydoctor.tools.explain import explain_query as _explain
from querydoctor.tools.explain import summarize_plan
from querydoctor.tools.hygiene import analyze_index_hygiene, hygiene_markdown
from querydoctor.tools.query_map import lookup
from querydoctor.tools.schema import get_table_info, get_table_row_estimates
from querydoctor.tools.slow_queries import get_slow_queries
from querydoctor.tools.validate import compare_costs


mcp = MCPServer(
    name="querydoctor",
    title="QueryDoctor",
    instructions=(
        "PostgreSQL performance agent. Start with list_slow_queries, then "
        "explain_query or recommend_indexes for one query (or "
        "recommend_workload for several). Recommendations pause for approval: "
        "show the user the proposed index and estimated planner cost "
        "reduction, and call approve_recommendation only with the user's "
        "decision. Planner-cost percentages are estimates, not runtimes."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

_graphs = {}


def _single_graph():
    if "single" not in _graphs:
        _graphs["single"] = build_graph(get_checkpointer("sqlite"))
    return _graphs["single"]


def _workload_graph():
    if "workload" not in _graphs:
        _graphs["workload"] = build_workload_graph(get_checkpointer("sqlite"))
    return _graphs["workload"]


def _resolve(query: str) -> dict:
    entry = lookup(query)
    if entry is None:
        raise ValueError(f"Unknown query {query!r}; use a name like 'q14' or a "
                         "queryid from list_slow_queries")
    return entry


def _interrupt_payload(graph, config):
    tasks = graph.get_state(config).tasks
    interrupts = [i for t in tasks for i in t.interrupts]
    return interrupts[0].value if interrupts else None


def _run_result(graph, config, thread_id: str) -> dict:
    values = graph.get_state(config).values
    payload = _interrupt_payload(graph, config)
    report = values.get("report")

    result = {
        "thread_id": thread_id,
        "status": "awaiting_approval" if payload else "done",
        "trace": [f"[{t['node']}] {t['message']}" for t in values.get("trace", [])],
    }
    if payload:
        result["approval_request"] = payload
        result["next_step"] = ("Ask the user to approve or reject, then call "
                               "approve_recommendation with this thread_id.")
    if report:
        result["outcome"] = report["outcome"]
        result["report_markdown"] = report["markdown"]
        result["migration_sql"] = report["migration_sql"]
        result["rollback_sql"] = report["rollback_sql"]
    return result


# ---------------------------------------------------------------------------
# read-only tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
async def list_slow_queries(limit: int = 10) -> list[dict]:
    """Slowest workload queries from pg_stat_statements (measured runtimes),
    each mapped to its concrete TPC-H SQL name (e.g. q18)."""

    rows = await anyio.to_thread.run_sync(
        lambda: get_slow_queries(limit=limit, only_mapped=True))
    return [{
        "name": q["name"], "queryid": str(q["queryid"]), "calls": q["calls"],
        "mean_exec_time_ms": round(q["mean_exec_time"], 1),
        "total_exec_time_ms": round(q["total_exec_time"], 1),
        "normalized_sql": " ".join(q["query"].split())[:300],
    } for q in rows]


@mcp.tool(annotations=READ_ONLY)
async def explain_query(query: str) -> dict:
    """Plan summary (EXPLAIN, planner estimates) and schema for a query
    name like 'q14' or a queryid: seq scans on large tables, joins, sorts,
    existing indexes."""

    def work():
        entry = _resolve(query)
        conn = get_conn()
        try:
            summary = summarize_plan(_explain(entry["sql"], conn=conn),
                                     get_table_row_estimates(conn=conn))
            tables = get_table_info(summary["tables"], conn=conn)
        finally:
            conn.close()
        return {
            "name": entry["name"], "sql": entry["sql"], "plan_summary": summary,
            "tables": [{
                "table": t["table"], "estimated_rows": t["estimated_rows"],
                "size": t["size"],
                "indexes": [i["definition"] for i in t["indexes"]],
            } for t in tables],
        }

    return await anyio.to_thread.run_sync(work)


@mcp.tool(annotations=READ_ONLY)
async def what_if_index(query: str, index_sql: str) -> dict:
    """Estimate the effect of a hypothetical index (HypoPG, plain EXPLAIN)
    on a query. index_sql must be a single CREATE INDEX statement; it is
    checked by the safety allow-list and never actually created."""

    def work():
        entry = _resolve(query)
        r = compare_costs(entry["sql"], index_sql)
        c = r["candidates"][0]
        return {
            "query": entry["name"], "index_sql": c["sql"] or c["input_sql"],
            "valid": c["valid"], "error": c["error"],
            "baseline_planner_cost": r["baseline_cost"],
            "new_planner_cost": c["new_cost"],
            "estimated_planner_cost_reduction_pct": (
                round(c["reduction_pct"], 2) if c["reduction_pct"] is not None else None),
            "index_used_by_planner": c["index_used"],
            "estimated_size_bytes": c["est_size_bytes"],
            "note": "Planner estimate with a hypothetical index, not a runtime.",
        }

    return await anyio.to_thread.run_sync(work)


@mcp.tool(annotations=READ_ONLY)
async def index_hygiene() -> dict:
    """Unused, duplicate and prefix-redundant indexes, with suggested
    DROP / recreate SQL (report only — nothing is executed)."""

    def work():
        r = analyze_index_hygiene()
        return {
            "findings": [{k: f[k] for k in ("index", "table", "size_bytes",
                                            "idx_scan", "kinds", "reasons",
                                            "drop_sql", "recreate_sql")}
                         for f in r["findings"]],
            "reclaimable_bytes": r["reclaimable_bytes"],
            "stats_window_hours": r["stats_window_hours"],
            "report_markdown": hygiene_markdown(r),
        }

    return await anyio.to_thread.run_sync(work)


# ---------------------------------------------------------------------------
# agent tools (stop at the approval interrupt)
# ---------------------------------------------------------------------------

@mcp.tool()
async def recommend_indexes(query: str, threshold: float | None = None,
                            max_iterations: int | None = None,
                            real_validate: bool = False,
                            calls_per_day: float | None = None) -> dict:
    """Run the QueryDoctor agent on one query until it needs human approval.
    threshold is a fraction (0.30 = 30% estimated planner cost reduction).
    Returns a thread_id and the recommendation; nothing is approved here."""

    def work():
        entry = _resolve(query)
        graph = _single_graph()
        thread_id = uuid.uuid4().hex[:12]
        config = run_config(thread_id)
        graph.invoke(initial_state(entry["name"], threshold, max_iterations,
                                   real_validate, calls_per_day), config)
        return _run_result(graph, config, thread_id)

    return await anyio.to_thread.run_sync(work)


@mcp.tool()
async def recommend_workload(top_n: int = 6, max_indexes: int = 3,
                             min_gain_pct: float = 2.0,
                             real_validate: bool = False,
                             calls_per_day: float | None = None) -> dict:
    """Optimize the top-N slow queries together and propose a shared index
    set (greedy, marginal planner-cost gain). Stops for approval and returns
    a thread_id."""

    def work():
        graph = _workload_graph()
        thread_id = "wl-" + uuid.uuid4().hex[:10]
        config = workload_config(thread_id)
        graph.invoke(initial_workload_state(top_n, max_indexes, min_gain_pct,
                                            real_validate=real_validate,
                                            calls_per_day=calls_per_day), config)
        return _run_result(graph, config, thread_id)

    return await anyio.to_thread.run_sync(work)


@mcp.tool()
async def approve_recommendation(thread_id: str, approved: bool,
                                 feedback: str | None = None) -> dict:
    """Resume a paused recommendation with the USER's decision. Rejecting with
    feedback makes the agent propose again (and pause again). Returns the
    final report with migration and rollback SQL when done. Only call this
    with an explicit decision from the user."""

    def work():
        if thread_id.startswith("wl-"):
            graph, config = _workload_graph(), workload_config(thread_id)
        else:
            graph, config = _single_graph(), run_config(thread_id)

        if _interrupt_payload(graph, config) is None:
            raise ValueError(f"Thread {thread_id} is not waiting for approval")

        graph.invoke(Command(resume={"approved": approved, "feedback": feedback}),
                     config)
        return _run_result(graph, config, thread_id)

    return await anyio.to_thread.run_sync(work)


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
