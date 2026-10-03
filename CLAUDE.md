# CLAUDE.md — QueryDoctor

QueryDoctor is an autonomous PostgreSQL performance agent (hackathon project). It finds slow queries, analyzes EXPLAIN plans, has an LLM propose indexes, validates them with HypoPG, critiques weak proposals and retries, asks a human to approve, then generates migration/rollback SQL and a report.

- **`plan.md`** is the build plan. Read it before making changes. Don't silently change its architecture. If you think the plan has a technical problem, explain it before deviating.
- **`task.md`** holds the owner's detailed instructions and work order. This file tracks progress.

Last updated: 2026-10-03

---

## Environment

| Item | Value |
|---|---|
| OS | macOS |
| Python | 3.12 venv at `.venv/` (run with `.venv/bin/python`) |
| DB | Docker `postgres:16` + hypopg, service `db` (container `querydoctor-db-1`), **port 5433** |
| Agent role | `qd_agent` / `qd_agent` (SELECT only + `pg_read_all_stats`) |
| Admin role | `postgres` / `postgres` (currently used for HypoPG work in `compare_costs`) |
| Dataset | TPC-H SF1, **PK indexes only** (intentional), `ANALYZE` done |
| LLM | Groq, `langchain-groq`, model `openai/gpt-oss-120b`, temperature 0 |
| Env vars (`.env`) | `GROQ_API_KEY`, `LLM_PROVIDER=groq`, `LLM_MODEL=openai/gpt-oss-120b` |
| Orchestration | LangGraph (`interrupt()` + checkpointer + thread_id) |
| UI | Streamlit calling the graph in-process (plan.md Option A); FastAPI only if it becomes genuinely necessary |

Expected row counts: region 5, nation 25, supplier 10,000, customer 150,000, part 200,000, partsupp 800,000, orders 1,500,000, lineitem 6,001,215 (total 8,661,245).

Useful commands:
```bash
docker compose up -d
docker compose exec -T db psql -U postgres -d tpch
.venv/bin/python scripts/run_workload.py          # fills pg_stat_statements (Q1,3,4,5,6,10,12,14,18,19 × 3)
.venv/bin/python -m querydoctor.tools.slow_queries
.venv/bin/python -m querydoctor.tools.explain
.venv/bin/python -m scripts.test_q12              # HypoPG experiment scripts (also test_q3, test_q6, test_q6_more_candidates)
```

---

## Non-negotiable design rules

1. **The LLM never produces numbers.** The LLM explains bottlenecks, proposes candidate indexes (and optional rewrites), and writes retry feedback. Statistics, plans, schema, costs, index-use detection, HypoPG validation, accept/reject thresholds and SQL safety checks all come from deterministic code and PostgreSQL.
2. **HypoPG is per-session.** Create hypothetical indexes and run EXPLAIN on the **same connection**. Call `hypopg_reset()` between independent experiments. Use one dedicated connection per candidate set or run, and keep connections out of graph state.
3. **Never use `EXPLAIN ANALYZE` with hypothetical indexes.** Use plain `EXPLAIN (FORMAT JSON)` only.
4. **Never send `CONCURRENTLY` to HypoPG.** Add it only in the migration: `CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`, with rollback `DROP INDEX CONCURRENTLY IF EXISTS ...`.
5. **Never EXPLAIN normalized `$1/$2` text from pg_stat_statements.** Map each queryid to the concrete SQL in `workload/tpch_queries/` and store the mapping in `data/query_map.json`.
6. **The LLM never executes arbitrary SQL.** Every LLM SQL passes `validate_index_sql()` (sqlglot) first. Allowed: exactly one `CREATE INDEX`, on an existing table and existing columns, with at most 4 key columns.
7. **No real indexes** until the optional local real-validation stage, and only on this local demo DB.
8. **Label planner numbers as "estimated planner cost reduction".** Never present them as runtime reduction. Actual runtime improvement may be reported only after a real index and `EXPLAIN ANALYZE` on the local scratch DB.
9. Don't fabricate or hardcode benchmark results. Keep the Groq key secret. `.env` must be in `.gitignore`.
10. **Critic is deterministic.** Accept only if the candidate is valid, the index is actually used, and reduction ≥ `IMPROVEMENT_THRESHOLD` (default 0.30). Retry if below threshold and iterations remain. Give up at `MAX_ITERATIONS` (3). The LLM writes only the retry feedback text.
11. Make small, testable changes and run tests after each one. Preserve working code; don't rewrite it for style. MVP first.

