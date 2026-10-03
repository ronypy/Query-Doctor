"""
Measure estimated planner-cost reduction (HypoPG) for candidate indexes on
several TPC-H queries, to pick reliable demo queries.

    python -m scripts.explore_demo_queries

Writes data/demo_candidates.json. Numbers are PostgreSQL planner cost
estimates with hypothetical indexes — not runtimes.
"""

import json

from querydoctor.config import PROJECT_ROOT
from querydoctor.db import get_conn
from querydoctor.tools.query_map import get_query_sql
from querydoctor.tools.schema import get_column_map, get_table_row_estimates
from querydoctor.tools.validate import compare_costs


CANDIDATES = {
    "q03": [
        "CREATE INDEX ON customer (c_mktsegment, c_custkey)",
        "CREATE INDEX ON orders (o_custkey, o_orderdate)",
        "CREATE INDEX ON orders (o_orderdate) INCLUDE (o_custkey, o_shippriority)",
        "CREATE INDEX ON lineitem (l_orderkey) INCLUDE (l_shipdate, l_extendedprice, l_discount)",
    ],
    "q04": [
        "CREATE INDEX ON orders (o_orderdate)",
        "CREATE INDEX ON orders (o_orderdate) INCLUDE (o_orderpriority)",
        "CREATE INDEX ON lineitem (l_orderkey) WHERE l_commitdate < l_receiptdate",
    ],
    "q05": [
        "CREATE INDEX ON orders (o_orderdate)",
        "CREATE INDEX ON orders (o_orderdate) INCLUDE (o_custkey)",
        "CREATE INDEX ON customer (c_nationkey)",
        "CREATE INDEX ON supplier (s_nationkey)",
    ],
    "q06": [
        "CREATE INDEX ON lineitem (l_shipdate)",
        "CREATE INDEX ON lineitem (l_shipdate, l_discount, l_quantity)",
        "CREATE INDEX ON lineitem (l_shipdate, l_discount, l_quantity) INCLUDE (l_extendedprice)",
        "CREATE INDEX ON lineitem (l_discount, l_shipdate, l_quantity) INCLUDE (l_extendedprice)",
    ],
    "q10": [
        "CREATE INDEX ON orders (o_orderdate)",
        "CREATE INDEX ON orders (o_orderdate) INCLUDE (o_custkey)",
        "CREATE INDEX ON lineitem (l_returnflag, l_orderkey)",
        "CREATE INDEX ON lineitem (l_orderkey) INCLUDE (l_returnflag, l_extendedprice, l_discount)",
    ],
    "q12": [
        "CREATE INDEX ON lineitem (l_receiptdate)",
        "CREATE INDEX ON lineitem (l_shipmode, l_receiptdate)",
        "CREATE INDEX ON lineitem (l_shipmode, l_receiptdate) INCLUDE (l_orderkey, l_commitdate, l_shipdate)",
        "CREATE INDEX ON lineitem (l_receiptdate) WHERE l_commitdate < l_receiptdate AND l_shipdate < l_commitdate",
    ],
    "q14": [
        "CREATE INDEX ON lineitem (l_shipdate)",
        "CREATE INDEX ON lineitem (l_shipdate) INCLUDE (l_partkey, l_extendedprice, l_discount)",
        "CREATE INDEX ON part (p_partkey) INCLUDE (p_type)",
    ],
    "q17": [
        "CREATE INDEX ON lineitem (l_partkey)",
        "CREATE INDEX ON lineitem (l_partkey) INCLUDE (l_quantity, l_extendedprice)",
        "CREATE INDEX ON part (p_brand, p_container)",
    ],
    "q18": [
        "CREATE INDEX ON lineitem (l_orderkey) INCLUDE (l_quantity)",
        "CREATE INDEX ON orders (o_custkey)",
    ],
    "q19": [
        "CREATE INDEX ON part (p_brand, p_container, p_size)",
        "CREATE INDEX ON lineitem (l_partkey)",
        "CREATE INDEX ON lineitem (l_shipinstruct, l_shipmode, l_partkey)",
    ],
    "q20": [
        "CREATE INDEX ON lineitem (l_partkey, l_suppkey)",
        "CREATE INDEX ON lineitem (l_partkey, l_suppkey, l_shipdate) INCLUDE (l_quantity)",
        "CREATE INDEX ON part (p_name)",
    ],
    "q02": [
        "CREATE INDEX ON partsupp (ps_partkey)",
        "CREATE INDEX ON part (p_size)",
        "CREATE INDEX ON supplier (s_nationkey)",
    ],
}


def main():

    conn = get_conn()
    column_map = get_column_map(conn=conn)
    table_rows = get_table_row_estimates(conn=conn)

    results = {}

    try:
        for name, candidates in CANDIDATES.items():

            sql = get_query_sql(name)

            r = compare_costs(
                sql,
                candidates,
                conn=conn,
                table_rows=table_rows,
                column_map=column_map,
            )

            results[name] = {
                "baseline_cost": r["baseline_cost"],
                "large_seq_scans": r["baseline_summary"]["large_seq_scans"],
                "candidates": [
                    {
                        "sql": c["sql"] or c["input_sql"],
                        "valid": c["valid"],
                        "error": c["error"],
                        "new_cost": c["new_cost"],
                        "reduction_pct": c["reduction_pct"],
                        "index_used": c["index_used"],
                        "est_size_bytes": c["est_size_bytes"],
                    }
                    for c in r["candidates"]
                ],
                "combined": {
                    "new_cost": r["combined"]["new_cost"],
                    "reduction_pct": r["combined"]["reduction_pct"],
                    "indexes_used": r["combined"]["indexes_used"],
                } if r["combined"] else None,
            }

            print()
            print(f"{name.upper()}  baseline {r['baseline_cost']:,.0f}  "
                  f"large seq scans: {', '.join(results[name]['large_seq_scans'])}")

            for c in results[name]["candidates"]:
                if not c["valid"]:
                    print(f"   INVALID  {c['sql']}  ({c['error']})")
                    continue
                size_mb = (c["est_size_bytes"] or 0) / 1024 / 1024
                print(f"   {c['reduction_pct']:6.2f}%  used={str(c['index_used']):5s} "
                      f"{size_mb:6.0f} MB  {c['sql']}")

            if r["combined"]:
                print(f"   {r['combined']['reduction_pct']:6.2f}%  (all together)")

    finally:
        conn.close()

    out = PROJECT_ROOT / "data" / "demo_candidates.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
