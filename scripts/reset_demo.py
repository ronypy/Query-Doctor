"""
Return the local demo database to a clean state.

    python -m scripts.reset_demo                # full reset + workload rerun
    python -m scripts.reset_demo --no-workload  # drop indexes / reset stats only

- drops every public index that does not back a constraint (PK/unique/...)
- resets pg_stat_statements and table/index statistics (pg_stat_reset)
- re-runs the workload so the dashboard has slow queries again

Local demo database only: refuses to run against a non-local admin URL.
"""

import argparse
import time
from urllib.parse import urlparse

from querydoctor.config import get_settings
from querydoctor.db import get_conn


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def assert_local_admin_db():
    host = urlparse(get_settings().admin_database_url).hostname
    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            f"Refusing to modify non-local database host {host!r}"
        )


def non_constraint_indexes(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ci.relname
            FROM pg_index i
            JOIN pg_class ci ON ci.oid = i.indexrelid
            JOIN pg_class ct ON ct.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = ct.relnamespace
            WHERE n.nspname = 'public'
              AND NOT EXISTS (
                  SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid
              )
            ORDER BY 1
        """)
        return [r[0] for r in cur.fetchall()]


def reset_demo(run_workload_after: bool = True, verbose: bool = True) -> dict:

    assert_local_admin_db()
    start = time.perf_counter()

    conn = get_conn(admin=True)
    try:
        dropped = non_constraint_indexes(conn)
        with conn.cursor() as cur:
            for name in dropped:
                # Name comes from the catalog; quote it as an identifier.
                cur.execute(f'DROP INDEX IF EXISTS public."{name}"')
                if verbose:
                    print(f"Dropped index {name}")
            cur.execute("SELECT hypopg_reset()")
            cur.execute("SELECT pg_stat_statements_reset()")
            cur.execute("SELECT pg_stat_reset()")
    finally:
        conn.close()

    if verbose:
        print("Reset pg_stat_statements and pg_stat counters.")

    if run_workload_after:
        from scripts.run_workload import run_workload
        run_workload(verbose=verbose)

    elapsed = time.perf_counter() - start
    if verbose:
        print(f"Reset complete in {elapsed:.1f} s")

    return {"dropped": dropped, "seconds": elapsed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--no-workload", action="store_true")
    args = parser.parse_args()
    reset_demo(run_workload_after=not args.no_workload)
