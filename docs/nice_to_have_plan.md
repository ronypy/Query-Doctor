# Nice-to-have features: implementation plan

Scope: every nice-to-have from `plan.md` §0 and §9 **except sponsor-track integration**, plus a new **index storage budget** (default 350 MB).

Order of work: the budget first, because every other feature uses it. Each step ends with tests and a checkpoint before the next one starts.

| # | Feature | Depends on | Est. |
|---|---|---|---|
| 0 | Prep: `git init` + commit, `scripts/reset_demo.py` | – | 30 min |
| 1 | **Storage budget (350 MB)** | – | 1 h |
| 2 | Real validation (actual EXPLAIN ANALYZE latency + actual index size) | 1 | 1.5 h |
| 3 | Monthly savings estimate ($) | 2 | 45 min |
| 4 | Index hygiene (unused / duplicate / redundant indexes) | 0 | 1 h |
| 5 | Workload mode (top-N queries, shared indexes, greedy under budget) | 1, 3 | 2.5 h |
| 6 | MCP server (FastMCP, stdio) | 1–5 | 1 h |

---

## Design rules carried over (unchanged)
- **Deterministic enforcement:** budget, acceptance and selection are enforced by code, never by the LLM. The LLM is only *told* the budget so it proposes sensibly.
- **Numbers come from PostgreSQL:** HypoPG estimates are labeled as estimates. Runtime claims come only from real `EXPLAIN ANALYZE`.
- **No real DDL outside real validation**, and real validation only on the local demo DB.

---

## Step 0: Prep
- `git init`, add a `.gitignore` check, first commit (rollback point before the bigger changes).
- `scripts/reset_demo.py` (plan.md §3.6, needed by hygiene and real validation):
  - drop every non-constraint index (`qd_*`, planted hygiene demo indexes, anything left over)
  - `hypopg_reset()`, `pg_stat_statements_reset()`, `pg_stat_reset()`
  - re-run the workload
- `scripts/plant_bad_indexes.py`: creates a few deliberately bad indexes for the hygiene demo (a duplicate pair, an unused index on a wide column, a prefix-redundant pair). Admin connection, local DB only.
- Sidebar **"Reset demo"** button in the UI (plan.md §8), behind a confirmation checkbox.

**Checkpoint 0:** reset runs in under ~60 s (most of it is the workload rerun), and only the 8 PK indexes remain afterwards.

---

## Step 1: Storage budget (350 MB)

**Meaning:** a cap on the **on-disk size of the new indexes QueryDoctor recommends in one run**. It is disk storage, not RAM. Size comes from `hypopg_relation_size()` (an estimate) and, once real validation runs, from `pg_relation_size()` (actual).
- Single-query mode: the recommended index must be ≤ the budget.
- Workload mode: the **sum** of the selected set must be ≤ the budget.
- Option `BUDGET_COUNTS_EXISTING_QD=1` (default off): subtract the size of already-applied real `qd_*` indexes from the budget, for cumulative use across runs.

**Changes**
- `config.py`: `STORAGE_BUDGET_MB=350` (also in `.env.example`).
- `AgentState`: `storage_budget_bytes`. The UI sidebar slider and CLI `--budget-mb` override it per run.
- `validate` node: each result gets `within_budget` and status `over_budget`. `best` = highest-reduction candidate that is **used AND within budget**. Also record `best_over_budget` for the feedback.
- `critic` (deterministic): accept only if valid, used, ≥ threshold, and **≤ budget**.
  - If the only passing candidates are over budget → retry with deterministic feedback: "X reaches 95% but needs 415 MB > 350 MB budget; propose a smaller index (fewer INCLUDE columns, fewer key columns, partial, or a narrower table)".
  - The LLM adds wording on top, as today.
- `propose` prompt: show the budget and each table's size; say that over-budget indexes are rejected automatically.
- **Human approval can never receive an over-budget index**, because the critic never routes there. A guard in `human_approval` re-checks size and refuses as a second line of defense.
- Report and UI: a "Storage: 316 MB of 350 MB budget" line, a budget column in the attempts table, and an "Over budget" status color/label in the chart.

**Tests**
- Fake-LLM graph test: an over-budget winner is not accepted and the retry sees a budget message.
- A within-budget candidate is accepted.
- The human_approval guard refuses an over-budget `best`.
- Workload budget sum (covered in step 5).

**Checkpoint 1:** live Q19 run. The 415 MB first pick is rejected for budget and the agent retries to an index ≤ 350 MB, e.g. `lineitem(l_shipmode, l_shipinstruct, l_partkey, l_quantity)` ≈ 316 MB.

---

## Step 2: Real validation (actual latency)

