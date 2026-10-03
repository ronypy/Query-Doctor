"""
Workload mode engine (deterministic, no LLM).

Optimizes the top-N queries together: given a pool of candidate indexes,
it measures every candidate on every query with HypoPG, then greedily picks
the set that most reduces the *total workload planner cost*

    workload_cost = Σ_q calls_q × planner_cost_q

Each greedy round re-plans every query with ALL already-selected indexes
plus one more candidate, so a candidate's score is its *marginal* gain —
redundant indexes (whose benefit is already provided) score ~0 and are
skipped automatically. An optional size penalty models write/storage
overhead. Selection stops at `max_indexes` or when the best marginal gain
drops below `min_gain_pct` of the baseline workload cost.

All numbers are PostgreSQL planner estimates with hypothetical indexes.
"""

import psycopg

from querydoctor.db import CRASH_ERROR, get_conn, wait_for_db
from querydoctor.tools.explain import explain_query, summarize_plan
from querydoctor.tools.hypopg import (
    create_hypothetical_index,
    hypothetical_index_size,
    reset_hypothetical,
)
from querydoctor.tools.schema import (
    get_column_map,
    get_table_info,
    get_table_row_estimates,
)
from querydoctor.tools.slow_queries import get_slow_queries
from querydoctor.tools.validate import plan_uses_index


def collect_workload(top_n: int = 6, conn=None) -> list[dict]:
    """Top-N mapped slow queries with baseline plan, cost and schema."""

    owns_connection = conn is None
    if conn is None:
        conn = get_conn()

    try:
        table_rows = get_table_row_estimates(conn=conn)
        queries = []

        for q in get_slow_queries(limit=top_n, only_mapped=True):
            summary = summarize_plan(explain_query(q["sql"], conn=conn), table_rows)
            queries.append({
                "name": q["name"],
                "queryid": str(q["queryid"]),
                "sql": q["sql"],
                "calls": q["calls"],
                "mean_exec_time": q["mean_exec_time"],
                "total_exec_time": q["total_exec_time"],
                "baseline_cost": float(summary["total_cost"]),
                "plan_summary": summary,
                "table_info": get_table_info(summary["tables"], conn=conn),
            })
    finally:
        if owns_connection:
            conn.close()

    return queries


def _plan_all(conn, queries, index_sqls):
    """
    One HypoPG experiment: create `index_sqls`, EXPLAIN every query,
    reset. Returns per-query cost and which hypothetical indexes it used.
    """

    reset_hypothetical(conn)
    try:
        hypos = [create_hypothetical_index(conn, sql) for sql in index_sqls]
        out = {}
        for q in queries:
            plan = explain_query(q["sql"], conn=conn)
            out[q["name"]] = {
                "cost": float(plan["Plan"]["Total Cost"]),
                "used": [
                    sql for sql, h in zip(index_sqls, hypos)
                    if plan_uses_index(plan["Plan"], h["indexname"])
                ],
            }
        return out
    finally:
        reset_hypothetical(conn)


class _Session:
    """Connection holder that can reconnect after a backend crash."""

    def __init__(self, conn):
        self.conn = conn if conn is not None else get_conn()
        self._own = [] if conn is not None else [self.conn]

    def recover(self):
        wait_for_db()
        self.conn = get_conn()
        self._own.append(self.conn)

    def close(self):
        for c in self._own:
            try:
                c.close()
            except Exception:
                pass


def _mark_crashed(cand: dict) -> None:
    cand.update({"crashed": True, "error": CRASH_ERROR, "per_query": {},
                 "benefit": 0.0, "queries_helped": []})


def _weighted(queries, costs: dict) -> float:
    return sum(q["calls"] * costs[q["name"]] for q in queries)


def evaluate_matrix(queries: list[dict], candidates: list[dict], conn) -> list[dict]:
    """
    Single-candidate effect on every query. Adds to each candidate:
    est_size_bytes, per_query {name: {cost, reduction_pct, used}},
    benefit (Σ calls × cost drop where used), queries_helped.
    """

    session = _Session(conn)
    try:
        for cand in candidates:
            try:
                _evaluate_one(session.conn, queries, cand)
            except psycopg.OperationalError:
                if not session.conn.closed:
                    raise
                session.recover()
                _mark_crashed(cand)
    finally:
        if session.conn is not conn:
            session.close()

    return candidates


def _evaluate_one(conn, queries, cand) -> None:

        reset_hypothetical(conn)
        try:
            hypo = create_hypothetical_index(conn, cand["sql"])
            cand["est_size_bytes"] = hypothetical_index_size(conn, hypo["indexrelid"])
        finally:
            reset_hypothetical(conn)

        planned = _plan_all(conn, queries, [cand["sql"]])

        per_query = {}
        benefit = 0.0
        for q in queries:
            p = planned[q["name"]]
            used = bool(p["used"])
            drop = q["baseline_cost"] - p["cost"] if used else 0.0
            per_query[q["name"]] = {
                "cost": p["cost"],
                "used": used,
                "reduction_pct": (drop / q["baseline_cost"] * 100
                                  if q["baseline_cost"] else 0.0),
            }
            benefit += q["calls"] * max(0.0, drop)

        cand["per_query"] = per_query
        cand["benefit"] = benefit
        cand["queries_helped"] = [
            n for n, v in per_query.items() if v["used"] and v["reduction_pct"] > 0.5
        ]


