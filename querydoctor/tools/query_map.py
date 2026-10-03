"""
Map pg_stat_statements queryids back to the concrete TPC-H SQL files.

pg_stat_statements normalizes literals to $1, $2, ... so its text cannot be
EXPLAINed. With pg_stat_statements loaded, PostgreSQL computes the same query
identifier for `EXPLAIN (VERBOSE)` output, so we can get each file's queryid
without executing the query.

Note: queryids depend on table OIDs. Rebuild the map after reloading TPC-H:

    python -m querydoctor.tools.query_map
"""

import json
import re

from querydoctor.config import PROJECT_ROOT
from querydoctor.db import get_conn


QUERY_DIR = PROJECT_ROOT / "workload" / "tpch_queries"
QUERY_MAP_PATH = PROJECT_ROOT / "data" / "query_map.json"

QUERY_FILE_RE = re.compile(r"^q(\d{2})\.sql$")


def _clean(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def compute_queryid(sql: str, conn) -> int | None:

    with conn.cursor() as cur:
        cur.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + _clean(sql))
        result = cur.fetchone()[0]

    return result[0].get("Query Identifier")


def build_query_map(conn=None) -> dict:

    owns_connection = conn is None

    if conn is None:
        conn = get_conn()

    queries = {}
    errors = {}

    try:
        for path in sorted(QUERY_DIR.glob("q*.sql")):

            if not QUERY_FILE_RE.match(path.name):
                continue

            name = path.stem
            sql = _clean(path.read_text())

            try:
                queryid = compute_queryid(sql, conn)
            except Exception as e:
                errors[name] = str(e).split("\n")[0]
                continue

            queries[str(queryid)] = {
                "name": name,
                "file": str(path.relative_to(PROJECT_ROOT)),
                "sql": sql,
            }

    finally:
        if owns_connection:
            conn.close()

    query_map = {"queries": queries, "errors": errors}

    QUERY_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUERY_MAP_PATH.write_text(json.dumps(query_map, indent=2))

    return query_map


def load_query_map() -> dict:

    if not QUERY_MAP_PATH.exists():
        return build_query_map()

    return json.loads(QUERY_MAP_PATH.read_text())


def lookup(key) -> dict | None:
    """
    Find a query by queryid (int or str) or by name ("q12", "Q12", "12").
    Returns {"queryid", "name", "file", "sql"} or None.
    """

    queries = load_query_map()["queries"]

    key = str(key).strip()

    if key in queries:
        return {"queryid": int(key), **queries[key]}

    name = key.lower()
    if name.isdigit():
        name = f"q{int(name):02d}"
    elif re.fullmatch(r"q\d", name):
        name = f"q0{name[1]}"

    for queryid, entry in queries.items():
        if entry["name"] == name:
            return {"queryid": int(queryid), **entry}

    return None


def get_query_sql(key) -> str:

    entry = lookup(key)

    if entry is None:
        raise KeyError(f"No concrete SQL mapped for {key!r}")

    return entry["sql"]


if __name__ == "__main__":

    result = build_query_map()

    for queryid, entry in result["queries"].items():
        print(f"{entry['name']}: {queryid}")

    for name, error in result["errors"].items():
        print(f"{name}: ERROR {error}")

    print(f"\nWrote {QUERY_MAP_PATH}")
