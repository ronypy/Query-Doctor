import psycopg

from querydoctor.db import get_conn

from querydoctor.tools.explain import (
    explain_query,
    summarize_plan,
)

from querydoctor.tools.hypopg import (
    create_hypothetical_index,
    hypothetical_index_size,
    reset_hypothetical,
)

from querydoctor.tools.safety import check_index_sql


def plan_uses_index(node, index_name: str):

    if node.get("Index Name") == index_name:
        return True

    for child in node.get("Plans", []):
        if plan_uses_index(child, index_name):
            return True

    return False


def _reduction_pct(baseline_cost: float, new_cost: float) -> float:

    if baseline_cost <= 0:
        return 0.0

    return (baseline_cost - new_cost) / baseline_cost * 100


def _run_experiment(conn, sql, checked, baseline_cost, table_rows):
    """
    One independent HypoPG experiment on `conn`: reset, create every
    candidate in `checked`, EXPLAIN, reset. Returns per-index usage.
    """

    reset_hypothetical(conn)

    try:
        hypos = []

        for c in checked:
            hypo = create_hypothetical_index(conn, c["sql"])
            hypo["size_bytes"] = hypothetical_index_size(
                conn, hypo["indexrelid"]
            )
            hypos.append(hypo)

        plan = explain_query(sql, conn=conn)

    finally:
        reset_hypothetical(conn)

    summary = summarize_plan(plan, table_rows)
    new_cost = float(summary["total_cost"])

    return {
        "new_cost": new_cost,
        "reduction_pct": _reduction_pct(baseline_cost, new_cost),
        "indexes": [
            {
                "sql": c["sql"],
                "hypothetical_index": h["indexname"],
                "used": plan_uses_index(plan["Plan"], h["indexname"]),
                "est_size_bytes": h["size_bytes"],
            }
            for c, h in zip(checked, hypos)
        ],
        "new_summary": summary,
    }


def compare_costs(
    sql: str,
    index_sqls: str | list[str],
    conn=None,
    table_rows: dict | None = None,
    column_map: dict | None = None,
):
    """
    Estimate planner-cost impact of hypothetical indexes with HypoPG.

    Every candidate is first checked by the sqlglot allow-list; only the
    normalized SQL reaches HypoPG. Then, on ONE connection (HypoPG indexes
    are session-local):

      - baseline: plain EXPLAIN with no hypothetical indexes
      - each valid candidate on its own (hypopg_reset() in between)
      - all valid candidates together, if there is more than one

    Never uses EXPLAIN ANALYZE: hypothetical indexes only affect plain
    EXPLAIN. All numbers are *estimated planner cost*, not runtime.

    Top-level `new_cost` / `reduction_pct` / `index_used` describe the
    whole valid set applied together (for a single index that is just the
    index), which keeps the original single-index return shape.
    """

    if isinstance(index_sqls, str):
        index_sqls = [index_sqls]

    owns_connection = conn is None

    if conn is None:
        conn = get_conn()

    try:

        # ------------------
        # Safety check (no SQL reaches the DB before this)
        # ------------------

        if column_map is None:
            from querydoctor.tools.schema import get_column_map
            column_map = get_column_map(conn=conn)

        candidates = []
        checked = []

        for raw in index_sqls:

            check = check_index_sql(raw, column_map)

            candidate = {
                "input_sql": raw.strip(),
                "sql": check["sql"],
                "table": check["table"],
                "valid": check["ok"],
                "error": None if check["ok"] else check["reason"],
                "hypothetical_index": None,
                "new_cost": None,
                "reduction_pct": None,
                "index_used": False,
                "est_size_bytes": None,
                "new_summary": None,
            }

            candidates.append(candidate)

            if check["ok"]:
                checked.append(check)

        # ------------------
        # Baseline (clean session)
        # ------------------

        reset_hypothetical(conn)

        baseline_plan = explain_query(sql, conn=conn)
        baseline_summary = summarize_plan(baseline_plan, table_rows)
        baseline_cost = float(baseline_summary["total_cost"])

        # ------------------
        # Each candidate individually
        # ------------------

        by_sql = {}

        for candidate in candidates:

            if not candidate["valid"]:
                continue

            check = next(c for c in checked if c["sql"] == candidate["sql"])

            try:
                run = _run_experiment(
                    conn, sql, [check], baseline_cost, table_rows
                )
            except psycopg.Error as e:
                candidate["valid"] = False
                candidate["error"] = (
                    "HypoPG/EXPLAIN failed: " + str(e).split("\n")[0]
                )
                continue

            index = run["indexes"][0]

            candidate.update({
                "hypothetical_index": index["hypothetical_index"],
                "new_cost": run["new_cost"],
                "reduction_pct": run["reduction_pct"],
                "index_used": index["used"],
                "est_size_bytes": index["est_size_bytes"],
                "new_summary": run["new_summary"],
            })

            by_sql[candidate["sql"]] = candidate

        # ------------------
        # Whole set together
        # ------------------

        valid = [c for c in checked if c["sql"] in by_sql]
        combined = None

        if len(valid) == 1:
            only = by_sql[valid[0]["sql"]]
            combined = {
                "sqls": [only["sql"]],
                "new_cost": only["new_cost"],
                "reduction_pct": only["reduction_pct"],
                "indexes_used": {only["sql"]: only["index_used"]},
                "new_summary": only["new_summary"],
            }

        elif len(valid) > 1:
            run = _run_experiment(conn, sql, valid, baseline_cost, table_rows)
            combined = {
                "sqls": [c["sql"] for c in valid],
                "new_cost": run["new_cost"],
                "reduction_pct": run["reduction_pct"],
                "indexes_used": {
                    i["sql"]: i["used"] for i in run["indexes"]
                },
                "new_summary": run["new_summary"],
            }

        used = [c for c in candidates if c["valid"] and c["index_used"]]
        best = max(used, key=lambda c: c["reduction_pct"], default=None)

        first = candidates[0]

        return {
            "baseline_cost": baseline_cost,
            "baseline_summary": baseline_summary,
            "candidates": candidates,
            "combined": combined,
            "best": best,

            # Single-index compatible fields (whole valid set together).
            "index_sql": first["sql"] or first["input_sql"],
            "hypothetical_index": first["hypothetical_index"],
            "new_cost": combined["new_cost"] if combined else baseline_cost,
            "reduction_pct": combined["reduction_pct"] if combined else 0.0,
            "index_used": (
                any(combined["indexes_used"].values()) if combined else False
            ),
            "new_summary": combined["new_summary"] if combined else None,
        }

    finally:

        try:
            reset_hypothetical(conn)
        finally:
            if owns_connection:
                conn.close()
