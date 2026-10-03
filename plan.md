# QueryDoctor — Build Plan

> An autonomous PostgreSQL performance agent built with LangGraph. It finds slow queries, reads their execution plans, proposes indexes and query rewrites, **validates them safely with hypothetical indexes (HypoPG)**, critiques its own results, and asks a human for approval before generating a migration script and a cost-savings report.

This file is written to be handed to Claude Code. Work through the phases in order; every phase ends with a checkpoint you can verify before moving on.

---

## 0. Goals, scope and demo story

### One-line pitch
"QueryDoctor is an AI database engineer that finds your slow queries, proves which fixes work without touching production, and asks before it changes anything."

### Must-have (MVP — demo depends on these)
1. Connect to a PostgreSQL database and list the slowest queries from `pg_stat_statements`.
2. For a chosen query: run `EXPLAIN (FORMAT JSON)` and summarize the bottlenecks.
3. LLM proposes 1–3 candidate indexes (and optionally a rewrite).
4. Validate each candidate with HypoPG: compare plan cost before vs. after.
5. Critic node: if improvement < threshold, loop back and propose again (max 3 iterations).
6. Human-in-the-loop approval with LangGraph `interrupt()`.
7. Output: migration SQL (`CREATE INDEX CONCURRENTLY ...`), rollback SQL, and a report with estimated speed-up.
8. Web dashboard that shows the agent's step-by-step trace and the before/after numbers.

### Nice-to-have (only after MVP works end to end)
- **Real validation**: apply the winning index on a scratch copy and run `EXPLAIN ANALYZE` to get actual latency.
- **Workload mode**: optimize the top N queries together and detect indexes that help several queries (and penalize redundant indexes / write overhead).
- **Index hygiene**: detect unused and duplicate indexes from `pg_stat_user_indexes`.
- **MCP server**: expose the tools so Claude Desktop / Claude Code / Cursor can call QueryDoctor.
- **Monthly savings estimate** in dollars (simple model: CPU-seconds saved × calls/day × price).
- Sponsor-track integration (check the hackathon prize list — e.g., store run history in a sponsor database, voice summary, auth).

### Out of scope
Applying changes to a real production database. The tool only *generates* migration SQL; humans run it.

### Demo story (3 minutes)
1. Dashboard shows the top 5 slow TPC-H queries (e.g., Q3 at 4+ seconds).
2. Click "Diagnose" on one. The trace streams: *reading plan → found Seq Scan on lineitem (6M rows) → proposing index → validating with hypothetical index → cost dropped 92% → critic approves*.
3. Agent pauses: "Approve this index?" Click Approve.
4. Show the generated migration and rollback SQL + report: "est. 4.2 s → 0.15 s, 28× faster".
5. (If done) Apply on scratch DB, show real `EXPLAIN ANALYZE` confirming the speed-up.

---

## 1. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | LangGraph ecosystem |
| Package manager | `uv` (or pip + venv) | fast setup |
| Agent framework | `langgraph`, `langchain-core` | stateful graph, loops, interrupts, checkpoints |
| LLM | `langchain-anthropic` (Claude) or `langchain-openai` / `langchain-google-genai` | make it configurable via env var — pick based on sponsor prizes/credits |
| Structured output | `pydantic` v2 + `.with_structured_output()` | reliable index proposals |
| Database | PostgreSQL 16 in Docker | |
| Extensions | `pg_stat_statements`, `hypopg` | slow-query stats, hypothetical indexes |
| DB driver | `psycopg[binary]` v3 | |
| SQL parsing / safety | `sqlglot` | validate that LLM-generated SQL is only `CREATE INDEX` |
| Benchmark data | TPC-H scale factor 1 generated with **DuckDB's `tpch` extension** | no need to compile `dbgen` |
| Checkpointer | `langgraph-checkpoint-sqlite` (`SqliteSaver`) — `MemorySaver` is fine for dev | needed for `interrupt()` / resume |
| Backend API | FastAPI + Server-Sent Events (SSE) | stream the trace to the UI |
| Frontend | **Streamlit** (fastest) — or Next.js if time allows | |
| Tracing (optional) | LangSmith | pretty trace screenshots for Devpost |
| Tests | `pytest` | |

