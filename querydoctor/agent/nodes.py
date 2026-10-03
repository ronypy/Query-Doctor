"""
LangGraph node functions.

LLM nodes: diagnose, propose, critic (feedback text on retry only).
Everything else — plans, schema, safety, HypoPG costs, accept/reject — is
deterministic code talking to PostgreSQL.
"""

import uuid

from langgraph.types import interrupt

from querydoctor.agent import prompts
from querydoctor.agent.llm import get_llm, get_structured_llm
from querydoctor.agent.state import Proposal
from querydoctor.config import get_settings
from querydoctor.db import get_conn
from querydoctor.report import build_report
from querydoctor.tools.explain import explain_query, summarize_plan
from querydoctor.tools.query_map import lookup
from querydoctor.tools.real_validate import real_validate as run_real_validation
from querydoctor.tools.safety import check_index_sql
from querydoctor.tools.schema import (
    get_column_map,
    get_table_info,
    get_table_row_estimates,
)
from querydoctor.tools.slow_queries import get_query_stats
from querydoctor.tools.validate import compare_costs


MAX_CANDIDATES = 3


def _trace(node: str, message: str, data: dict | None = None) -> list[dict]:
    entry = {"node": node, "message": message}
    if data is not None:
        entry["data"] = data
    return [entry]


def _describe_seq_scans(summary: dict) -> str:

    scans = [s for s in summary["seq_scans"] if s.get("large_table")]
    if not scans:
        return "No sequential scans on large tables."

    parts = []
    for s in scans:
        rows = s.get("table_rows")
        size = f" (~{rows / 1e6:.1f}M rows)" if rows and rows >= 1e6 else (
            f" (~{rows:,} rows)" if rows else "")
        filt = f" filtering {s['filter']}" if s.get("filter") else ""
        parts.append(f"Seq Scan on {s['table']}{size}{filt}")

    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# fetch_context  [tool]
# ---------------------------------------------------------------------------

def fetch_context(state: dict) -> dict:

    settings = get_settings()

    sql = state.get("sql")
    query_id = state.get("query_id")
    query_name = state.get("query_name")

    if not sql:
        entry = lookup(state["query_key"])
        if entry is None:
            raise ValueError(f"Unknown query {state['query_key']!r}")
        sql = entry["sql"]
        query_id = str(entry["queryid"])
        query_name = entry["name"]

    conn = get_conn()
    try:
        table_rows = get_table_row_estimates(conn=conn)
        summary = summarize_plan(explain_query(sql, conn=conn), table_rows)
        table_info = get_table_info(summary["tables"], conn=conn)
    finally:
        conn.close()

    stats = get_query_stats(query_id)

    runtime = (
        f" Measured mean runtime {stats['mean_exec_time']:,.0f} ms "
        f"over {stats['calls']} calls."
        if stats else ""
    )

    return {
        "run_id": state.get("run_id") or uuid.uuid4().hex[:12],
        "sql": sql,
        "query_id": query_id,
        "query_name": query_name,
        "stats": stats,
        "threshold": state.get("threshold", settings.improvement_threshold),
        "max_iterations": state.get("max_iterations", settings.max_iterations),
        "plan_summary": summary,
        "table_info": table_info,
        "iteration": 1,
        "trace": _trace(
            "fetch_context",
            f"Baseline planner cost {summary['total_cost']:,.0f}. "
            f"{_describe_seq_scans(summary)}{runtime}",
            {"baseline_cost": summary["total_cost"],
             "large_seq_scans": summary["large_seq_scans"],
             "stats": stats},
        ),
    }


# ---------------------------------------------------------------------------
# diagnose  [LLM]
# ---------------------------------------------------------------------------

def diagnose(state: dict) -> dict:

    try:
        reply = get_llm().invoke(prompts.diagnose_messages(state))
        diagnosis = reply.content.strip()
    except Exception as e:
        diagnosis = (
            f"(LLM unavailable: {type(e).__name__}) "
            + _describe_seq_scans(state["plan_summary"])
        )

    return {
        "diagnosis": diagnosis,
        "trace": _trace("diagnose", diagnosis),
    }


# ---------------------------------------------------------------------------
# propose  [LLM, structured output]
# ---------------------------------------------------------------------------

def propose(state: dict) -> dict:

    proposal: Proposal = get_structured_llm(Proposal).invoke(
        prompts.propose_messages(state)
    )

    data = proposal.model_dump()
    data["candidates"] = data["candidates"][:MAX_CANDIDATES]

    # Remember which human feedback this proposal answered (for history).
    data["human_feedback"] = state.get("human_feedback")

    listing = "; ".join(c["sql"].strip().rstrip(";") for c in data["candidates"])

    return {
        "proposal": data,
        "human_feedback": None,
        "trace": _trace(
            "propose",
            f"Iteration {state['iteration']}: proposed "
            f"{len(data['candidates'])} index(es): {listing}",
            {"candidates": data["candidates"],
             "rewrite_sql": data.get("rewrite_sql")},
        ),
    }


