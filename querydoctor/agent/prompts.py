"""
Prompts for the LLM nodes. Every number shown to the LLM comes from
PostgreSQL / HypoPG; the LLM is told never to invent numbers.
"""

import json


DIAGNOSE_SYSTEM = """\
You are a senior PostgreSQL performance engineer explaining a slow query to a \
developer who is not a database expert.

Use ONLY the facts in the plan summary and table information you are given. \
Do not invent numbers, timings or percentages; if you mention a number it must \
appear in the input. Write 2-3 plain sentences naming the main bottleneck \
(which table is scanned, which filter/join/sort forces the expensive work) \
and what kind of index could help. No markdown headings, no lists."""


PROPOSE_SYSTEM = """\
You are a senior PostgreSQL 16 performance engineer. Propose indexes that \
reduce the planner cost of ONE query. Each candidate will be validated \
automatically with HypoPG hypothetical indexes and plain EXPLAIN, so the \
measured results — not your opinion — decide what is accepted.

Rules:
- Propose 1 to 3 candidates, best first. Each candidate is exactly ONE \
`CREATE INDEX ON <table> (...)` statement. Each candidate is evaluated on its own.
- Never use CONCURRENTLY, UNIQUE, IF NOT EXISTS, or any statement other than \
CREATE INDEX. You may omit the index name.
- Use only tables and columns listed in the table information. At most 4 key \
columns; at most 4 INCLUDE columns. Allowed methods: btree (default), hash, brin.
- Target the expensive parts of the plan: Seq Scans on large tables, selective \
filters, join keys and sort/group keys. Put equality-filtered columns before \
range-filtered columns in composite indexes.
- Consider covering indexes (INCLUDE the other columns the query reads from that \
table) so PostgreSQL can use an index-only scan instead of visiting the heap. \
INCLUDE only columns this query actually references — never "all columns".
- Use a partial index (WHERE ...) only for predicates that are constant across \
executions (e.g. comparing two columns, or a fixed status). Do NOT hard-code the \
query's specific date/number literals in a partial index — those parameters \
change between calls.
- Do not duplicate an existing index.
- Do not repeat an index that was already tested (see previous attempts) unless \
you change it meaningfully (different columns, column order, or INCLUDE list), \
and explain what changed. Learn from the measured results: an index that was \
not used, or reduced cost too little, needs a different design.
- Never state expected percentages or costs; you do not know them.
- Optionally suggest a semantically equivalent query rewrite in rewrite_sql \
(it is shown to the human as an unvalidated suggestion only); otherwise leave it null."""


CRITIC_SYSTEM = """\
You are reviewing measured results of hypothetical-index experiments for a \
PostgreSQL query. The verdict has already been decided by deterministic rules: \
none of the candidates reached the required planner-cost reduction while being \
used by the planner. Write concrete feedback (3-5 sentences) for the engineer \
who will propose the next round of indexes.

Use only the numbers given, and state each candidate's "used by planner" \
status exactly as given — never contradict it. Say why the candidates fell \
short, using the evidence (index not used; plan still has a Seq Scan or heap \
access; reduction too small) and suggest a specific different direction, for \
example a different leading column, adding INCLUDE columns to allow an \
index-only scan, or targeting a different table or join in the plan.

Constraints the next proposal must respect: one CREATE INDEX per candidate, at \
most 4 key columns, at most 4 INCLUDE columns (only columns the query reads), \
only existing columns. Never suggest an index that was already tested. Do not \
write SQL statements; do not invent numbers."""


def _json(data) -> str:
    return json.dumps(data, indent=1, default=str)


def format_table_info(table_info: list[dict]) -> str:
    """Compact schema text: columns, row estimate, size, existing indexes."""

    lines = []

    for t in table_info:

        lines.append(
            f"Table {t['table']} (~{t['estimated_rows']:,} rows, {t['size']})"
        )
        lines.append(
            "  columns: " + ", ".join(
                f"{c['name']} {c['type']}" for c in t["columns"]
            )
        )
        for idx in t["indexes"]:
            lines.append(f"  existing index: {idx['definition']}")

    return "\n".join(lines)


def format_history(history: list[dict]) -> str:

    if not history:
        return "None yet."

    lines = []

    for attempt in history:

        lines.append(f"Iteration {attempt['iteration']}:")

        for c in attempt["candidates"]:
            if not c.get("valid", True):
                lines.append(f"  - {c['sql']}  -> REJECTED: {c['error']}")
            else:
                lines.append(
                    f"  - {c['sql']}  -> planner cost reduction "
                    f"{c['reduction_pct']:.1f}%, "
                    f"{'used' if c['index_used'] else 'NOT used'} by planner"
                )

        if attempt.get("combined") and len(attempt["combined"]["sqls"]) > 1:
            lines.append(
                f"  all valid candidates together: "
                f"{attempt['combined']['reduction_pct']:.1f}%"
            )

        if attempt.get("feedback"):
            lines.append(f"  critic feedback: {attempt['feedback']}")

        if attempt.get("human_feedback"):
            lines.append(f"  human reviewer feedback: {attempt['human_feedback']}")

    return "\n".join(lines)


def diagnose_messages(state: dict) -> list:

    user = f"""Query:
{state['sql']}

Plan summary (from EXPLAIN, estimated planner numbers):
{_json(state['plan_summary'])}

Tables:
{format_table_info(state['table_info'])}"""

    return [("system", DIAGNOSE_SYSTEM), ("human", user)]


def propose_messages(state: dict) -> list:

    threshold_pct = state["threshold"] * 100

    feedback = ""
    if state.get("human_feedback"):
        feedback = (
            "\n\nThe human reviewer rejected the previous recommendation "
            f"with this feedback (take it seriously):\n{state['human_feedback']}"
        )

    user = f"""Query to optimize:
{state['sql']}

Plan summary (from EXPLAIN with current indexes; costs are planner estimates):
{_json(state['plan_summary'])}

Bottleneck diagnosis:
{state.get('diagnosis', '')}

Tables and existing indexes:
{format_table_info(state['table_info'])}

Previous attempts with measured HypoPG results:
{format_history(state.get('history', []))}{feedback}

Acceptance rule: a candidate is accepted only if the planner uses it and the \
estimated planner cost drops by at least {threshold_pct:.0f}%.
This is iteration {state['iteration']} of {state['max_iterations']}."""

    return [("system", PROPOSE_SYSTEM), ("human", user)]


def critic_messages(state: dict, results: list[dict]) -> list:

    threshold_pct = state["threshold"] * 100

    rows = []
    for r in results:
        if not r["valid"]:
            rows.append(f"- {r['sql']}: rejected before testing ({r['error']})")
        else:
            rows.append(
                f"- {r['sql']}: {r['reduction_pct']:.1f}% planner cost "
                f"reduction; used by planner: {'YES' if r['index_used'] else 'NO'}"
            )

    best = state.get("best")
    best_plan = _json(best["new_summary"]) if best and best.get("new_summary") else "n/a"

    user = f"""Query:
{state['sql']}

Baseline plan summary:
{_json(state['plan_summary'])}

Results this iteration (required: >= {threshold_pct:.0f}% and used):
{chr(10).join(rows)}

Plan with the best candidate:
{best_plan}

Earlier attempts:
{format_history(state.get('history', []))}"""

    return [("system", CRITIC_SYSTEM), ("human", user)]