---

## Progress status (updated 2026-10-03)

### COMPLETE (verified)
- **Phase 1 infrastructure**
  - Docker Postgres 16 + hypopg on port 5433.
  - `pg_stat_statements` 1.10 and `hypopg` 1.4.3 installed.
  - TPC-H SF1 loaded with the 8 PK indexes only.
  - Generation scripts are in `scripts/`. Query files are in `workload/tpch_queries/q01–q22.sql`.
  - `scripts/run_workload.py` runs Q1,3,4,5,6,10,12,14,18,19 × 3.
- **Config:** `.gitignore` (covers `.env`), `.env.example`, `querydoctor/config.py` (pydantic-settings, `get_settings()`).
- **`db.py`:** `get_conn(admin=False)` reads URLs from config and sets statement_timeout. The agent connection is `qd_agent` with `default_transaction_read_only = on`.
  - **HypoPG works on this read-only agent connection.** All validation uses it, so admin is only for scripts and real validation.
- **`tools/query_map.py`:**
  - Gets each workload file's queryid via `EXPLAIN (VERBOSE)` ("Query Identifier", the same id pg_stat_statements uses), with no query execution.
  - Writes `data/query_map.json`, with all 22 queries mapped.
  - Helpers: `lookup(key)` and `get_query_sql("q12" | queryid)`.
  - Rebuild with `python -m querydoctor.tools.query_map` after reloading TPC-H, because queryids depend on OIDs.
- **`tools/slow_queries.py`:**
  - `get_slow_queries(limit, only_mapped=False)` keeps only `toplevel` entries, aggregates by queryid, and orders by total time.
  - Each row has queryid, query (normalized), calls, mean_exec_time, total_exec_time, rows, `name` and `sql` (concrete, from the map).
  - Use `only_mapped=True` in the UI and agent.
- **`tools/explain.py`:** `explain_query(sql, conn)` uses plain EXPLAIN only. `summarize_plan(plan, table_rows=None)` returns:
  - total_cost, plan_rows, tables, node_types
  - seq_scans (filter, table_rows, `large_table` ≥ 100k rows) and `large_seq_scans`
  - index_scans (index_cond, recheck_cond, filter)
  - sorts (sort_key)
  - joins (join_type, hash_cond, merge_cond, join_filter)
  - aggregates (group_key)
  - None fields are dropped; Q3 comes to about 1.7 KB of JSON.
- **`tools/schema.py`:**
  - `get_table_info(tables)`: columns + types, estimated_rows, size, indexes.
  - `get_table_row_estimates()` and `get_column_map()`.
- **`tools/safety.py`:** `check_index_sql(sql, column_map)` returns a dict with ok/reason/sql/table/columns/include. `validate_index_sql()` returns `(ok, reason)`.
  - Parses with sqlglot and requires exactly one statement, plain CREATE INDEX only.
  - Rejects UNIQUE, OR REPLACE, non-public schemas, methods other than btree/hash/brin, more than 4 key columns, more than 4 INCLUDE columns, and subqueries in WHERE.
  - Every referenced table and column must exist.
  - **Execute only the returned normalized `sql`**: it is regenerated from the AST, with no comments and no CONCURRENTLY.
- **`tools/hypopg.py`:** create, reset, list, plus `hypothetical_index_size()`.
- **`tools/validate.py`:** `compare_costs(sql, index_sqls: str | list, conn=None, table_rows=None, column_map=None)`.
  - Safety-checks every candidate, then runs the baseline on a clean session.
  - Tests **each candidate individually**, resetting in between, then **all valid candidates together**, all on ONE connection.
  - Returns `baseline_cost`, `baseline_summary`, `candidates[]`, `combined`, and `best` (the used candidate with the highest reduction).
    - Each candidate has: valid, error, sql, new_cost, reduction_pct, index_used, est_size_bytes, new_summary.
    - `combined` has: sqls, new_cost, reduction_pct, indexes_used (per sql), new_summary.
  - Top-level single-index keys are kept for the old scripts.
  - **`reduction_pct` is a percentage (0–100)**, while `IMPROVEMENT_THRESHOLD` is a fraction (0.30). Compare with `threshold * 100`.