# ---------------------------------------------------------------------------
# safety_check  [tool]
# ---------------------------------------------------------------------------

def safety_check(state: dict) -> dict:

    column_map = get_column_map()

    tested = {
        c["sql"]: attempt["iteration"]
        for attempt in state.get("history", [])
        for c in attempt["candidates"]
        if c.get("valid", True)
    }

    candidates = []
    seen = set()

    for c in state["proposal"]["candidates"]:

        check = check_index_sql(c["sql"], column_map)

        entry = {
            "input_sql": c["sql"],
            "sql": check["sql"] if check["ok"] else c["sql"].strip().rstrip(";").strip(),
            "table": check["table"],
            "rationale": c.get("rationale"),
            "targets": c.get("targets", []),
            "valid": check["ok"],
            "error": None if check["ok"] else check["reason"],
        }

        if entry["valid"] and entry["sql"] in tested:
            entry["valid"] = False
            entry["error"] = f"Already tested in iteration {tested[entry['sql']]}"
        elif entry["valid"] and entry["sql"] in seen:
            entry["valid"] = False
            entry["error"] = "Duplicate of another candidate"

        seen.add(entry["sql"])
        candidates.append(entry)

    rejected = [c for c in candidates if not c["valid"]]

    if rejected:
        message = (
            f"{len(candidates) - len(rejected)} of {len(candidates)} candidates "
            "passed the safety check. Rejected: "
            + "; ".join(f"{c['sql']} ({c['error']})" for c in rejected)
        )
    else:
        message = f"All {len(candidates)} candidates passed the safety check."

    return {
        "candidates": candidates,
        "trace": _trace("safety_check", message,
                        {"rejected": [{"sql": c["sql"], "error": c["error"]}
                                      for c in rejected]}),
    }


# ---------------------------------------------------------------------------
# validate  [tool: HypoPG]
# ---------------------------------------------------------------------------

def validate(state: dict) -> dict:

    candidates = state["candidates"]
    valid_sqls = [c["sql"] for c in candidates if c["valid"]]

    by_sql = {}
    combined = None
    baseline_cost = state["plan_summary"]["total_cost"]

    if valid_sqls:
        conn = get_conn()
        try:
            r = compare_costs(
                state["sql"],
                valid_sqls,
                conn=conn,
                table_rows=get_table_row_estimates(conn=conn),
                column_map=get_column_map(conn=conn),
            )
        finally:
            conn.close()

        baseline_cost = r["baseline_cost"]
        by_sql = {c["sql"]: c for c in r["candidates"]}

        if r["combined"]:
            combined = {
                "sqls": r["combined"]["sqls"],
                "new_cost": r["combined"]["new_cost"],
                "reduction_pct": r["combined"]["reduction_pct"],
                "indexes_used": r["combined"]["indexes_used"],
            }

    results = []
    best = None

    for c in candidates:

        measured = by_sql.get(c["sql"]) if c["valid"] else None

        result = {
            "sql": c["sql"],
            "table": c["table"],
            "rationale": c["rationale"],
            "baseline_cost": baseline_cost,
            "new_cost": None,
            "reduction_pct": None,
            "index_used": False,
            "est_size_bytes": None,
            "valid": c["valid"],
            "error": c["error"],
        }

        if measured is not None:
            result.update({
                "new_cost": measured["new_cost"],
                "reduction_pct": measured["reduction_pct"],
                "index_used": measured["index_used"],
                "est_size_bytes": measured["est_size_bytes"],
                "valid": measured["valid"],
                "error": measured["error"],
            })

            if result["valid"] and result["index_used"] and (
                best is None or result["reduction_pct"] > best["reduction_pct"]
            ):
                best = {**result, "new_summary": measured["new_summary"]}

        results.append(result)

    lines = []
    for r in results:
        if not r["valid"]:
            lines.append(f"{r['sql']}: rejected ({r['error']})")
        else:
            lines.append(
                f"{r['sql']}: {r['reduction_pct']:.1f}% est. planner cost, "
                f"{'used ✓' if r['index_used'] else 'not used ✗'}"
            )

    return {
        "results": results,
        "combined": combined,
        "best": best,
        "trace": _trace("validate", " | ".join(lines),
                        {"baseline_cost": baseline_cost, "results": results,
                         "combined": combined}),
    }


# ---------------------------------------------------------------------------
# critic  [rules + LLM feedback]
# ---------------------------------------------------------------------------

