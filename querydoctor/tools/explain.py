from collections import Counter

from querydoctor.db import get_conn


def explain_query(sql: str, conn=None):

    owns_connection = conn is None

    if conn is None:
        conn = get_conn()

    try:

        clean_sql = sql.strip().rstrip(";")

        with conn.cursor() as cur:

            cur.execute(
                "EXPLAIN (FORMAT JSON) " + clean_sql
            )

            result = cur.fetchone()[0]

        # FORMAT JSON returns an array containing one top-level object.
        return result[0]

    finally:

        if owns_connection:
            conn.close()


# Tables with at least this many (estimated) rows count as "large".
LARGE_TABLE_ROWS = 100_000


def _round(value):
    return round(value, 2) if isinstance(value, float) else value


def summarize_plan(explain_result: dict, table_rows: dict | None = None):
    """
    Deterministic, compact summary of an EXPLAIN (FORMAT JSON) plan.

    `table_rows` maps table name -> estimated row count (see
    schema.get_table_row_estimates). When given, seq scans are flagged
    with `large_table` so the LLM can focus on them.
    """

    root = explain_result["Plan"]

    node_types = Counter()
    tables = set()
    seq_scans = []
    index_scans = []
    sorts = []
    joins = []
    aggregates = []

    def walk(node):

        node_type = node.get("Node Type", "Unknown")
        node_types[node_type] += 1

        common = {
            "node_type": node_type,
            "total_cost": _round(node.get("Total Cost")),
            "plan_rows": node.get("Plan Rows"),
        }

        table = node.get("Relation Name")
        if table:
            tables.add(table)

        if node_type in ("Seq Scan", "Parallel Seq Scan"):

            scan = {
                **common,
                "table": table,
                "filter": node.get("Filter"),
            }

            if table_rows is not None:
                rows = table_rows.get(table)
                scan["table_rows"] = rows
                scan["large_table"] = (
                    rows is not None and rows >= LARGE_TABLE_ROWS
                )

            seq_scans.append(scan)

        elif "Index" in node_type or node_type == "Bitmap Heap Scan":

            index_scans.append({
                **common,
                "table": table,
                "index": node.get("Index Name"),
                "index_cond": node.get("Index Cond"),
                "recheck_cond": node.get("Recheck Cond"),
                "filter": node.get("Filter"),
            })

        if node_type in ("Sort", "Incremental Sort"):
            sorts.append({
                **common,
                "sort_key": node.get("Sort Key"),
            })

        if node_type in ("Hash Join", "Merge Join", "Nested Loop"):
            joins.append({
                **common,
                "join_type": node.get("Join Type"),
                "hash_cond": node.get("Hash Cond"),
                "merge_cond": node.get("Merge Cond"),
                "join_filter": node.get("Join Filter"),
            })

        if node_type == "Aggregate" and node.get("Group Key"):
            if node.get("Partial Mode") != "Partial":
                aggregates.append({
                    **common,
                    "strategy": node.get("Strategy"),
                    "group_key": node.get("Group Key"),
                })

        for child in node.get("Plans", []):
            walk(child)

    walk(root)

    def compact(items):
        return [
            {k: v for k, v in item.items() if v is not None}
            for item in items
        ]

    return {
        "total_cost": root.get("Total Cost"),
        "plan_rows": root.get("Plan Rows"),
        "tables": sorted(tables),
        "node_types": dict(node_types),
        "seq_scans": compact(seq_scans),
        "large_seq_scans": [
            s["table"] for s in seq_scans if s.get("large_table")
        ],
        "index_scans": compact(index_scans),
        "sorts": compact(sorts),
        "joins": compact(joins),
        "aggregates": compact(aggregates),
    }


if __name__ == "__main__":

    sql = """
    SELECT
        SUM(l_extendedprice * l_discount) AS revenue
    FROM lineitem
    WHERE l_shipdate >= DATE '1994-01-01'
      AND l_shipdate < DATE '1994-01-01' + INTERVAL '1 year'
      AND l_discount BETWEEN 0.05 AND 0.07
      AND l_quantity < 24
    """

    result = explain_query(sql)

    summary = summarize_plan(result)

    from pprint import pprint
    pprint(summary)