- **Demo query exploration:** `scripts/explore_demo_queries.py` writes `data/demo_candidates.json`. The summary and recommendation are in **`docs/demo_queries.md`**.
  - Recommended demo set: Q14 (plain `l_shipdate` 25.9% rejected → covering index 93.0%), Q12 (23.6% → covering 74.0%), Q19 (`l_partkey` 82.9% on the first try). Optional Q3 shows give_up (best 28.9%).
  - **Key insight for the prompts:** covering indexes (`INCLUDE`) that allow index-only scans are what get past 30% on these queries.
- **Tests:** `pytest.ini` and `tests/{conftest,test_safety,test_tools}.py`.
  - **38 tests pass** (`.venv/bin/python -m pytest -q`).
  - DB tests are marked `db` and skip if Postgres is unreachable.
- **Dependencies** (pinned in `requirements.txt`): langgraph, langchain-core, langchain-groq, langgraph-checkpoint-sqlite, pydantic(-settings), sqlglot, streamlit, psycopg, duckdb, python-dotenv, pytest.

- **Phase D (LangGraph agent), working end to end from the CLI:**
  - `agent/state.py`: Pydantic `IndexCandidate`, `Proposal`, `CandidateResult`, `Critique` + `AgentState`. `history` and `trace` are append-only reducers.
  - `agent/llm.py`: `get_llm()` returns ChatGroq at temperature 0.
    - `get_structured_llm()` uses **function_calling** with a strict json_schema fallback.
    - Non-strict json_schema sometimes echoed the schema back on gpt-oss.
  - `agent/prompts.py`: diagnose, propose and critic prompts.
    - The proposer gets the SQL, plan summary, schema with existing indexes, measured history, critic feedback and human feedback.
    - Rules: no CONCURRENTLY or UNIQUE, ≤ 4 key and ≤ 4 INCLUDE columns, INCLUDE only columns the query reads, no partial indexes that hardcode query literals.
  - `agent/nodes.py`: fetch_context → diagnose → propose → safety_check → validate → critic → human_approval (`interrupt()`) → report.
    - safety_check also rejects candidates already tested and duplicates.
    - validate makes one `compare_costs` call on its own connection.
    - The critic is deterministic: accept if used and ≥ threshold, give_up at max_iterations (reporting the best across all iterations), otherwise retry. The LLM writes only the retry feedback text.
    - Human rejection with feedback goes back to propose (iteration+1, hard cap 2× max_iterations). Rejection without feedback goes to report.
  - `agent/graph.py`: `build_graph(checkpointer)`, `get_checkpointer("sqlite"|"memory")` (`data/checkpoints.db`), `run_config(thread_id)` (recursion_limit 80), `initial_state()`.
    - CLI: `python -m querydoctor.agent.graph --query q14 [--threshold 0.3] [--max-iterations 3] [--auto-approve] [--thread-id X]`.
    - It writes `output/<thread>/report.md`, `migration.sql` and `rollback.sql`.
  - `querydoctor/report.py` (the base for Phase F):
    - `index_name()` (`qd_<table>_<cols>`, ≤ 63 chars).
    - `migration_statement()` builds `CREATE INDEX CONCURRENTLY IF NOT EXISTS` via sqlglot from the re-validated SQL. Rollback is `DROP INDEX CONCURRENTLY IF EXISTS`.
    - `build_report()` keeps measured runtime, planner cost, estimated reduction and actual runtime ("Not measured" unless real validation ran) separate.
    - Savings in dollars are deliberately omitted, because they would equate planner cost with runtime.
  - `tools/slow_queries.get_query_stats(queryid)` provides measured stats for the report.
  - `tests/test_graph.py` uses scripted fake LLMs with real HypoPG. It covers accept + approve, retry → accept, give_up, unsafe or repeated candidates, human rejection with and without feedback, and migration naming.
    - The live Groq smoke test runs with `RUN_LLM_TESTS=1`.
  - **Total: 45 passed, 1 skipped (live).**
  - Live runs are logged in `docs/demo_queries.md`: Q14 at 93.0%, Q19 rejected by the human with feedback then approved at 94.2%, Q12 at 80% threshold going 10.1% → 74.0% → give_up.