**Approach (safer than a scratch copy):** PostgreSQL DDL is transactional. On the **admin connection, inside one transaction**:
```
BEGIN;
SET LOCAL statement_timeout = '300s'; SET LOCAL maintenance_work_mem = '512MB';
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>   × (1 warm-up + 3)  → median "before"
CREATE INDEX qd_rv_... ON ...          -- built from the re-validated, normalized SQL
SELECT pg_relation_size('qd_rv_...')   -- ACTUAL size
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>   × (1 warm-up + 3)  → median "after", index used?
ROLLBACK;                              -- index disappears, even if anything fails
```
- No leftover index is possible: ROLLBACK runs in `finally`, and a crash also aborts the transaction.
- Other sessions never see the uncommitted index. Writes to that table block while it runs, which is fine on the local demo DB.
- `pg_advisory_lock` serializes real validations so two UI runs can't collide.
- Guard: runs only if `ENABLE_REAL_VALIDATION=1` **and** the admin URL host is `localhost`/`127.0.0.1`. Otherwise it is skipped with a clear message.

**Graph:** `human_approval → (approved) → real_validate → report`, controlled by a per-run toggle (`state.real_validate`, UI checkbox, CLI `--real-validate`). Adds to `best["actual"]`: before/after median ms, all runs, actual size, index used in the actual plan, and an **actual-size budget check**. The HypoPG size is an estimate, so if the actual size exceeds the budget the report flags it prominently.

**Report:** the existing "Actual runtime improvement" section fills in: "Measured on local demo DB: 381 ms → 24 ms (median of 3, warm cache)", with the caveats (warm cache, single machine, SF1).

**Tests**
- Q14 real validation is marked `db` and slow (~20–40 s for a lineitem index build).
  - Asserts a rollback happened: no `qd_rv_*` index in `pg_indexes` afterwards.
  - Asserts after < before, and actual size > 0.
- The guard refuses a non-local URL.

**Checkpoint 2:** UI approve with "Run real validation" ticked shows actual before/after ms, and `pg_indexes` shows no leftover index.

---

## Step 3: Monthly savings estimate

**Honesty rule:** dollars are computed **only from measured runtimes** (real validation before/after), never from planner cost. Without real validation the report says "Run real validation to estimate savings".

```
saved_seconds_per_call = (before_ms - after_ms) / 1000
monthly_cpu_hours      = saved_seconds_per_call × calls_per_day × 30 / 3600
monthly_savings_usd    = monthly_cpu_hours × VCPU_HOUR_PRICE_USD
```
- `calls_per_day`: `CALLS_PER_DAY` config / UI number input (an assumption shown in the report).
  - Default: derived from pg_stat_statements calls ÷ days since `pg_stat_statements_info.stats_reset`, clearly labeled.
  - Demo stats (3 calls in minutes) make the derived rate unrealistic, so the UI lets you enter it.
- `VCPU_HOUR_PRICE_USD` (default 0.04, an assumption stated in the report). It's a simple model: it treats query time as one busy vCPU and ignores I/O and parallel workers.
- Also show the **added write and storage cost** of the index (storage: actual MB × `STORAGE_GB_MONTH_USD`, default 0.10) so the estimate isn't one-sided.

**Tests:** pure-function tests for the formula; the report shows "n/a" without real validation.

**Checkpoint 3:** the report after real validation shows savings with every assumption listed.

---

## Step 4: Index hygiene

`querydoctor/tools/hygiene.py`, read-only, agent connection:
- **Unused:** `pg_stat_user_indexes.idx_scan = 0`, excluding indexes that back a PK/unique/exclusion constraint. Shows size and the stats window (`pg_stat_database.stats_reset`), with a warning when stats are recent: "unused since <reset>, N hours".
- **Duplicate:** same table, same `indkey`, same opclasses, same expressions and predicate, same method (from `pg_index`).
- **Redundant (prefix):** B-tree A's key columns are a leading prefix of B's, no predicates, A not backing a constraint. Flagged "probably redundant".
- Output: findings with reason, size and suggested `DROP INDEX CONCURRENTLY IF EXISTS ...` (+ recreate statement for rollback, from `pg_get_indexdef`). **Report only, nothing is executed.**
- Link to the budget: "reclaimable space" total, shown in the UI.
- CLI `python -m querydoctor.tools.hygiene`; UI **"Index hygiene"** tab; MCP tool.

**Tests:** in one admin **transaction** (rolled back), create a scratch table with a duplicate pair and a prefix-redundant pair, run the catalog checks on that same connection, and assert both are detected and the PK is never flagged. Unused detection is checked on the planted indexes.

**Checkpoint 4:** after `plant_bad_indexes.py`, the hygiene tab lists the duplicate, unused and redundant indexes with sizes. `reset_demo.py` cleans them up.

---

## Step 5: Workload mode

Optimize the top-N queries together and pick a set of indexes that maximizes total benefit within the storage budget. This is the classic index-selection problem.

