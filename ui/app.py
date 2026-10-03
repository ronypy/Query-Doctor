"""
QueryDoctor Streamlit dashboard. Runs the LangGraph agent in-process.

    .venv/bin/streamlit run ui/app.py
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st
from langgraph.types import Command

from querydoctor.agent.graph import (
    build_graph,
    get_checkpointer,
    initial_state,
    run_config,
)
from querydoctor.config import get_settings
from querydoctor.db import get_conn
from querydoctor.tools.slow_queries import get_slow_queries


st.set_page_config(page_title="QueryDoctor", page_icon="🩺", layout="wide")

settings = get_settings()

NODE_ICONS = {
    "fetch_context": "🔎",
    "diagnose": "🩺",
    "propose": "💡",
    "safety_check": "🛡️",
    "validate": "🧪",
    "critic": "⚖️",
    "human_approval": "🙋",
    "report": "📄",
}

# Status palette (dataviz reference): fixed roles, always paired with a label.
STATUS_COLORS = {
    "Baseline (current indexes)": "#8a8984",
    "Meets threshold": "#0ca30c",
    "Below threshold": "#fab219",
    "Not used by planner": "#c3c2b7",
}


# ---------------------------------------------------------------------------
# Cached resources
# ---------------------------------------------------------------------------

@st.cache_resource
def get_graph():
    return build_graph(get_checkpointer("sqlite"))


@st.cache_data(ttl=15, show_spinner=False)
def db_status() -> dict:
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version")
                version = cur.fetchone()[0]
                cur.execute(
                    "SELECT extname, extversion FROM pg_extension "
                    "WHERE extname IN ('pg_stat_statements', 'hypopg')"
                )
                extensions = dict(cur.fetchall())
        finally:
            conn.close()
        return {"ok": True, "version": version, "extensions": extensions}
    except Exception as e:
        return {"ok": False, "error": str(e).split("\n")[0]}


@st.cache_data(ttl=30, show_spinner=False)
def slow_queries() -> list[dict]:
    return get_slow_queries(limit=10, only_mapped=True)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def current_config():
    thread_id = st.session_state.get("thread_id")
    return run_config(thread_id) if thread_id else None


def pending_interrupt(graph, config):
    tasks = graph.get_state(config).tasks
    interrupts = [i for t in tasks for i in t.interrupts]
    return interrupts[0].value if interrupts else None


def run_stream(graph, payload, config, label: str):
    """Stream graph updates into a live status box, then rerun to render."""

    failed = False

    with st.status(label, expanded=True) as status:
        try:
            for update in graph.stream(payload, config, stream_mode="updates"):
                for node, value in update.items():
                    if node == "__interrupt__":
                        status.update(label="Waiting for your approval")
                        continue
                    for entry in (value or {}).get("trace", []):
                        icon = NODE_ICONS.get(entry["node"], "•")
                        status.write(f"{icon} **{entry['node']}** — {entry['message']}")
            status.update(state="complete", expanded=False)
        except Exception as e:
            failed = True
            status.update(label="The agent hit an error", state="error")
            st.session_state["run_error"] = f"{type(e).__name__}: {str(e).splitlines()[0]}"

    if not failed:
        st.session_state.pop("run_error", None)
    st.rerun()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def fmt_mb(size_bytes) -> str:
    return f"{size_bytes / 1024 / 1024:,.0f} MB" if size_bytes else "–"


def candidate_status(c: dict, threshold_pct: float) -> str:
    if not c.get("valid", True):
        return "Rejected by safety check"
    if not c.get("index_used"):
        return "Not used by planner"
    if c["reduction_pct"] >= threshold_pct:
        return "Meets threshold"
    return "Below threshold"


def short_index(sql: str) -> str:
    return sql.replace("CREATE INDEX ON ", "").replace("CREATE INDEX ", "")


def render_cost_chart(values: dict):

    threshold = values["threshold"]
    threshold_pct = threshold * 100
    baseline = values["plan_summary"]["total_cost"]

    rows = [{
        "label": "Baseline",
        "index": "current indexes only",
        "cost": baseline,
        "reduction": 0.0,
        "status": "Baseline (current indexes)",
    }]

    for attempt in values.get("history", []):
        for n, c in enumerate(attempt["candidates"], start=1):
            if not c.get("valid", True) or c.get("reduction_pct") is None:
                continue
            new_cost = c.get("new_cost")
            if new_cost is None:   # older checkpoints without new_cost
                new_cost = baseline * (1 - c["reduction_pct"] / 100)
            rows.append({
                "label": f"Iter {attempt['iteration']} · #{n}",
                "index": short_index(c["sql"]),
                "cost": new_cost,
                "reduction": c["reduction_pct"],
                "status": candidate_status(c, threshold_pct),
            })

    if len(rows) == 1:
        st.caption("No validated candidates yet.")
        return

    df = pd.DataFrame(rows)
    order = list(df["label"])
    statuses = [s for s in STATUS_COLORS if s in set(df["status"])]

    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, height={"band": 0.6}).encode(
        y=alt.Y("label:N", sort=order, title=None),
        x=alt.X("cost:Q", title="Estimated planner cost (lower is better)"),
        color=alt.Color(
            "status:N",
            scale=alt.Scale(domain=statuses,
                            range=[STATUS_COLORS[s] for s in statuses]),
            legend=alt.Legend(title=None, orient="top"),
        ),
        tooltip=[
            alt.Tooltip("index:N", title="Index"),
            alt.Tooltip("cost:Q", title="Est. planner cost", format=",.0f"),
            alt.Tooltip("reduction:Q", title="Est. reduction %", format=".1f"),
            alt.Tooltip("status:N", title="Status"),
        ],
    )

    accept_line = baseline * (1 - threshold)
    rule = alt.Chart(pd.DataFrame({"x": [accept_line]})).mark_rule(
        strokeDash=[4, 4], strokeWidth=2, color="#52514e"
    ).encode(x="x:Q")
    rule_label = alt.Chart(pd.DataFrame({
        "x": [accept_line],
        "text": [f"needed for ≥{threshold_pct:.0f}%"],
    })).mark_text(align="left", dx=4, dy=-6, color="#52514e").encode(
        x="x:Q", y=alt.value(0), text="text:N"
    )

    st.altair_chart(
        (bars + rule + rule_label).properties(height=max(140, 42 * len(rows))),
        width="stretch",
    )
    st.caption(
        "Costs are PostgreSQL planner estimates with HypoPG hypothetical "
        "indexes — not measured runtimes."
    )


def render_attempts_table(values: dict):

    threshold_pct = values["threshold"] * 100
    rows = []

    for attempt in values.get("history", []):
        for c in attempt["candidates"]:
            valid = c.get("valid", True)
            rows.append({
                "Iteration": attempt["iteration"],
                "Candidate index": c["sql"],
                "Est. cost reduction": (
                    f"{c['reduction_pct']:.1f}%" if valid and c.get("reduction_pct") is not None else "–"
                ),
                "Used by planner": ("yes" if c.get("index_used") else "no") if valid else "–",
                "Est. size": fmt_mb(c.get("est_size_bytes")),
                "Status": candidate_status(c, threshold_pct)
                          + ("" if valid else f": {c['error']}"),
                "Critic": attempt["verdict"],
            })

    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    for attempt in values.get("history", []):
        if attempt.get("feedback"):
            with st.expander(f"Critic feedback after iteration {attempt['iteration']}"):
                st.write(attempt["feedback"])


def render_approval(graph, config, payload: dict):

    best = payload["best"]

    with st.container(border=True):
        st.subheader("🙋 Approve this index recommendation?")
        st.code(best["sql"], language="sql")

        c1, c2, c3 = st.columns(3)
        c1.metric("Est. planner cost", f"{best['new_cost']:,.0f}",
                  delta=f"-{best['reduction_pct']:.1f}%", delta_color="inverse")
        c2.metric("Baseline planner cost", f"{best['baseline_cost']:,.0f}")
        c3.metric("Est. index size", fmt_mb(best.get("est_size_bytes")))

        if best.get("rationale"):
            st.caption(f"Rationale: {best['rationale']}")

        feedback = st.text_area(
            "Feedback for the agent (used if you reject)",
            placeholder="e.g. Prefer a smaller index without INCLUDE columns",
            key=f"feedback_{st.session_state['thread_id']}",
        )

        a, r, _ = st.columns([1, 1, 3])
        if a.button("✅ Approve", type="primary", key="approve"):
            run_stream(graph, Command(resume={"approved": True}), config,
                       "Generating report…")
        if r.button("❌ Reject", key="reject"):
            run_stream(
                graph,
                Command(resume={"approved": False, "feedback": feedback or None}),
                config,
                "Re-proposing with your feedback…" if feedback else "Finishing…",
            )


def render_report(report: dict, values: dict):

    name = values.get("query_name") or "query"

    d1, d2, d3, _ = st.columns([1, 1, 1, 2])
    d1.download_button("⬇ report.md", report["markdown"],
                       file_name=f"querydoctor_{name}_report.md")
    if report["migration_sql"]:
        d2.download_button("⬇ migration.sql", report["migration_sql"],
                           file_name=f"querydoctor_{name}_migration.sql")
        d3.download_button("⬇ rollback.sql", report["rollback_sql"],
                           file_name=f"querydoctor_{name}_rollback.sql")

        m, rb = st.columns(2)
        with m:
            st.markdown("**Migration SQL**")
            st.code(report["migration_sql"], language="sql")
        with rb:
            st.markdown("**Rollback SQL**")
            st.code(report["rollback_sql"], language="sql")

    with st.container(border=True):
        st.markdown(report["markdown"])


def render_run(graph, config):

    snapshot = graph.get_state(config)
    values = snapshot.values

    if not values:
        return

    label = (values.get("query_name") or values.get("query_key") or "").upper()
    st.header(f"Diagnosis — {label}")

    if st.session_state.get("run_error"):
        st.error(
            f"The agent stopped with an error: {st.session_state['run_error']}. "
            "Check the database / GROQ_API_KEY and try Diagnose again."
        )

    stats = values.get("stats") or {}
    summary = values.get("plan_summary") or {}
    best = values.get("best")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Measured mean runtime",
              f"{stats['mean_exec_time']:,.0f} ms" if stats else "–",
              help="From pg_stat_statements (measured).")
    m2.metric("Calls", f"{stats['calls']:,}" if stats else "–")
    m3.metric("Baseline planner cost",
              f"{summary['total_cost']:,.0f}" if summary else "–",
              help="PostgreSQL EXPLAIN estimate with current indexes.")
    m4.metric("Best est. cost reduction",
              f"{best['reduction_pct']:.1f}%" if best else "–",
              help="HypoPG hypothetical index, planner estimate — not runtime.")

    # Bottleneck
    if values.get("diagnosis"):
        with st.container(border=True):
            st.markdown("**🩺 Bottleneck**")
            st.markdown(values["diagnosis"])
            scans = [s for s in summary.get("seq_scans", []) if s.get("large_table")]
            for s in scans:
                filt = f" — filter `{s['filter']}`" if s.get("filter") else ""
                st.caption(f"Seq Scan on **{s['table']}** "
                           f"(~{s.get('table_rows', 0):,} rows){filt}")

    # Outcome / approval
    payload = pending_interrupt(graph, config)
    report = values.get("report")

    if payload:
        render_approval(graph, config, payload)
    elif report:
        if report["outcome"] == "approved":
            st.success(f"✅ Approved — migration creates `{report['index_name']}`.")
        elif report["outcome"] == "rejected":
            st.warning("❌ Recommendation rejected by the reviewer — no migration generated.")
        else:
            st.info(f"⚖️ No recommendation: {values['critique']['feedback']}")

    tab_trace, tab_results, tab_report = st.tabs(
        ["Agent trace", "Candidates & validation", "Report"]
    )

    with tab_trace:
        for entry in values.get("trace", []):
            icon = NODE_ICONS.get(entry["node"], "•")
            st.markdown(f"{icon} **{entry['node']}** — {entry['message']}")

    with tab_results:
        render_cost_chart(values)
        render_attempts_table(values)
        proposal = values.get("proposal") or {}
        if proposal.get("rewrite_sql"):
            with st.expander("Suggested query rewrite (not validated)"):
                st.code(proposal["rewrite_sql"], language="sql")
                if proposal.get("rewrite_rationale"):
                    st.caption(proposal["rewrite_rationale"])

    with tab_report:
        if report:
            render_report(report, values)
        else:
            st.caption("The report appears after approval, rejection, or give-up.")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

graph = get_graph()

with st.sidebar:
    st.header("Settings")
    threshold = st.slider(
        "Required est. planner cost reduction", 0.05, 0.95,
        float(settings.improvement_threshold), 0.05, format="%.2f",
        help="Critic accepts an index only if the planner uses it and the "
             "estimated cost drops at least this much. Applies to new runs.",
    )
    max_iterations = st.slider("Max proposal iterations", 1, 5,
                               int(settings.max_iterations))
    st.caption(f"LLM: {settings.llm_provider} · `{settings.llm_model}`")
    if st.button("Start over"):
        st.session_state.pop("thread_id", None)
        st.session_state.pop("run_error", None)
        st.rerun()
    st.divider()
    st.caption(
        "Safety: read-only DB role · sqlglot allow-list (single CREATE INDEX) · "
        "numbers come from PostgreSQL, never the LLM · human approval required."
    )

st.title("🩺 QueryDoctor")
st.caption("AI database performance engineer — finds slow queries, proves which "
           "indexes help with hypothetical indexes, and asks before changing anything.")

status = db_status()
if status["ok"]:
    ext = status["extensions"]
    st.success(
        f"Connected · PostgreSQL {status['version']} · "
        f"pg_stat_statements {ext.get('pg_stat_statements', 'missing')} · "
        f"hypopg {ext.get('hypopg', 'missing')}"
    )
else:
    st.error(f"Database unavailable: {status['error']} — is `docker compose up -d` running?")
    st.stop()

st.subheader("Slowest queries (pg_stat_statements)")

queries = slow_queries()

if not queries:
    st.warning("No workload queries recorded yet. Run `python scripts/run_workload.py`.")
    st.stop()

table = pd.DataFrame([{
    "Query": q["name"].upper(),
    "Mean (ms)": round(q["mean_exec_time"]),
    "Calls": q["calls"],
    "Total (ms)": round(q["total_exec_time"]),
    "Normalized SQL": " ".join(q["query"].split())[:120],
} for q in queries])

selection = st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    on_select="rerun",
    selection_mode="single-row",
    key="slow_queries",
    column_config={
        "Mean (ms)": st.column_config.NumberColumn(format="%d"),
        "Total (ms)": st.column_config.NumberColumn(format="%d"),
    },
)

rows = selection.selection.rows if selection else []
selected = queries[rows[0]] if rows else queries[0]

c1, c2 = st.columns([1, 4])
if c1.button(f"🩺 Diagnose {selected['name'].upper()}", type="primary", key="diagnose"):
    st.session_state["thread_id"] = uuid.uuid4().hex[:12]
    st.session_state.pop("run_error", None)
    run_stream(
        graph,
        initial_state(selected["name"], threshold, max_iterations),
        current_config(),
        f"Diagnosing {selected['name'].upper()}…",
    )
c2.caption("Select a row to choose a query. Runtimes are measured; costs below are planner estimates.")

config = current_config()
if config:
    st.divider()
    render_run(graph, config)
