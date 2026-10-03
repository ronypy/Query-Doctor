"""
Workload-mode LangGraph: optimize the top-N slow queries together.

START → collect → propose_all → evaluate → select
select:          set found → human_approval | nothing helps → report
human_approval:  approved → real_validate? → report
                 rejected + feedback → propose_all | rejected → report
report → END

The LLM only proposes candidates (one structured Proposal per query).
Safety, HypoPG measurement and the greedy set selection are deterministic.

CLI:
    python -m querydoctor.agent.workload_graph --top 6 [--max-indexes 3]
"""

import argparse
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from querydoctor.agent import prompts
from querydoctor.agent.graph import RECURSION_LIMIT, get_checkpointer
from querydoctor.agent.llm import get_structured_llm
from querydoctor.agent.state import Proposal
from querydoctor.config import PROJECT_ROOT, get_settings
from querydoctor.db import get_conn
from querydoctor.tools.real_validate import real_validate_set
from querydoctor.tools.schema import get_column_map
from querydoctor.workload import (
    build_candidate_pool,
    collect_workload,
    evaluate_matrix,
    greedy_select,
)
from querydoctor.workload_report import build_workload_report


MAX_CANDIDATES_PER_QUERY = 3
LLM_WORKERS = 2
LLM_ATTEMPTS = 4          # per query, exponential backoff (Groq 503s)


class WorkloadState(TypedDict, total=False):
    run_id: str
    top_n: int
    max_indexes: int
    min_gain_pct: float
    size_penalty_pct_per_gb: float
    max_iterations: int
    iteration: int
    real_validate: bool
    real_validation_runs: int
    calls_per_day: float | None

    queries: list[dict]
    proposals: dict            # query name -> [{"sql", "rationale"}]
    proposal_errors: dict      # query name -> error text
    pool: list[dict]           # safety-checked, deduplicated candidates (+ matrix)
    rejected: list[dict]
    selection: dict | None
    rounds: Annotated[list[dict], add]

    approved: bool | None
    human_feedback: str | None
    actual: dict | None
    actual_error: str | None
    report: dict | None
    trace: Annotated[list[dict], add]


def _trace(node: str, message: str, data: dict | None = None) -> list[dict]:
    entry = {"node": node, "message": message}
    if data is not None:
        entry["data"] = data
    return [entry]


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def collect(state: dict) -> dict:

    settings = get_settings()
    queries = collect_workload(state.get("top_n", 6))

    total_ms = sum(q["total_exec_time"] for q in queries)
    return {
        "run_id": state.get("run_id") or uuid.uuid4().hex[:12],
        "queries": queries,
        "iteration": 1,
        "max_iterations": state.get("max_iterations", settings.max_iterations),
        "trace": _trace(
            "collect",
            f"Workload: {len(queries)} queries "
            f"({', '.join(q['name'].upper() for q in queries)}), "
            f"{total_ms / 1000:,.1f} s measured total execution time.",
        ),
    }


def propose_all(state: dict) -> dict:

    queries = state["queries"]
    feedback = state.get("human_feedback")
    previous = [s["sql"] for s in (state.get("selection") or {}).get("selected", [])]
    llm = get_structured_llm(Proposal)

    def ask(q):
        messages = prompts.workload_propose_messages(
            q, queries, feedback, previous if feedback else None)
        error = None
        for attempt in range(LLM_ATTEMPTS):
            try:
                p = llm.invoke(messages)
                return q["name"], [
                    {"sql": c.sql, "rationale": c.rationale}
                    for c in p.candidates[:MAX_CANDIDATES_PER_QUERY]
                ], None
            except Exception as e:
                error = f"{type(e).__name__}: {str(e).splitlines()[0]}"
                if attempt < LLM_ATTEMPTS - 1:
                    time.sleep(2 * 2 ** attempt)      # 2, 4, 8 s
        return q["name"], [], error

    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        results = list(pool.map(ask, queries))

    proposals = {name: cands for name, cands, _ in results}
    errors = {name: err for name, _, err in results if err}
    n = sum(len(c) for c in proposals.values())

    message = (f"Round {state['iteration']}: {n} candidate indexes proposed "
               f"for {len(queries)} queries.")
    if errors:
        message += f" LLM failed for: {', '.join(errors)}."

    return {
        "proposals": proposals,
        "proposal_errors": errors,
        "human_feedback": None,
        "trace": _trace("propose_all", message, {"proposals": proposals}),
    }