- **Phase G (Streamlit MVP):** `ui/app.py`, run with `.venv/bin/streamlit run ui/app.py`. It calls the graph in-process (plan.md Option A) with the SQLite checkpointer and a per-run `thread_id` in `st.session_state`.
  - **Header:** DB status (PostgreSQL version, pg_stat_statements and hypopg versions).
  - **Slow-query table:** mapped queries only, with mean ms, calls and total ms. Selecting a row and clicking **Diagnose** starts a run.
  - **Live trace:** `graph.stream(..., stream_mode="updates")` writes into `st.status`.
  - **Metrics:** measured mean runtime, calls, baseline planner cost, best estimated reduction.
  - **Bottleneck card:** the LLM diagnosis plus the deterministic large seq scans.
  - **Approval card:** SQL, planner cost before and after, size, rationale; Approve, or Reject with optional feedback (`Command(resume=...)`).
  - **Outcome banner:** approved, rejected, or no recommendation.
  - **Tabs:** Agent trace; Candidates & validation; Report.
    - Candidates & validation has an Altair chart of estimated planner cost per candidate vs a gray baseline bar, a dashed "needed for ≥X%" line, status colors with a legend and tooltips, the attempts table, critic feedback and an unvalidated rewrite.
    - Report has the markdown, migration and rollback code, and downloads for report.md, migration.sql and rollback.sql.
  - **Sidebar:** threshold slider, max-iterations slider, Start over, safety notes.
  - **Verified with `streamlit.testing.v1.AppTest`** against real Groq + HypoPG:
    - Diagnose → approve → migration
    - Reject with feedback → re-propose → second approval
    - Reject without feedback → "rejected" banner
    - Threshold 0.95 with 1 iteration → give_up banner
    - The headless server boots and passes its health check.
  - **Not yet checked by eye in a browser.**

### MISSING
- Visual review of the dashboard in a real browser, plus a full demo rehearsal on Q14, Q12 and Q19 (plan.md Checkpoint 4: 3 queries in the browser without errors).
- `scripts/reset_demo.py` (and the sidebar "Reset demo" button from plan.md §8). Optional `real_validate`. DEMO_MODE LLM cache. README.md.
- `scripts/load_tpch.py` (loading was done manually), `scripts/reset_demo.py`.
- README.md.
- The project is not a git repo yet; run `git init` before committing phases.

### KNOWN ISSUES / NOTES
- pg_stat_statements also records ad-hoc admin and hypopg queries. `only_mapped=True` filters them out.
- Q17 and Q20 give dramatic reductions but aren't in the workload, because they run too long without indexes.
- Q3 is NOT a reliable give_up demo: the live LLM found a 31.0% index. Force give_up with `--threshold 0.8`.
- At a 30% threshold the LLM usually proposes covering indexes on the first try, so the retry story isn't guaranteed without the DEMO_MODE cache.
- The LLM can still write imperfect feedback text. Numbers always come from tools, and the report tables come from measured data only.
- An LLM call takes about 2–20 s. A full run takes about 20–60 s.

---

## Pending work, in order (from task.md)

**A. Finish the deterministic tools (Phase 2)** — DONE
1. `.gitignore`, `.env.example`, `config.py`. Read DB URLs, threshold, max iterations and timeout from env.
2. Build the query map: `data/query_map.json` plus a helper to get the concrete SQL for a queryid or for a name like `q12`.
3. Improve `summarize_plan()`: large-table seq scans, index conds, merge conds, still compact.
4. `tools/schema.py`: `get_table_info()`.
5. `tools/safety.py`: `validate_index_sql()`. Single statement, CREATE INDEX only, reject DROP/ALTER/DELETE/UPDATE/INSERT/multi-statement, table and columns must exist, at most 4 key columns.
6. Extend `compare_costs()` to multiple indexes, individual and combined evaluation, per-index use, sizes and a dedicated connection. Keep it backward compatible with the `scripts/test_q*.py` scripts.

**B. Choose the demo queries** — DONE (see docs/demo_queries.md)
- Measure Q3, Q6, Q10, Q12, Q14, Q18 and Q19 with HypoPG.
- Pick about 3 queries that are reliable and understandable. Include one that fails the first proposal, to show the retry loop.
- Write a results summary using estimated planner cost.

**C. Tests** — DONE (38 passing)
- Install `pytest`, then write `tests/test_tools.py` and `tests/test_safety.py`. They must cover:
  - slow queries ≥ 3
  - lineitem seq scan detected
  - a cost reduction on a demo query
  - `DROP TABLE lineitem` rejected
  - HypoPG reset between experiments
  - multi-index sets

