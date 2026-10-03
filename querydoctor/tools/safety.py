"""
Allow-list validation for LLM-generated index SQL.

Only a single, plain `CREATE INDEX` on an existing public table and existing
columns is accepted. Callers must execute the *normalized* SQL returned by
`check_index_sql` (regenerated from the parsed AST), never the raw input.
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


MAX_KEY_COLUMNS = 4
MAX_INCLUDE_COLUMNS = 4

# Access methods HypoPG can simulate.
ALLOWED_METHODS = {"btree", "hash", "brin"}


def _reject(reason: str) -> dict:
    return {"ok": False, "reason": reason, "sql": None, "table": None,
            "columns": [], "include": []}


def _column_names(node) -> list[str]:
    return [c.name.lower() for c in node.find_all(exp.Column)]


def check_index_sql(sql: str, column_map: dict[str, set[str]] | None = None) -> dict:
    """
    Returns {"ok", "reason", "sql", "table", "columns", "include"}.

    `sql` is the normalized statement (no CONCURRENTLY, no comments) that is
    safe to pass to hypopg_create_index(). `column_map` (table -> columns)
    defaults to the live schema.
    """

    if not isinstance(sql, str) or not sql.strip():
        return _reject("Empty SQL")

    try:
        statements = [
            s for s in sqlglot.parse(sql, read="postgres") if s is not None
        ]
    except SqlglotError as e:
        return _reject(f"SQL could not be parsed: {str(e).splitlines()[0]}")

    if len(statements) != 1:
        return _reject(
            f"Exactly one statement is allowed, got {len(statements)}"
        )

    stmt = statements[0]

    if not isinstance(stmt, exp.Create) or str(stmt.args.get("kind", "")).upper() != "INDEX":
        kind = stmt.key.upper()
        if isinstance(stmt, exp.Create):
            kind = f"CREATE {stmt.args.get('kind')}"
        return _reject(f"Only CREATE INDEX is allowed, got {kind}")

    if stmt.args.get("unique"):
        return _reject("UNIQUE indexes are not allowed (they change semantics)")

    if stmt.args.get("replace"):
        return _reject("CREATE OR REPLACE is not allowed")

    index = stmt.this
    if not isinstance(index, exp.Index):
        return _reject("Malformed CREATE INDEX statement")

    table_node = index.args.get("table")
    params = index.args.get("params")

    if table_node is None or params is None:
        return _reject("CREATE INDEX must name a table and columns")

    if table_node.args.get("catalog"):
        return _reject("Cross-database references are not allowed")

    schema = table_node.db
    if schema and schema.lower() != "public":
        return _reject(f"Only tables in schema public are allowed, got {schema}")

    table = table_node.name.lower()

    if column_map is None:
        from querydoctor.tools.schema import get_column_map
        column_map = get_column_map()

    if table not in column_map:
        return _reject(f"Unknown table: {table}")

    using = params.args.get("using")
    if using is not None and using.name.lower() not in ALLOWED_METHODS:
        return _reject(
            f"Index method {using.name} is not allowed "
            f"(allowed: {', '.join(sorted(ALLOWED_METHODS))})"
        )

    key_nodes = params.args.get("columns") or []
    include_nodes = params.args.get("include") or []
    where = params.args.get("where")

    if not key_nodes:
        return _reject("Index must have at least one key column")

    if len(key_nodes) > MAX_KEY_COLUMNS:
        return _reject(
            f"Too many key columns: {len(key_nodes)} (max {MAX_KEY_COLUMNS})"
        )

    if len(include_nodes) > MAX_INCLUDE_COLUMNS:
        return _reject(
            f"Too many INCLUDE columns: {len(include_nodes)} "
            f"(max {MAX_INCLUDE_COLUMNS})"
        )

    if where is not None:
        # Multi-column partial hypothetical indexes were observed to crash the
        # PostgreSQL backend (SIGSEGV, whole instance restarts) under
        # HypoPG 1.4.3 while planning TPC-H Q18, so partial indexes are not
        # allowed at all.
        return _reject(
            "Partial indexes (WHERE ...) are not allowed: they can crash the "
            "PostgreSQL backend under HypoPG 1.4.3"
        )

    key_columns = []
    for node in key_nodes:
        names = _column_names(node)
        if not names:
            return _reject(f"Key expression references no column: {node.sql()}")
        key_columns.append(node.this.sql(dialect="postgres"))

    include_columns = [n.name.lower() for n in include_nodes]

    plain_keys = {
        node.this.name.lower() for node in key_nodes
        if isinstance(node.this, exp.Column)
    }
    overlap = sorted(plain_keys & set(include_columns))
    if overlap:
        return _reject(
            f"INCLUDE columns must not repeat key columns: {', '.join(overlap)}"
        )

    referenced = set()
    for node in key_nodes:
        referenced.update(_column_names(node))
    referenced.update(include_columns)
    if where is not None:
        referenced.update(_column_names(where))

    unknown = sorted(referenced - column_map[table])
    if unknown:
        return _reject(
            f"Unknown column(s) on {table}: {', '.join(unknown)}"
        )

    # HypoPG does not accept CONCURRENTLY; it is added back in the migration.
    stmt.set("concurrently", False)

    return {
        "ok": True,
        "reason": "OK",
        "sql": stmt.sql(dialect="postgres", comments=False),
        "table": table,
        "columns": key_columns,
        "include": include_columns,
    }


def validate_index_sql(sql: str, column_map: dict[str, set[str]] | None = None) -> tuple[bool, str]:
    result = check_index_sql(sql, column_map)
    return result["ok"], result["reason"]