def evaluate(state: dict) -> dict:

    conn = get_conn()
    try:
        pool, rejected = build_candidate_pool(
            state["proposals"], get_column_map(conn=conn))
        evaluate_matrix(state["queries"], pool, conn)
    finally:
        conn.close()

    helpful = [c for c in pool if c["queries_helped"]]
    shared = [c for c in helpful if len(c["queries_helped"]) > 1]

    message = (f"{len(pool)} unique valid candidates ({len(rejected)} rejected by "
               f"the safety check). {len(helpful)} are used by the planner for at "
               f"least one query; {len(shared)} help several queries.")

    return {
        "pool": pool,
        "rejected": rejected,
        "trace": _trace("evaluate", message),
    }


def select(state: dict) -> dict:

    conn = get_conn()
    try:
        selection = greedy_select(
            state["queries"],
            state["pool"],
            conn,
            max_indexes=state.get("max_indexes", 3),
            min_gain_pct=state.get("min_gain_pct", 2.0),
            size_penalty_pct_per_gb=state.get("size_penalty_pct_per_gb", 0.0),
        )
    finally:
        conn.close()

    for r in state.get("rejected", []):
        selection["not_selected"].append(
            {"sql": r["sql"], "reason": f"rejected by safety check: {r['error']}",
             "benefit": 0.0})

    if selection["selected"]:
        steps = " → ".join(
            f"{s['chosen']} (+{s['marginal_gain_pct']:.1f}%)" for s in selection["steps"])
        message = (f"Greedy selection: {steps}. Estimated workload planner cost "
                   f"−{selection['workload_reduction_pct']:.1f}%.")
    else:
        message = "No candidate reduced the workload planner cost enough."

    return {
        "selection": selection,
        "rounds": [{"iteration": state["iteration"],
                    "selected": [s["sql"] for s in selection["selected"]],
                    "workload_reduction_pct": selection["workload_reduction_pct"]}],
        "trace": _trace("select", message),
    }


