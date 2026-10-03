from pathlib import Path

from querydoctor.tools.validate import compare_costs


sql = Path(
    "workload/tpch_queries/q06.sql"
).read_text()


candidates = [

    """
    CREATE INDEX ON lineitem
    (l_shipdate)
    """,

    """
    CREATE INDEX ON lineitem
    (l_shipdate, l_discount)
    """,

    """
    CREATE INDEX ON lineitem
    (l_shipdate, l_discount, l_quantity)
    """,

]


for candidate in candidates:

    result = compare_costs(
        sql,
        candidate
    )

    print()
    print("=" * 70)

    print(
        candidate.strip()
    )

    print(
        f"Baseline: "
        f"{result['baseline_cost']:,.2f}"
    )

    print(
        f"New: "
        f"{result['new_cost']:,.2f}"
    )

    print(
        f"Reduction: "
        f"{result['reduction_pct']:.2f}%"
    )

    print(
        f"Used: "
        f"{result['index_used']}"
    )
