"""
QueryDoctor LangGraph workflow.

START → fetch_context → diagnose → propose → safety_check → validate → critic
critic:          accept → human_approval | retry → propose | give_up → report
human_approval:  approved → report | rejected + feedback → propose | rejected → report
report → END

CLI:
    python -m querydoctor.agent.graph --query q14
"""

import argparse
import sqlite3
import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from querydoctor.agent import nodes
from querydoctor.agent.state import AgentState
from querydoctor.config import PROJECT_ROOT


CHECKPOINT_DB = PROJECT_ROOT / "data" / "checkpoints.db"

# Each iteration is ~5 node steps; max_iterations * 2 rounds (with human
# feedback) stays far below this.
RECURSION_LIMIT = 80


def get_checkpointer(kind: str = "sqlite"):

    if kind == "memory":
        return MemorySaver()

    from langgraph.checkpoint.sqlite import SqliteSaver

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
    return SqliteSaver(conn)


def build_graph(checkpointer=None):

    g = StateGraph(AgentState)

    g.add_node("fetch_context", nodes.fetch_context)
    g.add_node("diagnose", nodes.diagnose)
    g.add_node("propose", nodes.propose)
    g.add_node("safety_check", nodes.safety_check)
    g.add_node("validate", nodes.validate)
    g.add_node("critic", nodes.critic)
    g.add_node("human_approval", nodes.human_approval)
    g.add_node("report", nodes.report)

    g.add_edge(START, "fetch_context")
    g.add_edge("fetch_context", "diagnose")
    g.add_edge("diagnose", "propose")
    g.add_edge("propose", "safety_check")
    g.add_edge("safety_check", "validate")
    g.add_edge("validate", "critic")

    g.add_conditional_edges(
        "critic",
        nodes.route_after_critic,
        ["propose", "human_approval", "report"],
    )
    g.add_conditional_edges(
        "human_approval",
        nodes.route_after_approval,
        ["propose", "report"],
    )
    g.add_edge("report", END)

    return g.compile(
        checkpointer=checkpointer if checkpointer is not None else MemorySaver()
    )


def run_config(thread_id: str) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }


def initial_state(query_key: str, threshold: float | None = None,
                  max_iterations: int | None = None) -> dict:

    state = {"query_key": query_key, "history": [], "trace": []}

    if threshold is not None:
        state["threshold"] = threshold
    if max_iterations is not None:
        state["max_iterations"] = max_iterations

    return state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_updates(stream) -> dict | None:
    """Print trace entries as nodes finish; return an interrupt payload."""

    pending = None

    for update in stream:
        for node, value in update.items():

            if node == "__interrupt__":
                pending = value[0].value
                continue

            for entry in (value or {}).get("trace", []):
                print(f"\n[{entry['node']}] {entry['message']}")

    return pending


def _ask_approval(payload: dict) -> dict:

    best = payload["best"]
    size_mb = (best.get("est_size_bytes") or 0) / 1024 / 1024

    print("\n" + "=" * 70)
    print(payload["message"])
    print(f"  {best['sql']}")
    print(f"  Estimated planner cost: {best['baseline_cost']:,.0f} → "
          f"{best['new_cost']:,.0f} ({best['reduction_pct']:.1f}% reduction)")
    print(f"  Estimated index size: {size_mb:,.0f} MB")
    print("=" * 70)

    answer = input("Approve? [y/n] ").strip().lower()

    if answer.startswith("y"):
        return {"approved": True}

    feedback = input("Feedback for the agent (empty = stop): ").strip()
    return {"approved": False, "feedback": feedback or None}


def main():

    parser = argparse.ArgumentParser(description="Run QueryDoctor on one query")
    parser.add_argument("--query", required=True, help="q14, 14, or a queryid")
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--threshold", type=float, default=None,
                        help="fraction, e.g. 0.30")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--checkpointer", choices=["sqlite", "memory"],
                        default="sqlite")
    args = parser.parse_args()

    thread_id = args.thread_id or uuid.uuid4().hex[:12]
    graph = build_graph(get_checkpointer(args.checkpointer))
    config = run_config(thread_id)

    print(f"QueryDoctor — {args.query} (thread {thread_id})")

    pending = _print_updates(graph.stream(
        initial_state(args.query, args.threshold, args.max_iterations),
        config,
        stream_mode="updates",
    ))

    while pending is not None:

        decision = {"approved": True} if args.auto_approve else _ask_approval(pending)

        pending = _print_updates(graph.stream(
            Command(resume=decision), config, stream_mode="updates",
        ))

    report = graph.get_state(config).values.get("report")

    if report:
        out_dir = PROJECT_ROOT / "output" / thread_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(report["markdown"])
        if report["migration_sql"]:
            (out_dir / "migration.sql").write_text(report["migration_sql"])
            (out_dir / "rollback.sql").write_text(report["rollback_sql"])

        print("\n" + "=" * 70)
        print(report["markdown"])
        print("=" * 70)
        print(f"Outcome: {report['outcome']}. Files written to {out_dir}")


if __name__ == "__main__":
    main()