---

## 2. Repository structure

```
querydoctor/
├── plan.md                      # this file
├── README.md                    # final project README (Phase 9)
├── .env.example                 # env var template
├── pyproject.toml               # dependencies (uv)
├── docker-compose.yml           # postgres + extensions
├── docker/
│   └── postgres/
│       ├── Dockerfile           # postgres:16 + hypopg
│       └── init.sql             # create extensions, read-only role
├── scripts/
│   ├── generate_tpch.py         # DuckDB → TPC-H parquet/CSV
│   ├── load_tpch.py             # load into Postgres (no secondary indexes!)
│   ├── run_workload.py          # run the 22 TPC-H queries to fill pg_stat_statements
│   └── reset_demo.py            # drop added indexes, reset stats, hypopg_reset()
├── workload/
│   └── tpch_queries/            # q01.sql … q22.sql (parameters filled in)
├── querydoctor/
│   ├── __init__.py
│   ├── config.py                # settings from env (pydantic-settings)
│   ├── db.py                    # connection helpers, statement_timeout, read-only
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── slow_queries.py      # get_slow_queries()
│   │   ├── explain.py           # explain_query(), summarize_plan()
│   │   ├── schema.py            # get_table_info(): columns, row counts, existing indexes
│   │   ├── hypopg.py            # create_hypothetical_index(), reset_hypothetical()
│   │   ├── validate.py          # compare_costs(), real_validate() (nice-to-have)
│   │   └── safety.py            # validate_index_sql() using sqlglot
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── state.py             # AgentState TypedDict + Pydantic models
│   │   ├── prompts.py           # system/user prompts for each LLM node
│   │   ├── llm.py               # get_llm() factory based on env
│   │   ├── nodes.py             # node functions
│   │   └── graph.py             # StateGraph wiring + checkpointer
│   ├── report.py                # migration SQL, rollback SQL, markdown report
│   ├── api.py                   # FastAPI app: start run, stream events, resume
│   └── mcp_server.py            # (nice-to-have) MCP tools
├── ui/
│   └── app.py                   # Streamlit dashboard
└── tests/
    ├── test_tools.py
    ├── test_safety.py
    └── test_graph.py
```

---

## 3. Phase 1 — Environment and database (≈1.5 h)

### 3.1 Python project
```bash
uv init querydoctor && cd querydoctor
uv add langgraph langchain-core langchain-anthropic pydantic pydantic-settings \
       "psycopg[binary]" sqlglot duckdb fastapi uvicorn sse-starlette streamlit \
       langgraph-checkpoint-sqlite python-dotenv httpx
uv add --dev pytest
```

### 3.2 `.env.example`
```
LLM_PROVIDER=anthropic            # anthropic | openai | google
LLM_MODEL=                        # set to the model you want to use
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
DATABASE_URL=postgresql://qd_agent:qd_agent@localhost:5433/tpch
ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/tpch
IMPROVEMENT_THRESHOLD=0.30        # critic requires >=30% cost reduction
MAX_ITERATIONS=3
STATEMENT_TIMEOUT_MS=30000
LANGSMITH_API_KEY=                # optional
```

### 3.3 `docker/postgres/Dockerfile`
```dockerfile
FROM postgres:16
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-hypopg \
 && rm -rf /var/lib/apt/lists/*
```
(The official image ships the PGDG apt repo, so `postgresql-16-hypopg` is installable.)

### 3.4 `docker-compose.yml`
```yaml
services:
  db:
    build: ./docker/postgres
    environment:
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: tpch
    command: >
      postgres
      -c shared_preload_libraries=pg_stat_statements
      -c pg_stat_statements.track=all
      -c shared_buffers=512MB
      -c work_mem=16MB
    ports: ["5433:5432"]
    volumes:
      - ./docker/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```
Port 5433 avoids clashing with any local Postgres.

