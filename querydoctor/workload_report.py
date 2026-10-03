"""
Workload-mode report: combined migration/rollback for the selected index
set, per-query estimated planner-cost effect, greedy selection steps,
candidates left out (with reasons), and — only if real validation ran —
measured runtimes and a savings estimate.
"""

from datetime import date

from querydoctor.report import migration_statement
from querydoctor.savings import (
    CHANGE_LABELS,
    classify_change,
    derive_calls_per_day,
    estimate_monthly_savings,
)
from querydoctor.config import get_settings


def _mb(size_bytes) -> str:
    return f"{(size_bytes or 0) / 1024 / 1024:,.0f} MB"


def _unique_migrations(sqls: list[str]) -> list[tuple[str, str]]:
    """(create_sql, name) per index, de-duplicating qd_ names."""
    out, seen = [], set()
    for sql in sqls:
        create, name = migration_statement(sql)
        if name in seen:
            n = 2
            while f"{name[:60]}_{n}" in seen:
                n += 1
            create, name = migration_statement(sql, name=f"{name[:60]}_{n}")
        seen.add(name)
        out.append((create, name))
    return out


def workload_savings(state: dict) -> dict | None:
    """Sum of per-query savings from MEASURED runtimes only."""

    actual = state.get("actual")
    if not actual:
        return None

    assumed = state.get("calls_per_day") or get_settings().calls_per_day
    per_query = []
    reliable = True

    for q in state["queries"]:
        m = actual["queries"].get(q["name"])
        # Only count queries whose real plan used a new index; any other
        # before/after difference is measurement noise.
        if not m or not m["indexes_used"]:
            continue
        if assumed:
            cpd, source = assumed, "user-provided assumption (per query)"
        else:
            derived = derive_calls_per_day(q["queryid"])
            if not derived:
                continue
            cpd = derived["calls_per_day"]
            source = f"derived ({derived['window_hours']:.2f} h window)"
            reliable = reliable and derived["reliable"]
        s = estimate_monthly_savings(m["before_ms"], m["after_ms"], cpd, 0)
        per_query.append({"name": q["name"], "calls_per_day": cpd,
                          "source": source, **s})

    size = sum(i["actual_size_bytes"] for i in actual["indexes"])
    storage = estimate_monthly_savings(0, 0, 0, size)["monthly_storage_cost_usd"]
    compute = sum(p["monthly_compute_savings_usd"] for p in per_query)
    settings = get_settings()

    return {
        "per_query": per_query,
        "monthly_cpu_hours_saved": sum(p["monthly_cpu_hours_saved"] for p in per_query),
        "monthly_compute_savings_usd": compute,
        "monthly_storage_cost_usd": storage,
        "monthly_net_savings_usd": compute - storage,
        "vcpu_hour_usd": settings.vcpu_hour_price_usd,
        "storage_gb_month_usd": settings.storage_gb_month_usd,
        "reliable": reliable,
    }