def greedy_select(
    queries: list[dict],
    candidates: list[dict],
    conn,
    max_indexes: int = 3,
    min_gain_pct: float = 2.0,
    size_penalty_pct_per_gb: float = 0.0,
) -> dict:
    """
    Greedy marginal-gain selection (see module docstring).
    Returns selected indexes, per-round steps, per-query before/after,
    and the reason each unselected candidate was left out.
    """

    baseline_costs = {q["name"]: q["baseline_cost"] for q in queries}
    baseline_total = _weighted(queries, baseline_costs)

    selected: list[dict] = []
    current_costs = dict(baseline_costs)
    current_total = baseline_total
    current_used: dict = {q["name"]: [] for q in queries}
    steps = []
    pool = [c for c in candidates if c.get("queries_helped")]

    session = _Session(conn)

    while pool and len(selected) < max_indexes:

        scored = []
        for cand in list(pool):
            try:
                planned = _plan_all(session.conn, queries,
                                    [s["sql"] for s in selected] + [cand["sql"]])
            except psycopg.OperationalError:
                if not session.conn.closed:
                    raise
                session.recover()
                _mark_crashed(cand)
                pool.remove(cand)
                continue
            costs = {n: v["cost"] for n, v in planned.items()}
            used_by = [n for n, v in planned.items() if cand["sql"] in v["used"]]
            total = _weighted(queries, costs)
            gain_pct = (current_total - total) / baseline_total * 100
            penalty = size_penalty_pct_per_gb * cand.get("est_size_bytes", 0) / 1024 ** 3
            scored.append({
                "cand": cand, "gain_pct": gain_pct, "score": gain_pct - penalty,
                "costs": costs, "total": total, "used_by": used_by,
                "planned": planned,
            })

        if not scored:
            break
        scored.sort(key=lambda s: s["score"], reverse=True)
        best = scored[0]

        if not best["used_by"] or best["gain_pct"] < min_gain_pct:
            break

        cand = best["cand"]
        selected.append({
            **{k: cand[k] for k in ("sql", "table", "est_size_bytes", "sources",
                                    "rationale")},
            "marginal_gain_pct": best["gain_pct"],
            "used_by": best["used_by"],
        })
        current_costs = best["costs"]
        current_total = best["total"]
        current_used = {n: v["used"] for n, v in best["planned"].items()}

        steps.append({
            "round": len(selected),
            "chosen": cand["sql"],
            "marginal_gain_pct": best["gain_pct"],
            "cumulative_reduction_pct": (baseline_total - current_total)
                                        / baseline_total * 100,
            "runners_up": [
                {"sql": s["cand"]["sql"], "marginal_gain_pct": s["gain_pct"]}
                for s in scored[1:4]
            ],
        })
        pool = [c for c in pool if c is not cand]

    if session.conn is not conn:
        session.close()

    # Why each candidate was not selected.
    selected_sqls = {s["sql"] for s in selected}
    not_selected = []
    for cand in candidates:
        if cand["sql"] in selected_sqls:
            continue
        if cand.get("crashed"):
            reason = CRASH_ERROR
        elif not cand.get("valid", True):
            reason = f"rejected by safety check: {cand.get('error')}"
        elif not cand.get("queries_helped"):
            reason = "not used by the planner for any workload query"
        elif len(selected) >= max_indexes:
            reason = f"max {max_indexes} indexes reached"
        else:
            reason = (f"marginal gain below {min_gain_pct:.1f}% once the "
                      "selected indexes exist (redundant)")
        not_selected.append({"sql": cand["sql"], "reason": reason,
                             "benefit": cand.get("benefit", 0.0)})

    per_query = []
    for q in queries:
        before = baseline_costs[q["name"]]
        after = current_costs[q["name"]]
        per_query.append({
            "name": q["name"],
            "calls": q["calls"],
            "mean_exec_time": q["mean_exec_time"],
            "baseline_cost": before,
            "final_cost": after,
            "reduction_pct": (before - after) / before * 100 if before else 0.0,
            "indexes_used": current_used.get(q["name"], []),
        })

    return {
        "selected": selected,
        "steps": steps,
        "per_query": per_query,
        "not_selected": not_selected,
        "baseline_workload_cost": baseline_total,
        "final_workload_cost": current_total,
        "workload_reduction_pct": (baseline_total - current_total)
                                  / baseline_total * 100 if baseline_total else 0.0,
        "total_size_bytes": sum(s.get("est_size_bytes") or 0 for s in selected),
        "params": {"max_indexes": max_indexes, "min_gain_pct": min_gain_pct,
                   "size_penalty_pct_per_gb": size_penalty_pct_per_gb},
    }


def build_candidate_pool(proposals: dict[str, list[dict]], column_map=None) -> list[dict]:
    """
    Safety-check and deduplicate LLM proposals from all queries.
    `proposals` maps query name -> [{"sql", "rationale"}].
    """

    from querydoctor.tools.safety import check_index_sql

    if column_map is None:
        column_map = get_column_map()

    pool: dict[str, dict] = {}
    rejected = []

    for query_name, cands in proposals.items():
        for c in cands:
            check = check_index_sql(c["sql"], column_map)
            if not check["ok"]:
                rejected.append({"sql": c["sql"].strip().rstrip(";"),
                                 "source": query_name,
                                 "valid": False, "error": check["reason"]})
                continue
            entry = pool.setdefault(check["sql"], {
                "sql": check["sql"],
                "table": check["table"],
                "rationale": c.get("rationale"),
                "sources": [],
                "valid": True,
                "error": None,
            })
            if query_name not in entry["sources"]:
                entry["sources"].append(query_name)

    return list(pool.values()), rejected