**Pipeline** (a second LangGraph graph, `agent/workload_graph.py`, reusing the existing nodes' tools):
1. **collect:** top-N mapped queries (default 6) with calls, baseline plan and cost.
2. **propose_all (LLM):** one structured `Proposal` per query (existing prompt, plus budget info) → candidate pool. Each candidate goes through the safety check and normalized-SQL dedup. Optionally seeded with the accepted indexes from earlier single-query runs.
3. **evaluate (HypoPG):** a candidate × query benefit matrix on one connection. `benefit(c, q) = calls_q × max(0, cost_q − cost_q|c)`, keeping only cases where the index is used.
4. **select (deterministic greedy, budget-constrained):**
   - Each round, re-evaluate every remaining candidate **together with the already-selected set**: all selected + the candidate as hypothetical indexes at once. This gives its *marginal* benefit, so redundant indexes score ~0 automatically.
   - Pick the one with the best `marginal_benefit / size_MB`, minus a write-overhead penalty `WRITE_PENALTY_PER_MB × size`. The penalty is configurable and 0 by default for read-only TPC-H, noted in the report.
   - Stop when nothing fits the remaining budget or marginal benefit < `MIN_MARGINAL_PCT`.
   - Cost: about 18 candidates × 6 queries × ~4 rounds of plain EXPLAINs. These are planning-only, so it takes seconds.
5. **human_approval (interrupt):** approve or reject the whole set. Rejecting with feedback re-runs propose_all with the feedback.
6. **report:** one migration with all selected indexes, a combined rollback, a per-query before/after planner-cost table, the budget used, the indexes considered but not chosen (with the reason: over budget / redundant / no benefit), and savings if real validation is enabled (each query validated with the whole set inside one transaction).

**UI:** a **"Workload mode"** page/tab with an N slider and a budget readout. It shows a matrix heatmap (candidate × query est. reduction; sequential single-hue ramp per the dataviz rules), the selection steps, the approval card and the report.

**CLI:** `python -m querydoctor.agent.workload_graph --top 6 --budget-mb 350`.

**Tests**
- Fake-LLM test: greedy respects the budget (sum ≤ budget).
- A redundant duplicate candidate is not selected twice.
- A shared index that helps two queries outranks one that helps a single query.
- The approval interrupt fires and the report includes all selected indexes.

**Checkpoint 5:** a live run on the top 6 queries picks a set ≤ 350 MB, and the report shows per-query estimated reductions.

---

## Step 6: MCP server

`querydoctor/mcp_server.py` using the official `mcp` SDK (FastMCP, stdio). Tools:

| Tool | What it does |
|---|---|
| `list_slow_queries(limit)` | mapped slow queries with measured stats |
| `explain_query(query)` | plan summary for `q14` / a queryid |
| `what_if_index(query, index_sql)` | HypoPG comparison (safety-checked) |
| `recommend_indexes(query, threshold?, budget_mb?)` | runs the agent **until the approval interrupt** and returns the recommendation + `thread_id` (no auto-approval) |
| `approve_recommendation(thread_id, approved, feedback?, real_validate?)` | resumes the graph; returns the report + migration/rollback SQL |
| `index_hygiene()` | hygiene findings |
| `recommend_workload(top_n, budget_mb?)` | workload mode until approval; approval uses the same `approve_recommendation` |

- Uses the SQLite checkpointer, so threads persist across tool calls.
- Nothing executes DDL except real validation, which needs the env flag.
- Setup docs:
  - Claude Code: `claude mcp add querydoctor -- /abs/path/.venv/bin/python -m querydoctor.mcp_server`
  - Claude Desktop: a JSON config snippet
- **Tests:** an in-process MCP client session lists the tools, calls `what_if_index` on Q14, and calls `recommend_indexes` with a fake LLM up to the interrupt.

**Checkpoint 6:** in Claude Code, ask "why is q14 slow and what index would help within 350 MB?" → tool calls → recommendation → approve → migration.

---

## Out of scope / recommended separately
- Sponsor tracks (excluded as requested).
- From plan.md Phase 8 (hardening, not a nice-to-have but strongly recommended before the demo): `DEMO_MODE` LLM response cache, 3 rehearsals, backup video, README.

## Open decisions (defaults chosen; say if you want otherwise)
1. **Budget scope:** per run, covering only new recommended indexes. Optional cumulative mode counts existing real `qd_*` indexes.
2. **Over-budget handling:** hard block in code; the human cannot approve an over-budget index. Raising the budget slider for a new run is the escape hatch.
3. **Savings basis:** only from measured real-validation runtimes. No dollar figure is derived from planner cost.
4. **Real validation method:** transactional build + EXPLAIN ANALYZE + ROLLBACK on the local DB, instead of a separate scratch database copy.
