"""
Real validation: build the index for real and measure actual latency with
EXPLAIN ANALYZE — inside ONE transaction that is always rolled back.

PostgreSQL DDL is transactional, so:

    BEGIN
      EXPLAIN ANALYZE <query>      (1 warm-up + N runs)  -> median "before"
      CREATE INDEX qd_rv_... ON ...                         (real index)
      pg_relation_size(...)                                 (actual size)
      EXPLAIN ANALYZE <query>      (1 warm-up + N runs)  -> median "after"
    ROLLBACK                                                (index is gone)

Other sessions never see the uncommitted index, and nothing can be left
behind (a crash also aborts the transaction). Writes to the table are
blocked while it runs — acceptable on the local demo database only.

Guarded: requires ENABLE_REAL_VALIDATION=1 and a localhost admin URL.
"""

import hashlib
import statistics
import time
from urllib.parse import urlparse

import sqlglot
from sqlglot import exp

from querydoctor.config import get_settings
from querydoctor.db import get_conn
from querydoctor.tools.safety import check_index_sql
from querydoctor.tools.validate import plan_uses_index


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Serializes real validations across sessions (pg_advisory_xact_lock key).
ADVISORY_LOCK_KEY = 0x51D0C70


class RealValidationDisabled(RuntimeError):
    pass


def real_validation_status() -> tuple[bool, str]:
    """(allowed, reason) — the UI shows the reason when disabled."""

    settings = get_settings()

    if not settings.enable_real_validation:
        return False, "Set ENABLE_REAL_VALIDATION=1 in .env to enable."

    host = urlparse(settings.admin_database_url).hostname
    if host not in LOCAL_HOSTS:
        return False, f"Admin database host {host!r} is not local."

    return True, "Enabled on the local demo database."


def _explain_analyze(cur, sql: str) -> dict:
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
    return cur.fetchone()[0][0]


def _measure(cur, sql: str, runs: int) -> tuple[list[float], dict]:
    _explain_analyze(cur, sql)                       # warm-up (cache)
    plans = [_explain_analyze(cur, sql) for _ in range(runs)]
    return [p["Execution Time"] for p in plans], plans[-1]


def _real_index_statement(normalized_sql: str) -> tuple[str, str]:
    """Same index definition, with a unique qd_rv_ name, no CONCURRENTLY."""

    name = "qd_rv_" + hashlib.sha1(normalized_sql.encode()).hexdigest()[:12]

    stmt = sqlglot.parse_one(normalized_sql, read="postgres")
    stmt.this.set("this", exp.to_identifier(name))
    stmt.set("concurrently", False)
    stmt.set("exists", False)

    return stmt.sql(dialect="postgres"), name


def real_validate(sql: str, index_sql: str, runs: int = 3,
                  timeout_s: int = 600) -> dict:
    """
    Measure actual EXPLAIN ANALYZE latency before/after a real index.
    Returns medians in ms, every run, actual index size and build time.
    """

    allowed, reason = real_validation_status()
    if not allowed:
        raise RealValidationDisabled(reason)

    check = check_index_sql(index_sql)
    if not check["ok"]:
        raise ValueError(f"Refusing to build index: {check['reason']}")

    create_sql, name = _real_index_statement(check["sql"])
    query = sql.strip().rstrip(";")

    conn = get_conn(admin=True)

    try:
        # force_rollback: the transaction is rolled back even on success.
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cur:

                cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
                cur.execute("SET LOCAL maintenance_work_mem = '512MB'")

                before_runs, before_plan = _measure(cur, query, runs)

                t = time.perf_counter()
                cur.execute(create_sql)
                build_seconds = time.perf_counter() - t

                cur.execute("SELECT pg_relation_size(%s::regclass)", (name,))
                actual_size = int(cur.fetchone()[0])

                after_runs, after_plan = _measure(cur, query, runs)

        # Defensive check: the index must not exist after the rollback.
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (name,))
            if cur.fetchone()[0] is not None:
                raise RuntimeError(f"Index {name} still exists after rollback")

    finally:
        conn.close()

    before_ms = statistics.median(before_runs)
    after_ms = statistics.median(after_runs)

    return {
        "index_sql": check["sql"],
        "index_name": name,
        "runs": runs,
        "before_ms": before_ms,
        "after_ms": after_ms,
        "before_runs_ms": before_runs,
        "after_runs_ms": after_runs,
        "speedup": before_ms / after_ms if after_ms > 0 else None,
        "runtime_reduction_pct": (before_ms - after_ms) / before_ms * 100
                                 if before_ms > 0 else 0.0,
        "index_used": plan_uses_index(after_plan["Plan"], name),
        "actual_size_bytes": actual_size,
        "build_seconds": build_seconds,
        "method": "EXPLAIN ANALYZE in a rolled-back transaction on the local "
                  f"demo DB; median of {runs} warm runs",
    }


if __name__ == "__main__":
    import argparse
    from querydoctor.tools.query_map import get_query_sql

    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--index", required=True)
    args = parser.parse_args()

    r = real_validate(get_query_sql(args.query), args.index)
    print(f"{r['index_sql']}")
    print(f"before {r['before_ms']:.1f} ms → after {r['after_ms']:.1f} ms "
          f"({r['speedup']:.1f}×), used={r['index_used']}, "
          f"size {r['actual_size_bytes'] / 1024 / 1024:.0f} MB, "
          f"build {r['build_seconds']:.1f} s")
