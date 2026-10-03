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
    cur.execute("EXPLAIN (ANALYZE, TIMING OFF, BUFFERS, FORMAT JSON) " + sql)
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


def real_validate_set(queries: list[tuple[str, str]], index_sqls: list[str],
                      runs: int = 3, timeout_s: int = 900) -> dict:
    """
    Real validation of a SET of indexes against several queries, in ONE
    rolled-back transaction: measure every query, build every index,
    measure every query again.

    `queries` is [(name, sql), ...]. Returns per-query medians and runs,
    which real indexes each query's plan used, and per-index actual size
    and build time.
    """

    allowed, reason = real_validation_status()
    if not allowed:
        raise RealValidationDisabled(reason)

    builds = []
    for index_sql in index_sqls:
        check = check_index_sql(index_sql)
        if not check["ok"]:
            raise ValueError(f"Refusing to build index: {check['reason']}")
        create_sql, name = _real_index_statement(check["sql"])
        builds.append({"sql": check["sql"], "create_sql": create_sql, "name": name})

    cleaned = [(name, sql.strip().rstrip(";")) for name, sql in queries]
    per_query = {name: {} for name, _ in cleaned}

    conn = get_conn(admin=True)

    try:
        # force_rollback: the transaction is rolled back even on success.
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cur:

                cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
                cur.execute("SET LOCAL maintenance_work_mem = '512MB'")

                for name, query in cleaned:
                    per_query[name]["before_runs_ms"], _ = _measure(cur, query, runs)

                for b in builds:
                    t = time.perf_counter()
                    cur.execute(b["create_sql"])
                    b["build_seconds"] = time.perf_counter() - t
                    cur.execute("SELECT pg_relation_size(%s::regclass)", (b["name"],))
                    b["actual_size_bytes"] = int(cur.fetchone()[0])

                for name, query in cleaned:
                    runs_ms, plan = _measure(cur, query, runs)
                    per_query[name]["after_runs_ms"] = runs_ms
                    per_query[name]["indexes_used"] = [
                        b["sql"] for b in builds
                        if plan_uses_index(plan["Plan"], b["name"])
                    ]

        # Defensive check: no index may exist after the rollback.
        with conn.cursor() as cur:
            for b in builds:
                cur.execute("SELECT to_regclass(%s)", (b["name"],))
                if cur.fetchone()[0] is not None:
                    raise RuntimeError(f"Index {b['name']} still exists after rollback")

    finally:
        conn.close()

    for r in per_query.values():
        r["before_ms"] = statistics.median(r["before_runs_ms"])
        r["after_ms"] = statistics.median(r["after_runs_ms"])
        r["speedup"] = r["before_ms"] / r["after_ms"] if r["after_ms"] > 0 else None
        r["runtime_reduction_pct"] = (
            (r["before_ms"] - r["after_ms"]) / r["before_ms"] * 100
            if r["before_ms"] > 0 else 0.0
        )

    return {
        "queries": per_query,
        "indexes": [{k: b[k] for k in ("sql", "name", "actual_size_bytes",
                                       "build_seconds")} for b in builds],
        "runs": runs,
        "method": "EXPLAIN (ANALYZE, TIMING OFF) in a rolled-back transaction "
                  f"on the local demo DB; median of {runs} warm run(s)",
    }


def real_validate(sql: str, index_sql: str, runs: int = 3,
                  timeout_s: int = 600) -> dict:
    """
    Measure actual EXPLAIN ANALYZE latency before/after one real index.
    Returns medians in ms, every run, actual index size and build time.
    """

    r = real_validate_set([("query", sql)], [index_sql], runs, timeout_s)
    q = r["queries"]["query"]
    index = r["indexes"][0]

    return {
        "index_sql": index["sql"],
        "index_name": index["name"],
        "runs": runs,
        "before_ms": q["before_ms"],
        "after_ms": q["after_ms"],
        "before_runs_ms": q["before_runs_ms"],
        "after_runs_ms": q["after_runs_ms"],
        "speedup": q["speedup"],
        "runtime_reduction_pct": q["runtime_reduction_pct"],
        "index_used": bool(q["indexes_used"]),
        "actual_size_bytes": index["actual_size_bytes"],
        "build_seconds": index["build_seconds"],
        "method": r["method"],
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