def build_workload_report(state: dict) -> dict:

    sel = state.get("selection") or {"selected": [], "per_query": [], "steps": [],
                                     "not_selected": [], "workload_reduction_pct": 0}
    selected = sel["selected"]
    approved = bool(state.get("approved"))

    if selected and approved:
        outcome = "approved"
    elif selected:
        outcome = "rejected"
    else:
        outcome = "no_fix"

    migration_sql = rollback_sql = None
    names = []

    if outcome == "approved":
        migrations = _unique_migrations([s["sql"] for s in selected])
        names = [n for _, n in migrations]
        header = (
            f"-- QueryDoctor workload migration — {date.today().isoformat()}\n"
            f"-- Queries: {', '.join(q['name'].upper() for q in state['queries'])}\n"
            f"-- Estimated workload planner cost reduction (HypoPG): "
            f"{sel['workload_reduction_pct']:.1f}%\n"
            f"-- CONCURRENTLY cannot run inside a transaction block; run each "
            f"statement separately.\n"
        )
        migration_sql = header + "\n".join(f"{c};" for c, _ in migrations) + "\n"
        rollback_sql = ("-- Rollback for the workload migration\n"
                        + "\n".join(f"DROP INDEX CONCURRENTLY IF EXISTS {n};"
                                    for n in reversed(names)) + "\n")

    actual = state.get("actual")
    savings = workload_savings(state) if actual else None

    md = ["# QueryDoctor workload report", ""]
    md += [
        f"Optimized the top {len(state.get('queries', []))} slow queries together. "
        "Each index's score is its *marginal* reduction of the total workload "
        "planner cost (Σ calls × cost) with the already-selected indexes present, "
        "so redundant indexes are not picked twice.",
        "",
        f"Selection limits: max {sel.get('params', {}).get('max_indexes', '–')} "
        f"indexes, min marginal gain "
        f"{sel.get('params', {}).get('min_gain_pct', 0):.1f}%, size penalty "
        f"{sel.get('params', {}).get('size_penalty_pct_per_gb', 0):.1f} pts/GB.",
        "",
    ]

    md += ["## Selected indexes", ""]
    if selected:
        md += ["| # | Index | Marginal est. gain | Est. size | Used by | Proposed for |",
               "|---|---|---|---|---|---|"]
        for i, s in enumerate(selected, start=1):
            md.append(
                f"| {i} | `{s['sql']}` | {s['marginal_gain_pct']:.1f}% | "
                f"{_mb(s.get('est_size_bytes'))} | {', '.join(s['used_by'])} | "
                f"{', '.join(s.get('sources') or [])} |")
        md += ["", f"Total estimated size: {_mb(sel.get('total_size_bytes'))}. "
               f"**Estimated workload planner cost reduction: "
               f"{sel['workload_reduction_pct']:.1f}%**", ""]
        md.append(f"Human decision: **{'approved' if approved else 'rejected'}**"
                  + (f" — {state['human_feedback']}" if state.get("human_feedback") else ""))
        md.append("")
    else:
        md += ["**No recommendation.** No candidate reduced the workload planner "
               "cost enough to be worth an index.", ""]

    md += ["## Per-query effect (estimated planner cost)", "",
           "| Query | Calls | Mean runtime (measured) | Baseline cost | With selected set | Est. reduction |",
           "|---|---|---|---|---|---|"]
    for q in sel["per_query"]:
        md.append(f"| {q['name'].upper()} | {q['calls']} | {q['mean_exec_time']:,.0f} ms | "
                  f"{q['baseline_cost']:,.0f} | {q['final_cost']:,.0f} | "
                  f"{q['reduction_pct']:.1f}% |")
    md.append("")

    if sel["steps"]:
        md += ["## Greedy selection steps", ""]
        for s in sel["steps"]:
            runners = "; ".join(f"`{r['sql']}` +{r['marginal_gain_pct']:.1f}%"
                                for r in s["runners_up"]) or "none"
            md.append(f"{s['round']}. `{s['chosen']}` +{s['marginal_gain_pct']:.1f}% "
                      f"(cumulative {s['cumulative_reduction_pct']:.1f}%). "
                      f"Runners-up: {runners}")
        md.append("")

    if sel["not_selected"]:
        md += ["## Candidates not selected", ""]
        for n in sel["not_selected"]:
            md.append(f"- `{n['sql']}` — {n['reason']}")
        md.append("")

    md += ["## Actual runtime improvement", ""]
    if actual:
        md += [f"Measured with the **real index set** ({actual['method']}):", "",
               "| Query | Before | After | Speed-up | Real indexes used | Verdict |",
               "|---|---|---|---|---|---|"]
        regressions = []
        for name, r in actual["queries"].items():
            change = classify_change(r["before_ms"], r["after_ms"],
                                     bool(r["indexes_used"]))
            if change == "regression":
                regressions.append(name.upper())
            speed = f"{r['speedup']:.1f}×" if r["indexes_used"] else "–"
            md.append(f"| {name.upper()} | {r['before_ms']:,.0f} ms | "
                      f"{r['after_ms']:,.0f} ms | {speed} | "
                      f"{len(r['indexes_used'])} | {CHANGE_LABELS[change]} |")
        if regressions:
            md += ["", f"**Warning:** {', '.join(regressions)} got slower with the "
                   "new indexes even though the planner chose them. Consider "
                   "rejecting the set or removing the index those queries use."]
        size = sum(i["actual_size_bytes"] for i in actual["indexes"])
        md += ["", f"Actual total index size: {_mb(size)}. All indexes were built in a "
               "transaction that was rolled back; nothing was left in the database.",
               "", "Caveats: warm cache, single local machine, TPC-H SF1.", ""]
    else:
        if state.get("actual_error"):
            md += [f"Real validation failed or was skipped: {state['actual_error']}", ""]
        md += ["Not measured. Numbers above are planner estimates with hypothetical "
               "indexes, not runtimes.", ""]

    md += ["## Estimated monthly savings", ""]
    if savings:
        md += [
            "From the **measured** runtimes above (not from planner cost), counting "
            "only queries whose plan used a new index; slower queries count as "
            "negative savings:", "",
            f"- CPU time saved: {savings['monthly_cpu_hours_saved']:,.1f} vCPU-hours / month "
            f"(calls/day: {savings['per_query'][0]['source'] if savings['per_query'] else 'n/a'}"
            f"{', ' + format(savings['per_query'][0]['calls_per_day'], ',.0f') if savings['per_query'] else ''})",
            f"- Compute saved: **${savings['monthly_compute_savings_usd']:,.2f} / month** "
            f"at ${savings['vcpu_hour_usd']:.3f} per vCPU-hour (assumption)",
            f"- Index storage added: ${savings['monthly_storage_cost_usd']:,.2f} / month "
            f"at ${savings['storage_gb_month_usd']:.2f} per GB-month (assumption)",
            f"- **Net: ${savings['monthly_net_savings_usd']:,.2f} / month**", "",
            "Simple model: one busy vCPU per running query; ignores I/O pricing, "
            "parallel workers, cache effects and index write overhead.", "",
        ]
    else:
        md += ["Not estimated — dollar savings are only computed from measured "
               "runtimes. Approve with real validation enabled to get an estimate.", ""]

    if outcome == "approved":
        md += ["## Migration", "", "```sql", migration_sql.strip(), "```", "",
               "## Rollback", "", "```sql", rollback_sql.strip(), "```", ""]

    return {
        "outcome": outcome,
        "index_names": names,
        "migration_sql": migration_sql,
        "rollback_sql": rollback_sql,
        "actual": actual,
        "savings": savings,
        "markdown": "\n".join(md),
    }