### 3.5 `docker/postgres/init.sql`
```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;

-- Agent role: can read data and stats, cannot write tables.
CREATE ROLE qd_agent LOGIN PASSWORD 'qd_agent';
GRANT CONNECT ON DATABASE tpch TO qd_agent;
GRANT USAGE ON SCHEMA public TO qd_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO qd_agent;
GRANT pg_read_all_stats TO qd_agent;
```
Note: HypoPG's functions are usable by non-superusers in the session that creates them; hypothetical indexes live only in that backend session. If permission issues appear, run HypoPG calls with the admin URL inside a dedicated connection — but keep the LLM-facing tools restricted (see safety).

### 3.6 Generate and load TPC-H
`scripts/generate_tpch.py`
- `duckdb.connect()` → `INSTALL tpch; LOAD tpch; CALL dbgen(sf=1);`
- Export each of the 8 tables (`region, nation, part, supplier, partsupp, customer, orders, lineitem`) to `data/<table>.csv` (or parquet).
- Also export the 22 queries: `SELECT query_nr, query FROM tpch_queries();` → write `workload/tpch_queries/qNN.sql`.
  - Check each query runs on Postgres; fix any DuckDB-specific syntax (e.g., interval literals) by hand. If a query is troublesome, drop it — 8–10 good queries are enough for the demo.

`scripts/load_tpch.py`
- Create tables with **primary keys only** (no secondary indexes — we want the agent to find them).
- `COPY ... FROM STDIN` via psycopg's `cursor.copy()`.
- Run `ANALYZE;` at the end (important: planner statistics).

`scripts/run_workload.py`
- `SELECT pg_stat_statements_reset();`
- Run each query 3 times with a timeout; print timings.

`scripts/reset_demo.py`
- Drop every index not backing a PK/unique constraint, `SELECT hypopg_reset();`, reset stats, rerun workload.

### ✅ Checkpoint 1
```sql
SELECT round(mean_exec_time) AS ms, calls, left(query, 80)
FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 5;
```
shows several queries taking ≥1 s, and
```sql
SELECT * FROM hypopg_create_index('CREATE INDEX ON lineitem (l_shipdate)');
```
returns an `indexrelid` and name.

---

## 4. Phase 2 — Tools as plain Python (≈2 h)

Write and test these **without any LLM**. Every function returns plain dicts/Pydantic models (JSON-serializable so they fit in graph state).

