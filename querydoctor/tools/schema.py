from querydoctor.db import get_conn


def _with_conn(fn):
    """Run fn(conn) on the given connection or a temporary agent one."""

    def wrapper(*args, conn=None, **kwargs):

        if conn is not None:
            return fn(*args, conn=conn, **kwargs)

        conn = get_conn()
        try:
            return fn(*args, conn=conn, **kwargs)
        finally:
            conn.close()

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_with_conn
def get_table_row_estimates(conn=None) -> dict[str, int]:
    """Planner row estimates (pg_class.reltuples) for public tables."""

    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.relname, c.reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
        """)
        return {name: int(rows) for name, rows in cur.fetchall()}


@_with_conn
def get_column_map(conn=None) -> dict[str, set[str]]:
    """table -> set of column names, for every public table."""

    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
        """)

        columns: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            columns.setdefault(table, set()).add(column)

    return columns


@_with_conn
def get_table_info(tables: list[str], conn=None) -> list[dict]:
    """
    For each table: columns + types, estimated rows, total size and
    existing indexes. Unknown tables are skipped.
    """

    tables = sorted({t.lower() for t in tables})

    if not tables:
        return []

    with conn.cursor() as cur:

        cur.execute("""
            SELECT c.relname,
                   c.reltuples::bigint,
                   pg_total_relation_size(c.oid),
                   pg_size_pretty(pg_total_relation_size(c.oid))
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND c.relname = ANY(%s)
        """, (tables,))
        meta = {r[0]: r[1:] for r in cur.fetchall()}

        cur.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            ORDER BY table_name, ordinal_position
        """, (tables,))
        columns: dict[str, list[dict]] = {}
        for table, column, data_type in cur.fetchall():
            columns.setdefault(table, []).append(
                {"name": column, "type": data_type}
            )

        cur.execute("""
            SELECT tablename, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = ANY(%s)
            ORDER BY tablename, indexname
        """, (tables,))
        indexes: dict[str, list[dict]] = {}
        for table, name, definition in cur.fetchall():
            indexes.setdefault(table, []).append(
                {"name": name, "definition": definition}
            )

    result = []

    for table in tables:

        if table not in meta:
            continue

        rows, size_bytes, size_pretty = meta[table]

        result.append({
            "table": table,
            "estimated_rows": int(rows),
            "size_bytes": int(size_bytes),
            "size": size_pretty,
            "columns": columns.get(table, []),
            "indexes": indexes.get(table, []),
        })

    return result


if __name__ == "__main__":

    from pprint import pprint

    pprint(get_table_info(["lineitem", "orders", "nope"]))
    pprint(get_table_row_estimates())
