from pathlib import Path
from pprint import pprint

from querydoctor.tools.validate import compare_costs


sql = Path(
    "workload/tpch_queries/q06.sql"
).read_text()


candidate = """
CREATE INDEX ON lineitem
(l_shipdate, l_discount, l_quantity)
"""


result = compare_costs(
    sql,
    candidate
)


print("\n============================")
print("Q6 RESULT")
print("============================")

print(
    f"Baseline cost: "
    f"{result['baseline_cost']:,.2f}"
)

print(
    f"New cost: "
    f"{result['new_cost']:,.2f}"
)

print(
    f"Reduction: "
    f"{result['reduction_pct']:.2f}%"
)

print(
    f"Index used: "
    f"{result['index_used']}"
)

print(
    f"Hypothetical index: "
    f"{result['hypothetical_index']}"
)

print("\nNew plan summary:")

pprint(
    result["new_summary"]
)