**D. LangGraph (Phase 3–4)** — DONE
- Install `langchain-groq` and `langgraph-checkpoint-sqlite`.
- Write `state.py` (models from plan.md §5), `llm.py` (Groq, temperature 0) and `prompts.py`.
- `nodes.py` flow: fetch_context → diagnose → propose (structured `Proposal`, ≤ 3 candidates, with history and feedback) → safety_check → validate → critic. The critic routes to retry, give_up or human_approval (`interrupt()`). Approval leads to the report; rejection with feedback goes back to propose.
- Use a checkpointer and thread_id, and enforce the iteration cap and recursion_limit.

**E. CLI checkpoint** — DONE (verified on Q14, Q3, Q19 with human rejection, and Q12 forced give_up)
- `python -m querydoctor.agent.graph --query q12` must run end-to-end with an approval prompt and resume.
- **Don't start the UI until this works reliably.**

**F. Report (Phase 5)** — MOSTLY DONE in report.py; NEXT: review the output and optionally add real_validate
- `report.py`: migration SQL (CONCURRENTLY IF NOT EXISTS, `qd_<table>_<cols>` name), rollback SQL, and a markdown report.
- The report must keep these separate: measured pg_stat_statements runtime, planner cost, hypothetical reduction, and actual runtime (only if real validation ran).

**G. Streamlit MVP (Phase 6)** — DONE (pending a visual check in a browser)
- `ui/app.py` shows:
  - DB status
  - slow-query table and a Diagnose button
  - step-by-step trace
  - bottleneck
  - candidates and validation results
  - baseline vs candidate cost
  - accept/reject state
  - approve/reject with feedback
  - report, migration and rollback

**Later (nice-to-have / hardening):**
- real_validate (EXPLAIN ANALYZE on a real index in the local DB)
- `reset_demo.py`
- LLM response cache with `DEMO_MODE=1`
- workload mode, index hygiene, MCP server
- README and Devpost write-up

After each major phase, report: files created/modified, commands run, test results, what remains, and the next step. Update the status section of this file as you go.

---

## Nice-to-have phase (planned 2026-10-03)

Full plan: **`docs/nice_to_have_plan.md`**. Scope is every nice-to-have from plan.md except sponsor tracks, plus a new **index storage budget, default 350 MB** (on-disk index size, not RAM). Order of work:

0. Prep: git init, `scripts/reset_demo.py`, `scripts/plant_bad_indexes.py`, UI "Reset demo" button.
1. **Storage budget:**
   - `STORAGE_BUDGET_MB=350`.
   - The critic accepts only used, ≥ threshold **and ≤ budget** candidates. This is enforced in code, never by the LLM.
   - An over-budget winner → deterministic retry feedback ("propose a smaller index").
   - The human_approval node re-checks size as a second guard.
   - Workload mode: the sum of the selected set must be ≤ budget.
2. **Real validation:** admin connection, local DB only, `ENABLE_REAL_VALIDATION=1`.
   - In one transaction: EXPLAIN ANALYZE before (median of 3) → CREATE INDEX → actual size → EXPLAIN ANALYZE after → **ROLLBACK**.
   - An advisory lock serializes runs. Runs after approval when toggled.
3. **Monthly savings ($):** computed only from measured real-validation runtimes.
   - Assumptions `CALLS_PER_DAY` and `VCPU_HOUR_PRICE_USD` are stated in the report.
   - Index storage cost is shown too.
4. **Index hygiene:** `tools/hygiene.py` finds unused (idx_scan = 0, non-constraint), duplicate and prefix-redundant indexes. It suggests DROP + recreate SQL as report only, and shows reclaimable space.
5. **Workload mode:** `agent/workload_graph.py`.
   - Top-N queries → LLM proposal per query → safety check → candidate × query HypoPG benefit matrix (calls × cost drop).
   - **Greedy marginal-benefit-per-MB selection under the budget**, with all selected indexes present, so redundant indexes score ~0.
   - Then approval → combined migration.
6. **MCP server:** `querydoctor/mcp_server.py` (FastMCP, stdio).
   - Tools: list_slow_queries, explain_query, what_if_index, recommend_indexes (stops at the approval interrupt and returns thread_id), approve_recommendation, index_hygiene, recommend_workload.

Status: PLANNED, not started.