### `querydoctor/db.py`
- `get_conn(admin: bool = False)` — psycopg connection; sets `statement_timeout` and `SET default_transaction_read_only = on` for the agent connection.
- Use **one dedicated connection per agent run for HypoPG work**, because hypothetical indexes exist only inside that session. Store a `run_id → connection` map in a small session manager (not in graph state — connections aren't serializable).

### `tools/slow_queries.py`
`get_slow_queries(limit=10) -> list[SlowQuery]`
```sql
SELECT queryid, query, calls, mean_exec_time, total_exec_time, rows
FROM pg_stat_statements
WHERE query NOT ILIKE '%pg_stat_statements%'
  AND query ILIKE 'select%'
ORDER BY total_exec_time DESC
LIMIT %s;
```
Note: `pg_stat_statements` normalizes literals into `$1, $2`. For the demo, map each `queryid` back to the original text in `workload/tpch_queries/` (match by running each file once and recording queryid), so EXPLAIN gets concrete parameters. Store the mapping in `data/query_map.json`.

### `tools/explain.py`
- `explain_query(sql, analyze=False) -> dict` — `EXPLAIN (FORMAT JSON[, ANALYZE, BUFFERS]) <sql>`; return the plan JSON and `Total Cost`.
- `summarize_plan(plan_json) -> PlanSummary` — deterministic walk of the plan tree that extracts: total cost, node types, **Seq Scans on large tables** (table, rows, filter condition), sorts, hash joins with big inputs, and the columns appearing in `Filter`, `Join Filter`, `Hash Cond`, `Sort Key`. This compact summary goes to the LLM instead of raw JSON (saves tokens and improves accuracy).

### `tools/schema.py`
`get_table_info(tables: list[str]) -> list[TableInfo]` — columns + types (`information_schema.columns`), estimated row counts (`pg_class.reltuples`), existing indexes (`pg_indexes`), size (`pg_total_relation_size`).

### `tools/safety.py`
`validate_index_sql(sql) -> (ok: bool, reason: str)`
- Parse with `sqlglot.parse_one(sql, read="postgres")`.
- Allow only a single `CREATE INDEX` statement; reject `DROP`, `ALTER`, `UPDATE`, `DELETE`, multiple statements, semicolons injecting extra commands.
- Check referenced table and columns exist (from `schema.py`).
- Reject indexes with more than 4 columns.

### `tools/hypopg.py`
- `create_hypothetical_index(conn, index_sql) -> {"indexrelid", "indexname"}` — calls `SELECT * FROM hypopg_create_index(%s)` **only after** `validate_index_sql` passes.
- `hypothetical_index_size(conn, indexrelid)` — `hypopg_relation_size()` (for the report).
- `reset_hypothetical(conn)` — `SELECT hypopg_reset();`

### `tools/validate.py`
`compare_costs(conn, sql, index_sqls) -> ValidationResult`
1. `hypopg_reset()`; baseline = `EXPLAIN (FORMAT JSON)` cost.
2. Create the candidate hypothetical index(es).
3. New plan = `EXPLAIN (FORMAT JSON)` (**plain EXPLAIN — HypoPG does not work with ANALYZE**).
4. Check whether the new plan actually **uses** the hypothetical index (search plan for its name).
5. Return: baseline cost, new cost, reduction %, index used (bool), new plan summary.
6. Also evaluate each candidate **individually** so you can drop candidates that don't help.

`real_validate(admin_conn, sql, index_sql)` *(nice-to-have)* — on a scratch DB or inside a session: `CREATE INDEX`, `EXPLAIN ANALYZE` 3 runs, take median, `DROP INDEX`. Only ever on the local demo database.

### ✅ Checkpoint 2 — `tests/test_tools.py`
- `get_slow_queries()` returns ≥3 queries.
- `summarize_plan()` finds a Seq Scan on `lineitem` for TPC-H Q6 (or similar).
- `compare_costs(q6, ["CREATE INDEX ON lineitem (l_shipdate)"])` shows a lower cost and `index_used=True`.
- `validate_index_sql("DROP TABLE lineitem")` → rejected.

---

## 5. Phase 3 — Agent state and LLM layer (≈1 h)

### `agent/state.py`
```python
from typing import TypedDict, Annotated, Literal
from operator import add
from pydantic import BaseModel, Field

class IndexCandidate(BaseModel):
    sql: str = Field(description="A single CREATE INDEX statement")
    rationale: str
    targets: list[str] = Field(description="Plan bottlenecks this index addresses")

class Proposal(BaseModel):
    candidates: list[IndexCandidate] = Field(max_length=3)
    rewrite_sql: str | None = None
    rewrite_rationale: str | None = None

class CandidateResult(BaseModel):
    sql: str
    baseline_cost: float
    new_cost: float
    reduction_pct: float
    index_used: bool
    est_size_bytes: int | None = None
    valid: bool = True
    error: str | None = None

class Critique(BaseModel):
    verdict: Literal["accept", "retry", "give_up"]
    feedback: str

class AgentState(TypedDict, total=False):
    run_id: str
    query_id: str
    sql: str
    plan_summary: dict
    table_info: list[dict]
    proposal: dict
    results: list[dict]
    best: dict | None
    critique: dict
    iteration: int
    history: Annotated[list[dict], add]       # all past attempts (fed back to proposer)
    trace: Annotated[list[dict], add]         # UI event log: {"node","message","data"}
    approved: bool | None
    human_feedback: str | None
    report: dict | None
```

### `agent/llm.py`
`get_llm()` — reads `LLM_PROVIDER` / `LLM_MODEL` and returns the chat model with `temperature=0`. Everything else uses `llm.with_structured_output(Proposal)` / `Critique`.

### `agent/prompts.py`
- **Diagnose prompt**: given plan summary + table info, explain the main bottleneck in 2–3 sentences for a non-expert (shown in the UI).
- **Propose prompt**: role = senior PostgreSQL performance engineer. Rules:
  - Propose at most 3 `CREATE INDEX` statements; no `CONCURRENTLY` here (HypoPG doesn't accept it — added later in the migration).
  - Prefer columns in selective filters, join keys, and sort keys; consider composite and covering (`INCLUDE`) indexes; consider partial indexes if the filter is constant.
  - Don't duplicate existing indexes (list them in the prompt).
  - Include `history` of previous attempts with their measured results and the critic's feedback, so it doesn't repeat failures.
- **Critic prompt**: given measured results (numbers come from tools, never from the LLM), threshold, index sizes and iteration count → `accept` / `retry` (with concrete feedback) / `give_up`.
  - Make the critic partly **deterministic**: if best reduction ≥ threshold and the index is used → accept without calling the LLM; if `iteration >= MAX_ITERATIONS` → give_up. Use the LLM only for the feedback text on retry. This makes the demo reliable.

---

## 6. Phase 4 — LangGraph workflow (≈2.5 h)

### Graph
```
START
  └─► fetch_context        (plan + summary + table info)        [tool]
        └─► diagnose        (plain-language bottleneck)          [LLM]
              └─► propose   (Proposal, structured output)        [LLM]
                    └─► safety_check (sqlglot filter)            [tool]
                          └─► validate (HypoPG cost compare)     [tool]
                                └─► critic                       [rules + LLM]
                                      ├─ retry   ─► propose  (loop, iteration+1)
                                      ├─ give_up ─► report (no-fix report)
                                      └─ accept  ─► human_approval   [interrupt]
                                                      ├─ approved ─► real_validate? ─► report ─► END
                                                      └─ rejected + feedback ─► propose
```

### `agent/nodes.py` — node responsibilities
| Node | Input | Output to state | Trace message example |
|---|---|---|---|
| `fetch_context` | `sql` | `plan_summary`, `table_info` | "Baseline cost 1,234,567. Seq Scan on lineitem (6.0M rows) filtering l_shipdate." |
| `diagnose` | summary | trace only | LLM explanation |
| `propose` | summary, tables, history, human_feedback | `proposal` | "Proposed 2 indexes: …" |
| `safety_check` | proposal | marks invalid candidates | "Rejected 1 candidate: references unknown column" |
| `validate` | valid candidates | `results`, `best`, appends to `history` | "lineitem(l_shipdate, l_discount): −91.4% cost, used ✓" |
| `critic` | results, iteration | `critique`, `iteration` | "Accepted: 91% reduction exceeds 30% threshold" |
| `human_approval` | best | `approved`, `human_feedback` | "Waiting for approval…" |
| `real_validate` (opt.) | best | adds actual ms to `best` | "Actual: 4,210 ms → 152 ms" |
| `report` | everything | `report` | "Report ready" |

### Human-in-the-loop (`human_approval` node)
```python
from langgraph.types import interrupt

def human_approval(state):
    decision = interrupt({
        "type": "approval",
        "best": state["best"],
        "message": "Approve this index recommendation?",
    })
    # decision = {"approved": bool, "feedback": str | None}
    return {
        "approved": decision["approved"],
        "human_feedback": decision.get("feedback"),
        "trace": [{"node": "human_approval",
                   "message": "Approved" if decision["approved"] else f"Rejected: {decision.get('feedback')}"}],
    }
```
Resume from the API with:
```python
from langgraph.types import Command
graph.invoke(Command(resume={"approved": True}), config={"configurable": {"thread_id": run_id}})
```

### `agent/graph.py`
- `StateGraph(AgentState)`, add nodes, `add_edge` for linear steps, `add_conditional_edges("critic", route_after_critic)` and `add_conditional_edges("human_approval", route_after_approval)`.
- Compile with a checkpointer: `SqliteSaver.from_conn_string("data/checkpoints.db")` (or `MemorySaver()` during development). Interrupts **require** a checkpointer and a `thread_id`.
- Guard against infinite loops: `iteration` cap + LangGraph's `recursion_limit` in the config.

### ✅ Checkpoint 3 — `tests/test_graph.py` / CLI
Add `python -m querydoctor.agent.graph --query q06` that runs the graph in the terminal, prints the trace, stops at the interrupt, asks `Approve? [y/n]`, then resumes and prints the report. **The project is "demo-able" once this works** — everything after is presentation.

---

## 7. Phase 5 — Report generation (≈45 min)

`querydoctor/report.py`
- **Migration SQL**: convert `CREATE INDEX ON t (...)` → `CREATE INDEX CONCURRENTLY IF NOT EXISTS qd_<table>_<cols> ON t (...);` with a header comment (query id, expected reduction, date).
- **Rollback SQL**: `DROP INDEX CONCURRENTLY IF EXISTS qd_<table>_<cols>;`
- **Markdown report**: problem query, bottleneck explanation, attempts table (iteration, index, cost reduction, used?), winner, estimated size, trade-offs (write overhead, storage), and — if real validation ran — actual latency before/after.
- **Savings estimate** (label clearly as an estimate): `time_saved_per_call × calls_per_day × 30`, optionally converted to a vCPU-hour cost assumption you state in the report.

---

## 8. Phase 6 — API and dashboard (≈3 h)

### Option A (fastest): Streamlit calls the graph directly
Skip FastAPI for the MVP; Streamlit runs the graph in-process using `graph.stream(..., stream_mode="updates")` and renders each update as it arrives. Keep the graph and a `thread_id` in `st.session_state`. **Recommended for a 36-hour hackathon.**

### Option B: FastAPI + any frontend
`querydoctor/api.py`
- `GET /queries` → slow queries list.
- `POST /runs {query_id}` → creates `run_id`, starts the graph in a background task.
- `GET /runs/{run_id}/events` → SSE stream of trace events (`stream_mode="updates"`); emits an `approval_required` event on interrupt.
- `POST /runs/{run_id}/resume {approved, feedback}` → `Command(resume=...)`.
- `GET /runs/{run_id}/report` → report JSON + SQL.

### `ui/app.py` (Streamlit) layout
1. **Header**: "QueryDoctor 🩺 — AI database performance engineer", DB connection status.
2. **Slow queries table**: rank, mean ms, calls, total time, short SQL; "Diagnose" button per row.
3. **Agent trace panel**: each node as an expandable step with an icon and status (use `st.status` / `st.chat_message`).
4. **Results chart**: bar chart baseline vs. new cost per candidate per iteration.
5. **Approval card**: proposed SQL in a code block, reduction %, size; Approve / Reject + feedback text box.
6. **Report tab**: rendered markdown, download buttons for `migration.sql`, `rollback.sql`, `report.md`.
7. **Sidebar**: threshold slider, max iterations, "Reset demo" button (calls `scripts/reset_demo.py` logic).

### ✅ Checkpoint 4
Full demo flow works in the browser for at least 3 different queries without errors.

---

## 9. Phase 7 — Nice-to-haves (pick by remaining time, in this order)

1. **Real validation** (big wow factor, ~1 h): actual `EXPLAIN ANALYZE` latency on the local demo DB after approval.
2. **Workload mode** (~2 h): loop over top-N queries, collect all candidates, evaluate each candidate's total benefit across queries (sum of `calls × cost reduction`), greedily select under a storage budget. This mirrors the classic index-selection problem from your research — mention that in the pitch.
3. **Index hygiene** (~45 min): report unused indexes (`pg_stat_user_indexes.idx_scan = 0`) and duplicates.
4. **MCP server** (~1 h): `mcp_server.py` using the official `mcp` Python SDK (FastMCP) exposing `get_slow_queries`, `explain_query`, `what_if_index`, `recommend_indexes` (runs the graph, auto-approval off). Demo: ask Claude Desktop "why is my lineitem query slow?".
5. **Sponsor tracks**: plug in whatever the hackathon offers (e.g., persist run history in a sponsor database, a voice summary of the report, auth on the dashboard, deploy on a sponsor cloud).

---

## 10. Phase 8 — Reliability and demo hardening (≈1.5 h — do not skip)

- **Pick 3 demo queries** that reliably show big wins (TPC-H Q6, Q3/Q10, Q12/Q14 are usually good candidates — verify yourself).
- `scripts/reset_demo.py` returns the DB to a clean state in < 30 s; practice the full demo after a reset at least 3 times.
- **Cache LLM responses** for the demo queries (a simple JSON cache keyed by prompt hash) with a `DEMO_MODE=1` switch, so a slow or failing API won't break the live demo.
- Timeouts on every DB call; friendly error messages in the UI.
- **Record a backup demo video** (2–3 min) as soon as Checkpoint 4 passes.
- Take LangSmith (or UI) trace screenshots for the Devpost page.

### Safety principles to mention in the pitch
- The agent uses a **read-only role**; it never executes DDL on production.
- All LLM-generated SQL passes a **sqlglot allow-list** (single `CREATE INDEX` only).
- All numbers in the report come from **the database's planner/executor, not from the LLM**.
- Changes require **explicit human approval**; output is a reviewable migration with rollback.

---

## 11. Phase 9 — Submission (≈1.5 h, start at least 3 h before the deadline)

### README.md
- Problem → solution → 30-second GIF → architecture diagram (the graph in §6; LangGraph can export Mermaid via `graph.get_graph().draw_mermaid()`) → quick start (`docker compose up`, `uv run python scripts/...`, `uv run streamlit run ui/app.py`) → safety design → results table → what's next.

### Devpost write-up sections
- **Inspiration**: index tuning is expensive expert work; tie to your PhD research on RL-based index selection.
- **What it does** / **How we built it** (LangGraph loop, HypoPG what-if analysis, human-in-the-loop, structured outputs).
- **Challenges**: HypoPG sessions, parameterized queries from `pg_stat_statements`, making LLM proposals reliable.
- **Accomplishments**: measured results table (e.g., "Q6: 92% cost reduction").
- **What's next**: workload-level optimization with RL, write-cost modeling, MySQL support, CI integration (comment on PRs that add slow queries).

### Pitch (2 minutes)
1. Problem + number (10 s) → 2. Live demo (80 s) → 3. Why it's trustworthy: safety design (15 s) → 4. What's next + your research background (15 s).

---

## 12. Timeline

| When | Phase | Checkpoint |
|---|---|---|
| Sat 11:30–13:00 | 1. Environment + TPC-H | slow queries visible, HypoPG works |
| Sat 13:00–15:00 | 2. Tools | `pytest tests/test_tools.py` passes |
| Sat 15:00–16:00 | 3. State + prompts + LLM | structured proposal returned for Q6 |
| Sat 16:00–19:00 | 4. Graph + interrupt | CLI end-to-end run with approval |
| Sat 19:00–20:00 | 5. Report | migration/rollback/report generated |
| Sat 20:00–23:00 | 6. Streamlit UI | browser demo works |
| Sun 08:30–11:00 | 7. Nice-to-haves (real validation first) | |
| Sun 11:00–12:30 | 8. Hardening + backup video | 3 clean demo rehearsals |
| Sun 12:30–submit | 9. README, Devpost, pitch | submitted early |

Adjust to the event's actual submission deadline. If you fall behind, cut from Phase 7 first, never Phase 8.

---

## 13. How to use this plan with Claude Code

Give Claude Code one phase at a time, for example:

> "Read plan.md. Implement Phase 1 (sections 3.1–3.6). Stop at Checkpoint 1 and show me the commands to verify it."

Then: "Implement Phase 2 tools and the tests in Checkpoint 2", and so on. After each phase, run the checkpoint yourself and commit (`git commit -m "phase N: ..."`) so you can roll back if a later phase breaks something.

---

## 14. Known pitfalls

- **HypoPG + `EXPLAIN ANALYZE`**: hypothetical indexes only affect plain `EXPLAIN`. Use real indexes for actual timings.
- **HypoPG is per-session**: create and EXPLAIN on the same connection; `hypopg_reset()` between candidates.
- **`CONCURRENTLY`** is not accepted by `hypopg_create_index` — strip it for validation, add it in the migration.
- **Forgot `ANALYZE`** after loading → planner estimates are wrong and results look random.
- **`pg_stat_statements` parameter placeholders** (`$1`) — can't EXPLAIN them directly; use the query-map approach (§4).
- **Small tables**: Postgres may prefer Seq Scans on small tables even with an index; demo on `lineitem`, `orders`, `partsupp`.
- **LLM hallucinated columns** → caught by `safety.py` schema check; feed the error back into `history` so the next proposal fixes it.
- **Docker memory**: give Docker ≥4 GB for TPC-H SF1 (~1 GB data + indexes).
