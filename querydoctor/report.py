"""
Migration SQL, rollback SQL and a markdown report for an agent run.

Every number comes from PostgreSQL: runtimes from pg_stat_statements
(measured), costs from EXPLAIN (planner estimates), reductions from
HypoPG hypothetical indexes (estimated). Actual runtime improvement is only
reported if real validation ran.
"""

import hashlib
import re
from datetime import date

import sqlglot
from sqlglot import exp

from querydoctor.savings import (
    CHANGE_LABELS,
    NOISE_BAND,
    classify_change,
    savings_for_state,
)
from querydoctor.tools.safety import check_index_sql


MAX_IDENTIFIER = 63


def index_name(table: str, columns: list[str]) -> str:
    """Deterministic name: qd_<table>_<cols>, at most 63 characters."""

    parts = [re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") for c in columns]
    name = "qd_" + "_".join([table.lower()] + [p for p in parts if p])

    if len(name) > MAX_IDENTIFIER:
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        name = name[: MAX_IDENTIFIER - 9] + "_" + digest

    return name


def migration_statement(index_sql: str, column_map: dict | None = None,
                        name: str | None = None) -> tuple[str, str]:
    """
    Turn a validated hypothetical-index statement into
    CREATE INDEX CONCURRENTLY IF NOT EXISTS qd_... ON ...
    Returns (create_sql, index_name).
    """

    check = check_index_sql(index_sql, column_map)
    if not check["ok"]:
        raise ValueError(f"Refusing to build migration: {check['reason']}")

    if name is None:
        name = index_name(check["table"], check["columns"])

    stmt = sqlglot.parse_one(check["sql"], read="postgres")
    stmt.this.set("this", exp.to_identifier(name))
    stmt.set("concurrently", True)
    stmt.set("exists", True)

    return stmt.sql(dialect="postgres"), name


def _fmt_cost(value) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "n/a"


def _fmt_size(size_bytes) -> str:
    if not size_bytes:
        return "n/a"
    return f"{size_bytes / 1024 / 1024:,.0f} MB"


def _attempts_table(history: list[dict]) -> str:

    lines = [
        "| Iteration | Candidate index | Est. planner cost reduction | Used by planner | Est. size |",
        "|---|---|---|---|---|",
    ]

    for attempt in history:
        for c in attempt["candidates"]:
            if not c.get("valid", True):
                lines.append(
                    f"| {attempt['iteration']} | `{c['sql']}` | rejected: {c['error']} | – | – |"
                )
            else:
                lines.append(
                    f"| {attempt['iteration']} | `{c['sql']}` | "
                    f"{c['reduction_pct']:.1f}% | "
                    f"{'yes' if c['index_used'] else 'no'} | "
                    f"{_fmt_size(c.get('est_size_bytes'))} |"
                )

    return "\n".join(lines)


def build_report(state: dict) -> dict:

    best = state.get("best")
    critique = state.get("critique") or {}
    stats = state.get("stats") or {}
    history = state.get("history") or []
    threshold_pct = state.get("threshold", 0.30) * 100
    query_label = (state.get("query_name") or state.get("query_key") or "query").upper()

    accepted = critique.get("verdict") == "accept"
    approved = bool(state.get("approved"))

    if accepted and approved:
        outcome = "approved"
    elif accepted:
        outcome = "rejected"
    else:
        outcome = "no_fix"

    migration_sql = rollback_sql = name = None

    if outcome == "approved":

        create_sql, name = migration_statement(best["sql"])
        today = date.today().isoformat()

        migration_sql = (
            f"-- QueryDoctor migration — {today}\n"
            f"-- Query: {query_label} (queryid {state.get('query_id')})\n"
            f"-- Estimated planner cost reduction (HypoPG): {best['reduction_pct']:.1f}%\n"
            f"-- Estimated index size: {_fmt_size(best.get('est_size_bytes'))}\n"
            + (
                f"-- Measured locally: {best['actual']['before_ms']:,.0f} ms -> "
                f"{best['actual']['after_ms']:,.0f} ms, actual size "
                f"{_fmt_size(best['actual']['actual_size_bytes'])}\n"
                if best.get("actual") else ""
            ) +
            f"-- CONCURRENTLY cannot run inside a transaction block.\n"
            f"{create_sql};\n"
        )

        rollback_sql = (
            f"-- Rollback for {name}\n"
            f"DROP INDEX CONCURRENTLY IF EXISTS {name};\n"
        )

    # ---------- markdown ----------

    md = [f"# QueryDoctor report — {query_label}", ""]

    md += ["## Query", "", "```sql", state.get("sql", "").strip(), "```", ""]

    md += ["## Measured runtime (pg_stat_statements)", ""]
    if stats:
        md += [
            f"- Mean execution time: **{stats['mean_exec_time']:,.0f} ms** "
            f"over {stats['calls']} calls",
            f"- Total execution time: {stats['total_exec_time']:,.0f} ms",
            "",
        ]
    else:
        md += ["- No pg_stat_statements entry for this query.", ""]

    md += ["## Bottleneck", "", state.get("diagnosis") or "n/a", ""]

    md += [
        "## Attempts (HypoPG hypothetical indexes)",
        "",
        f"Acceptance rule: the planner must use the index and the estimated "
        f"planner cost must drop by at least {threshold_pct:.0f}%.",
        "",
        _attempts_table(history),
        "",
    ]

    md += ["## Result", ""]

    if outcome == "no_fix":
        md += [
            f"**No recommendation.** {critique.get('feedback', '')}",
            "",
            "No candidate met the acceptance rule, so no index is proposed. "
            "This is reported honestly rather than recommending a weak index.",
            "",
        ]
    else:
        md += [
            f"Recommended index: `{best['sql']}`",
            "",
            f"- Baseline planner cost: {_fmt_cost(best.get('baseline_cost'))}",
            f"- Planner cost with hypothetical index: {_fmt_cost(best.get('new_cost'))}",
            f"- **Estimated planner cost reduction: {best['reduction_pct']:.1f}%**",
            f"- Estimated index size: {_fmt_size(best.get('est_size_bytes'))}",
            f"- Human decision: **{'approved' if approved else 'rejected'}**"
            + (f" — {state['human_feedback']}" if state.get("human_feedback") else ""),
            "",
        ]

        if best.get("rationale"):
            md += [f"Rationale: {best['rationale']}", ""]

    actual = (best or {}).get("actual")
    savings = savings_for_state(state) if actual else None

    md += ["## Actual runtime improvement", ""]
    if actual:
        md += [
            f"Measured with a **real index** and EXPLAIN ANALYZE on the local demo "
            f"database ({actual['method']}):",
            "",
            f"- Before: **{actual['before_ms']:,.1f} ms** "
            f"(runs: {', '.join(f'{x:,.1f}' for x in actual['before_runs_ms'])})",
            f"- After: **{actual['after_ms']:,.1f} ms** "
            f"(runs: {', '.join(f'{x:,.1f}' for x in actual['after_runs_ms'])})",
            (f"- **{actual['speedup']:.1f}× faster** "
             f"({actual['runtime_reduction_pct']:.1f}% lower execution time)"
             if actual["after_ms"] < actual["before_ms"] * (1 - NOISE_BAND) else
             f"- **{CHANGE_LABELS[classify_change(actual['before_ms'], actual['after_ms'], actual['index_used'])]}** "
             f"({actual['runtime_reduction_pct']:+.1f}% execution time change)"),
            f"- Real index used by the planner: {'yes' if actual['index_used'] else 'no'}",
            f"- Actual index size: {_fmt_size(actual['actual_size_bytes'])} "
            f"(HypoPG estimate: {_fmt_size(best.get('est_size_bytes'))}); "
            f"build time {actual['build_seconds']:.1f} s",
            "- The index was built inside a transaction that was rolled back; "
            "nothing was left in the database.",
            "",
            "Caveats: warm cache, single local machine, TPC-H SF1 — production "
            "latency will differ.",
            "",
        ]
    else:
        if (best or {}).get("actual_error"):
            md += [f"Real validation failed or was skipped: {best['actual_error']}", ""]
        md += [
            "Not measured. The numbers above are PostgreSQL planner estimates "
            "with a hypothetical index; planner cost reduction does not translate "
            "directly into the same runtime reduction.",
            "",
        ]

    md += ["## Estimated monthly savings", ""]
    if savings:
        md += [
            "Estimate from the **measured** runtimes above (not from planner cost):",
            "",
            f"- Time saved per call: {savings['saved_ms_per_call']:,.1f} ms",
            f"- Calls per day: {savings['calls_per_day']:,.0f} "
            f"({savings['calls_per_day_source']})",
            f"- CPU time saved: {savings['monthly_cpu_hours_saved']:,.1f} vCPU-hours / month",
            f"- Compute saved: **${savings['monthly_compute_savings_usd']:,.2f} / month** "
            f"at ${savings['vcpu_hour_usd']:.3f} per vCPU-hour (assumption)",
            f"- Index storage added: ${savings['monthly_storage_cost_usd']:,.2f} / month "
            f"at ${savings['storage_gb_month_usd']:.2f} per GB-month (assumption)",
            f"- **Net: ${savings['monthly_net_savings_usd']:,.2f} / month**",
            "",
            "Simple model: one busy vCPU per running query; ignores I/O pricing, "
            "parallel workers, cache effects and index write overhead.",
            "",
        ]
    else:
        md += [
            "Not estimated — dollar savings are only computed from measured "
            "runtimes. Approve with real validation enabled to get an estimate.",
            "",
        ]

    if outcome == "approved":
        md += [
            "## Trade-offs",
            "",
            f"- Storage: about "
            f"{_fmt_size((actual or {}).get('actual_size_bytes') or best.get('est_size_bytes'))}"
            f" of extra disk space{' (measured)' if actual else ' (HypoPG estimate)'}.",
            f"- Writes: every INSERT/UPDATE/DELETE on `{best.get('table')}` must "
            "also maintain this index.",
            "- Build it with CONCURRENTLY (below) to avoid blocking writes.",
            "",
            "## Migration", "", "```sql", migration_sql.strip(), "```", "",
            "## Rollback", "", "```sql", rollback_sql.strip(), "```", "",
        ]

    if state.get("proposal", {}).get("rewrite_sql"):
        md += [
            "## Suggested query rewrite (not validated)",
            "",
            "```sql",
            state["proposal"]["rewrite_sql"].strip(),
            "```",
            "",
            state["proposal"].get("rewrite_rationale") or "",
            "",
        ]

    return {
        "outcome": outcome,
        "index_name": name,
        "migration_sql": migration_sql,
        "rollback_sql": rollback_sql,
        "actual": actual,
        "savings": savings,
        "markdown": "\n".join(md),
    }