def critic(state: dict) -> dict:

    threshold_pct = state["threshold"] * 100
    iteration = state["iteration"]
    best = state.get("best")
    results = state.get("results", [])

    if best and best["index_used"] and best["reduction_pct"] >= threshold_pct:
        verdict = "accept"
        feedback = (
            f"Accepted: {best['sql']} reduces estimated planner cost by "
            f"{best['reduction_pct']:.1f}% (threshold {threshold_pct:.0f}%) "
            "and the planner uses it."
        )

    elif iteration >= state["max_iterations"]:
        verdict = "give_up"

        # Best used candidate across ALL iterations, for an honest message.
        tried = [
            c for attempt in state.get("history", [])
            for c in attempt["candidates"]
        ] + results
        used = [c for c in tried if c.get("valid") and c.get("index_used")]
        overall = max(used, key=lambda c: c["reduction_pct"], default=None)

        best_text = (
            f"the best index the planner used ({overall['sql']}) reached "
            f"{overall['reduction_pct']:.1f}%"
            if overall else "no candidate was used by the planner"
        )
        feedback = (
            f"Giving up after {iteration} iteration(s): {best_text}, "
            f"below the {threshold_pct:.0f}% threshold."
        )

    else:
        verdict = "retry"
        try:
            reply = get_llm().invoke(prompts.critic_messages(state, results))
            feedback = reply.content.strip()
        except Exception as e:
            feedback = (
                f"(LLM unavailable: {type(e).__name__}) No candidate was used "
                f"with at least {threshold_pct:.0f}% planner cost reduction. "
                "Try a different leading column or a covering index."
            )

    attempt = {
        "iteration": iteration,
        "candidates": [
            {k: r[k] for k in ("sql", "valid", "error", "new_cost",
                               "reduction_pct", "index_used", "est_size_bytes")}
            for r in results
        ],
        "combined": state.get("combined"),
        "verdict": verdict,
        "feedback": feedback if verdict == "retry" else None,
        "human_feedback": state.get("proposal", {}).get("human_feedback"),
    }

    update = {
        "critique": {"verdict": verdict, "feedback": feedback},
        "history": [attempt],
        "trace": _trace("critic", f"{verdict.upper()}: {feedback}",
                        {"verdict": verdict}),
    }

    if verdict == "retry":
        update["iteration"] = iteration + 1

    return update


# ---------------------------------------------------------------------------
# human_approval  [interrupt]
# ---------------------------------------------------------------------------

def human_approval(state: dict) -> dict:

    best = state["best"]

    decision = interrupt({
        "type": "approval",
        "message": "Approve this index recommendation?",
        "query": state.get("query_name"),
        "best": {k: best.get(k) for k in ("sql", "table", "rationale",
                                          "baseline_cost", "new_cost",
                                          "reduction_pct", "est_size_bytes")},
        "threshold": state["threshold"],
    })

    approved = bool(decision.get("approved"))
    feedback = (decision.get("feedback") or "").strip() or None

    update = {
        "approved": approved,
        "human_feedback": feedback,
        "trace": _trace(
            "human_approval",
            "Approved by human." if approved
            else f"Rejected by human{': ' + feedback if feedback else '.'}",
        ),
    }

    if not approved and feedback:
        update["iteration"] = state["iteration"] + 1

    return update


# ---------------------------------------------------------------------------
# real_validate  [tool: real index in a rolled-back transaction]
# ---------------------------------------------------------------------------

def real_validate(state: dict) -> dict:

    best = state["best"]

    try:
        actual = run_real_validation(
            state["sql"], best["sql"], runs=get_settings().real_validation_runs
        )
    except Exception as e:
        return {
            "best": {**best, "actual": None,
                     "actual_error": f"{type(e).__name__}: {str(e).splitlines()[0]}"},
            "trace": _trace("real_validate",
                            f"Real validation skipped/failed: {str(e).splitlines()[0]}"),
        }

    used = "used" if actual["index_used"] else "NOT used"
    message = (
        f"Actual (EXPLAIN ANALYZE, median of {actual['runs']}): "
        f"{actual['before_ms']:,.0f} ms → {actual['after_ms']:,.0f} ms "
        f"({actual['speedup']:.1f}× faster), real index {used}, actual size "
        f"{actual['actual_size_bytes'] / 1024 / 1024:,.0f} MB, built in "
        f"{actual['build_seconds']:.1f} s and rolled back."
    )

    return {
        "best": {**best, "actual": actual},
        "trace": _trace("real_validate", message, {"actual": actual}),
    }


# ---------------------------------------------------------------------------
# report  [deterministic]
# ---------------------------------------------------------------------------

def report(state: dict) -> dict:

    result = build_report(state)

    message = {
        "approved": f"Report ready. Migration creates {result['index_name']}.",
        "rejected": "Report ready. Recommendation was rejected; no migration generated.",
        "no_fix": "Report ready. No index met the acceptance rule; no migration generated.",
    }[result["outcome"]]

    return {"report": result, "trace": _trace("report", message)}


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def route_after_critic(state: dict) -> str:
    return {
        "accept": "human_approval",
        "retry": "propose",
        "give_up": "report",
    }[state["critique"]["verdict"]]


def route_after_approval(state: dict) -> str:

    if state.get("approved"):
        return "real_validate" if state.get("real_validate") else "report"

    # Rejected with feedback -> try again, within a hard cap.
    hard_cap = state["max_iterations"] * 2
    if state.get("human_feedback") and state["iteration"] <= hard_cap:
        return "propose"

    return "report"
