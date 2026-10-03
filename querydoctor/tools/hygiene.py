"""
Index hygiene: unused, duplicate and prefix-redundant indexes.

Read-only catalog analysis. Findings come with suggested
`DROP INDEX CONCURRENTLY` SQL and a recreate statement for rollback —
nothing is executed. Indexes backing a constraint (PK / unique /
exclusion) and unique indexes are never suggested for removal.

    python -m querydoctor.tools.hygiene
"""

from querydoctor.db import get_conn


# Below this stats window, "unused" findings are flagged as low-confidence.
MIN_CONFIDENT_WINDOW_HOURS = 24 * 7


INDEX_SQL = """
SELECT
    ci.relname                                   AS index_name,
    ct.relname                                   AS table_name,
    am.amname                                    AS method,
    i.indnkeyatts                                AS n_key,
    i.indkey::int2[]                             AS attnums,
    i.indclass::oid[]                            AS opclasses,
    i.indcollation::oid[]                        AS collations,
    i.indoption::int2[]                          AS options,
    ARRAY(
        SELECT coalesce(a.attname, '(expression)')
        FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
        LEFT JOIN pg_attribute a
               ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        ORDER BY k.ord
    )                                            AS columns,
    pg_get_expr(i.indexprs, i.indrelid)          AS expressions,
    pg_get_expr(i.indpred, i.indrelid)           AS predicate,
    i.indisunique                                AS is_unique,
    i.indisvalid                                 AS is_valid,
    EXISTS (SELECT 1 FROM pg_constraint c
            WHERE c.conindid = i.indexrelid)     AS backs_constraint,
    pg_relation_size(i.indexrelid)               AS size_bytes,
    pg_get_indexdef(i.indexrelid)                AS definition,
    coalesce(s.idx_scan, 0)                      AS idx_scan
FROM pg_index i
JOIN pg_class ci     ON ci.oid = i.indexrelid
JOIN pg_class ct     ON ct.oid = i.indrelid
JOIN pg_namespace n  ON n.oid = ct.relnamespace
JOIN pg_am am        ON am.oid = ci.relam
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
WHERE n.nspname = %s
ORDER BY ct.relname, ci.relname
"""


def _protected(ix: dict) -> bool:
    return ix["backs_constraint"] or ix["is_unique"]


def _key(ix: dict, n: int | None = None) -> tuple:
    """Comparable key-column signature (first n key columns)."""
    n = ix["n_key"] if n is None else n
    return (
        tuple(ix["attnums"][:n]),
        tuple(ix["opclasses"][:n]),
        tuple(ix["collations"][:n]),
        tuple(ix["options"][:n]),
    )


def _signature(ix: dict) -> tuple:
    """Everything that makes two indexes functionally identical."""
    return (
        ix["table_name"], ix["method"], ix["n_key"],
        tuple(ix["attnums"]), tuple(ix["opclasses"]),
        tuple(ix["collations"]), tuple(ix["options"]),
        ix["expressions"], ix["predicate"],
    )


def _drop_sql(name: str, schema: str) -> str:
    return f'DROP INDEX CONCURRENTLY IF EXISTS {schema}."{name}";'


def _recreate_sql(definition: str) -> str:
    return definition.replace(" INDEX ", " INDEX CONCURRENTLY ", 1) + ";"


def _stats_window(cur) -> tuple:
    cur.execute("""
        SELECT stats_reset,
               extract(epoch FROM now() - stats_reset) / 3600.0
        FROM pg_stat_database WHERE datname = current_database()
    """)
    reset, hours = cur.fetchone()
    return reset, (float(hours) if hours is not None else None)


