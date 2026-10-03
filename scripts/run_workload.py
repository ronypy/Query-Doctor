"""
Run TPC-H workload queries to populate pg_stat_statements.

    python -m scripts.run_workload            # default 10 queries × 3
"""

import time

from querydoctor.config import PROJECT_ROOT
from querydoctor.db import get_conn


QUERY_DIR = PROJECT_ROOT / "workload" / "tpch_queries"

# Start with these. They are enough to populate a meaningful workload.
QUERY_NUMBERS = [1, 3, 4, 5, 6, 10, 12, 14, 18, 19]

RUNS_PER_QUERY = 3


def run_workload(query_numbers=QUERY_NUMBERS, runs=RUNS_PER_QUERY,
                 timeout_ms=30000, verbose=True):

    results = []

    conn = get_conn()

    try:
        for query_num in query_numbers:

            path = QUERY_DIR / f"q{query_num:02d}.sql"

            if not path.exists():
                if verbose:
                    print(f"Skipping Q{query_num}: file not found")
                continue

            sql = path.read_text().strip()

            if verbose:
                print()
                print("=" * 60)
                print(f"TPC-H Q{query_num}")
                print("=" * 60)

            for run in range(1, runs + 1):

                try:
                    with conn.cursor() as cur:

                        # Prevent a troublesome query from blocking the hackathon.
                        cur.execute(f"SET statement_timeout = {int(timeout_ms)}")

                        start = time.perf_counter()
                        cur.execute(sql)

                        # Make sure result data is consumed.
                        if cur.description:
                            cur.fetchall()

                        elapsed = time.perf_counter() - start

                    if verbose:
                        print(f"Run {run}: {elapsed:.3f} seconds")

                    results.append((query_num, run, elapsed, "success"))

                except Exception as e:

                    error = str(e).split("\n")[0]

                    if verbose:
                        print(f"Run {run}: FAILED — {error}")

                    results.append((query_num, run, None, error))

                    # Don't run the same broken query another two times.
                    break

    finally:
        conn.close()

    if verbose:
        print()
        print("=" * 60)
        print("WORKLOAD COMPLETE")
        print("=" * 60)

    return results


if __name__ == "__main__":
    run_workload()
