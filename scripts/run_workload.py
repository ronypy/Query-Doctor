import time
from pathlib import Path

import psycopg


QUERY_DIR = Path("workload/tpch_queries")

DATABASE = {
    "host": "localhost",
    "port": 5433,
    "dbname": "tpch",
    "user": "qd_agent",
    "password": "qd_agent",
}


# Start with these. They are enough to populate a meaningful workload.
QUERY_NUMBERS = [
    1,
    3,
    4,
    5,
    6,
    10,
    12,
    14,
    18,
    19,
]


RUNS_PER_QUERY = 3


conn = psycopg.connect(
    **DATABASE,
    autocommit=True
)


results = []


for query_num in QUERY_NUMBERS:

    path = QUERY_DIR / f"q{query_num:02d}.sql"

    if not path.exists():
        print(f"Skipping Q{query_num}: file not found")
        continue

    sql = path.read_text().strip()

    print()
    print("=" * 60)
    print(f"TPC-H Q{query_num}")
    print("=" * 60)

    for run in range(1, RUNS_PER_QUERY + 1):

        try:

            with conn.cursor() as cur:

                # Prevent a troublesome query from blocking the hackathon.
                cur.execute("SET statement_timeout = '30000ms'")

                start = time.perf_counter()

                cur.execute(sql)

                # Make sure result data is consumed.
                if cur.description:
                    cur.fetchall()

                elapsed = time.perf_counter() - start

            print(
                f"Run {run}: "
                f"{elapsed:.3f} seconds"
            )

            results.append(
                (
                    query_num,
                    run,
                    elapsed,
                    "success"
                )
            )

        except Exception as e:

            error = str(e).split("\n")[0]

            print(
                f"Run {run}: FAILED — {error}"
            )

            results.append(
                (
                    query_num,
                    run,
                    None,
                    error
                )
            )

            # Don't run the same broken query another two times.
            break


conn.close()


print()
print("=" * 60)
print("WORKLOAD COMPLETE")
print("=" * 60)