def analyze_index_hygiene(conn=None, schema: str = "public") -> dict:

    owns_connection = conn is None
    if conn is None:
        conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(INDEX_SQL, (schema,))
            cols = [d.name for d in cur.description]
            indexes = [dict(zip(cols, row)) for row in cur.fetchall()]
            stats_reset, window_hours = _stats_window(cur)
    finally:
        if owns_connection:
            conn.close()

    findings: dict[str, dict] = {}

    def flag(ix: dict, kind: str, reason: str, related: str | None = None):
        f = findings.setdefault(ix["index_name"], {
            "index": ix["index_name"],
            "table": ix["table_name"],
            "size_bytes": int(ix["size_bytes"]),
            "idx_scan": int(ix["idx_scan"]),
            "definition": ix["definition"],
            "kinds": [],
            "reasons": [],
            "related": [],
            "drop_sql": _drop_sql(ix["index_name"], schema),
            "recreate_sql": _recreate_sql(ix["definition"]),
        })
        if kind not in f["kinds"]:
            f["kinds"].append(kind)
            f["reasons"].append(reason)
        if related and related not in f["related"]:
            f["related"].append(related)

    # ---- duplicates: identical definitions; keep one (protected first)
    groups: dict[tuple, list[dict]] = {}
    for ix in indexes:
        groups.setdefault(_signature(ix), []).append(ix)

    duplicate_of = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        keep = sorted(group, key=lambda x: (not _protected(x), -x["idx_scan"],
                                            x["index_name"]))[0]
        for ix in group:
            if ix is keep or _protected(ix):
                continue
            duplicate_of[ix["index_name"]] = keep["index_name"]
            flag(ix, "duplicate",
                 f"Identical to {keep['index_name']} (same columns, method, "
                 "operator classes and predicate).", keep["index_name"])

    # ---- prefix-redundant: B-tree A's key columns are a leading prefix of B's
    for a in indexes:
        if (_protected(a) or a["method"] != "btree" or a["predicate"]
                or a["expressions"] or a["index_name"] in duplicate_of):
            continue
        for b in indexes:
            if (b is a or b["table_name"] != a["table_name"]
                    or b["method"] != "btree" or b["predicate"]
                    or b["expressions"] or not b["is_valid"]
                    or b["n_key"] <= a["n_key"]):
                continue
            if _key(a) != _key(b, a["n_key"]):
                continue
            # A's INCLUDE columns must also be available in B.
            a_include = set(a["attnums"][a["n_key"]:])
            if not a_include <= set(b["attnums"]):
                continue
            flag(a, "redundant",
                 f"Key columns ({', '.join(a['columns'][:a['n_key']])}) are a "
                 f"leading prefix of {b['index_name']} "
                 f"({', '.join(b['columns'][:b['n_key']])}); "
                 f"{b['index_name']} can serve the same lookups.",
                 b["index_name"])
            break

    # ---- unused: never scanned since the stats reset
    confident = window_hours is not None and window_hours >= MIN_CONFIDENT_WINDOW_HOURS
    for ix in indexes:
        if _protected(ix) or ix["idx_scan"] > 0:
            continue
        window = (f"{window_hours:.1f} h" if window_hours is not None
                  else "unknown window")
        flag(ix, "unused",
             f"0 index scans since statistics reset ({window})"
             + ("" if confident else " — short window, verify before dropping")
             + ".")

    result = sorted(findings.values(),
                    key=lambda f: (-f["size_bytes"], f["index"]))

    return {
        "findings": result,
        "index_count": len(indexes),
        "reclaimable_bytes": sum(f["size_bytes"] for f in result),
        "stats_reset": stats_reset.isoformat() if stats_reset else None,
        "stats_window_hours": window_hours,
        "unused_confident": confident,
    }


def hygiene_markdown(result: dict) -> str:

    lines = ["# Index hygiene report", ""]

    window = result["stats_window_hours"]
    lines.append(
        f"{result['index_count']} indexes analyzed. Statistics window: "
        + (f"{window:.1f} hours" if window is not None else "unknown")
        + ("" if result["unused_confident"]
           else " (short — treat 'unused' findings as hints).")
    )
    lines.append("")

    if not result["findings"]:
        lines.append("No unused, duplicate or redundant indexes found.")
        return "\n".join(lines)

    lines.append(
        f"Potentially reclaimable: **{result['reclaimable_bytes'] / 1024 / 1024:,.0f} MB**"
    )
    lines.append("")
    lines += ["| Index | Table | Size | Scans | Finding |", "|---|---|---|---|---|"]
    for f in result["findings"]:
        lines.append(
            f"| `{f['index']}` | {f['table']} | "
            f"{f['size_bytes'] / 1024 / 1024:,.1f} MB | {f['idx_scan']} | "
            f"{'; '.join(f['reasons'])} |"
        )

    lines += ["", "## Suggested cleanup (review before running)", "", "```sql"]
    lines += [f["drop_sql"] for f in result["findings"]]
    lines += ["```", "", "## Rollback", "", "```sql"]
    lines += [f["recreate_sql"] for f in result["findings"]]
    lines += ["```"]

    return "\n".join(lines)


if __name__ == "__main__":
    print(hygiene_markdown(analyze_index_hygiene()))