def human_approval(state: dict) -> dict:

    sel = state["selection"]

    decision = interrupt({
        "type": "workload_approval",
        "message": "Approve this index set for the workload?",
        "selected": sel["selected"],
        "per_query": sel["per_query"],
        "workload_reduction_pct": sel["workload_reduction_pct"],
        "total_size_bytes": sel["total_size_bytes"],
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


def real_validate(state: dict) -> dict:

    selected = [s["sql"] for s in state["selection"]["selected"]]
    queries = [(q["name"], q["sql"]) for q in state["queries"]]
    runs = state.get("real_validation_runs", 3)

    try:
        actual = real_validate_set(queries, selected, runs=runs)
    except Exception as e:
        error = f"{type(e).__name__}: {str(e).splitlines()[0]}"
        return {"actual": None, "actual_error": error,
                "trace": _trace("real_validate", f"Real validation skipped/failed: {error}")}

    helped = [r for r in actual["queries"].values() if r["indexes_used"]]
    before = sum(r["before_ms"] for r in helped)
    after = sum(r["after_ms"] for r in helped)
    size = sum(i["actual_size_bytes"] for i in actual["indexes"])

    return {
        "actual": actual,
        "trace": _trace(
            "real_validate",
            f"Actual (EXPLAIN ANALYZE, median of {runs}) for the {len(helped)} "
            f"queries whose plans used a new index: {before:,.0f} ms → "
            f"{after:,.0f} ms total "
            f"({(before / after) if after else 0:.1f}× faster); {len(selected)} real indexes, "
            f"{size / 1024 / 1024:,.0f} MB, built and rolled back.",
        ),
    }


def report(state: dict) -> dict:
    result = build_workload_report(state)
    message = {
        "approved": f"Workload report ready: migration creates {len(result['index_names'])} index(es).",
        "rejected": "Workload report ready. Set rejected; no migration generated.",
        "no_fix": "Workload report ready. No index set helped; no migration generated.",
    }[result["outcome"]]
    return {"report": result, "trace": _trace("report", message)}


# ---------------------------------------------------------------------------
# routing + graph
# ---------------------------------------------------------------------------

def route_after_select(state: dict) -> str:
    return "human_approval" if state["selection"]["selected"] else "report"


def route_after_approval(state: dict) -> str:
    if state.get("approved"):
        return "real_validate" if state.get("real_validate") else "report"
    if state.get("human_feedback") and state["iteration"] <= state["max_iterations"]:
        return "propose_all"
    return "report"


def build_workload_graph(checkpointer=None):

    from langgraph.checkpoint.memory import MemorySaver

    g = StateGraph(WorkloadState)
    g.add_node("collect", collect)
    g.add_node("propose_all", propose_all)
    g.add_node("evaluate", evaluate)
    g.add_node("select", select)
    g.add_node("human_approval", human_approval)
    g.add_node("real_validate", real_validate)
    g.add_node("report", report)

    g.add_edge(START, "collect")
    g.add_edge("collect", "propose_all")
    g.add_edge("propose_all", "evaluate")
    g.add_edge("evaluate", "select")
    g.add_conditional_edges("select", route_after_select, ["human_approval", "report"])
    g.add_conditional_edges("human_approval", route_after_approval,
                            ["propose_all", "real_validate", "report"])
    g.add_edge("real_validate", "report")
    g.add_edge("report", END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


def initial_workload_state(top_n: int = 6, max_indexes: int = 3,
                           min_gain_pct: float = 2.0,
                           size_penalty_pct_per_gb: float = 0.0,
                           real_validate: bool = False,
                           real_validation_runs: int = 3,
                           calls_per_day: float | None = None) -> dict:
    state = {
        "top_n": top_n, "max_indexes": max_indexes, "min_gain_pct": min_gain_pct,
        "size_penalty_pct_per_gb": size_penalty_pct_per_gb,
        "real_validate": real_validate,
        "real_validation_runs": real_validation_runs,
        "trace": [], "rounds": [],
    }
    if calls_per_day:
        state["calls_per_day"] = calls_per_day
    return state


def workload_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id},
            "recursion_limit": RECURSION_LIMIT}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(description="QueryDoctor workload mode")
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--max-indexes", type=int, default=3)
    parser.add_argument("--min-gain-pct", type=float, default=2.0)
    parser.add_argument("--size-penalty", type=float, default=0.0,
                        help="percentage points of score subtracted per GB of index")
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--real-validate", action="store_true")
    parser.add_argument("--real-validation-runs", type=int, default=3)
    parser.add_argument("--calls-per-day", type=float, default=None)
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()

    thread_id = args.thread_id or "wl-" + uuid.uuid4().hex[:10]
    graph = build_workload_graph(get_checkpointer("sqlite"))
    config = workload_config(thread_id)

    def stream(payload):
        pending = None
        for update in graph.stream(payload, config, stream_mode="updates"):
            for node, value in update.items():
                if node == "__interrupt__":
                    pending = value[0].value
                    continue
                for entry in (value or {}).get("trace", []):
                    print(f"\n[{entry['node']}] {entry['message']}")
        return pending

    print(f"QueryDoctor workload mode (thread {thread_id})")
    pending = stream(initial_workload_state(
        args.top, args.max_indexes, args.min_gain_pct, args.size_penalty,
        args.real_validate, args.real_validation_runs, args.calls_per_day))

    while pending is not None:
        print("\n" + "=" * 70)
        print(pending["message"])
        for s in pending["selected"]:
            print(f"  {s['sql']}  (+{s['marginal_gain_pct']:.1f}%, "
                  f"{(s.get('est_size_bytes') or 0) / 1024 / 1024:,.0f} MB, "
                  f"helps {', '.join(s['used_by'])})")
        print(f"  Estimated workload planner cost reduction: "
              f"{pending['workload_reduction_pct']:.1f}%")
        print("=" * 70)

        if args.auto_approve:
            decision = {"approved": True}
        elif input("Approve? [y/n] ").strip().lower().startswith("y"):
            decision = {"approved": True}
        else:
            fb = input("Feedback for the agent (empty = stop): ").strip()
            decision = {"approved": False, "feedback": fb or None}

        pending = stream(Command(resume=decision))

    result = graph.get_state(config).values.get("report")
    if result:
        out_dir = PROJECT_ROOT / "output" / thread_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(result["markdown"])
        if result["migration_sql"]:
            (out_dir / "migration.sql").write_text(result["migration_sql"])
            (out_dir / "rollback.sql").write_text(result["rollback_sql"])
        print("\n" + result["markdown"])
        print(f"\nOutcome: {result['outcome']}. Files written to {out_dir}")


if __name__ == "__main__":
    main()
