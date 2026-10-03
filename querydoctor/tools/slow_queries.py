from querydoctor.db import get_conn
from querydoctor.tools.query_map import load_query_map


def get_slow_queries(limit: int = 10, only_mapped: bool = False):
    """
    Slowest top-level SELECTs from pg_stat_statements, aggregated by queryid
    (the same query can appear once per user).

    `query` is the normalized pg_stat_statements text ($1, $2, ...). When the
    queryid is in data/query_map.json, `name` and `sql` hold the concrete
    workload query — use `sql` (never `query`) for EXPLAIN.
    """

    sql = """
        SELECT
            queryid,
            min(query) AS query,
            sum(calls) AS calls,
            sum(total_exec_time) / nullif(sum(calls), 0) AS mean_exec_time,
            sum(total_exec_time) AS total_exec_time,
            sum(rows) AS rows
        FROM pg_stat_statements
        WHERE toplevel
          AND query NOT ILIKE %s
          AND query ILIKE %s
        GROUP BY queryid
        ORDER BY total_exec_time DESC
    """

    queries = load_query_map()["queries"]

    conn = get_conn()

    try:
        with conn.cursor() as cur:

            cur.execute(
                sql,
                ("%pg_stat_statements%", "select%")
            )

            rows = cur.fetchall()

    finally:
        conn.close()

    result = []

    for row in rows:

        mapped = queries.get(str(row[0]))

        if only_mapped and mapped is None:
            continue

        result.append({
            "queryid": row[0],
            "query": row[1],
            "calls": int(row[2]),
            "mean_exec_time": float(row[3] or 0),
            "total_exec_time": float(row[4]),
            "rows": int(row[5]),
            "name": mapped["name"] if mapped else None,
            "sql": mapped["sql"] if mapped else None,
        })

        if len(result) >= limit:
            break

    return result


if __name__ == "__main__":

    queries = get_slow_queries()

    for i, q in enumerate(queries, start=1):

        print()
        print(f"#{i} {q['name'] or '(unmapped)'}")
        print(f"Query ID: {q['queryid']}")
        print(f"Calls: {q['calls']}")
        print(f"Mean: {q['mean_exec_time']:.2f} ms")
        print(f"Total: {q['total_exec_time']:.2f} ms")
        print(q["query"][:150])


def get_query_stats(queryid) -> dict | None:
    """Measured pg_stat_statements numbers for one queryid (all users)."""

    if queryid is None:
        return None

    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sum(calls),
                       sum(total_exec_time) / nullif(sum(calls), 0),
                       sum(total_exec_time),
                       sum(rows)
                FROM pg_stat_statements
                WHERE toplevel AND queryid = %s
            """, (int(queryid),))
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None or row[0] is None:
        return None

    return {
        "calls": int(row[0]),
        "mean_exec_time": float(row[1] or 0),
        "total_exec_time": float(row[2]),
        "rows": int(row[3]),
    }
