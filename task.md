You are working on my hackathon project **QueryDoctor**. The repository already contains `plan.md`, which is the authoritative build plan. Read `plan.md` completely before making changes.

Do NOT assume the repository is at Phase 1. I have already manually implemented and tested several parts of the plan. Your first task is to inspect the existing repository, run the relevant tests/checks, compare the current codebase against `plan.md`, and determine exactly what is complete, partially complete, broken, or missing.

## Current project context

The project is QueryDoctor: an autonomous PostgreSQL performance agent that discovers slow queries, analyzes execution plans, proposes indexes, validates them safely with HypoPG, critiques weak proposals, uses human approval, and generates migration/report output.

Environment:

- macOS
- Docker PostgreSQL 16
- PostgreSQL exposed on port `5433`
- Python virtual environment using Python 3.12
- TPC-H SF1 dataset loaded
- Groq API will be used for the LLM
- Preferred Groq model:
  `openai/gpt-oss-120b`
- We want LangGraph for orchestration
- Streamlit should be used for the MVP UI unless there is a strong reason otherwise

## Things already completed manually

Please VERIFY these rather than rebuilding them blindly.
### Virtual ENV
/Users/rony/Desktop/101/MLH_Hack/QueryDoctor/.venv
### PostgreSQL / TPC-H

TPC-H SF1 is loaded into PostgreSQL.

Expected table cardinalities:

- region: 5
- nation: 25
- supplier: 10,000
- customer: 150,000
- part: 200,000
- partsupp: 800,000
- orders: 1,500,000
- lineitem: 6,001,215

Expected total:
8,661,245 rows

The database was intentionally created with primary-key indexes only and no useful secondary indexes so QueryDoctor can discover optimization opportunities.

`ANALYZE` has been executed.

The following extensions are installed:

- `pg_stat_statements`
- `hypopg`

`pg_stat_statements` is enabled through `shared_preload_libraries`.

### Workload

TPC-H queries have been executed multiple times.

`pg_stat_statements` successfully records slow queries.

Some measured mean runtimes were approximately:

- Q18: 3519 ms
- Q3: 2067 ms
- Q1: 1839 ms
- Q10: 737 ms
- Q12: 488 ms
- Q19: 414 ms
- Q5: 387 ms
- Q14: 381 ms
- Q6: 326 ms
- Q4: 280 ms

Do not assume these exact timings will remain identical after reruns.

### Current Python tools

The repository should already contain some or all of:

- `querydoctor/db.py`
- `querydoctor/tools/slow_queries.py`
- `querydoctor/tools/explain.py`
- `querydoctor/tools/hypopg.py`
- `querydoctor/tools/validate.py`

Existing functionality should include:

1. `get_slow_queries()`
   - reads from `pg_stat_statements`

2. `explain_query()`
   - runs `EXPLAIN (FORMAT JSON)`

3. `summarize_plan()`
   - recursively extracts plan information such as:
     - total cost
     - node types
     - sequential scans
     - index scans
     - sorts
     - joins
     - filters / conditions where available

4. HypoPG helper functions
   - create hypothetical index
   - reset hypothetical indexes

5. `compare_costs()`
   - baseline plain EXPLAIN
   - create hypothetical index
   - rerun EXPLAIN in the SAME PostgreSQL connection
   - compare planner cost
   - detect whether hypothetical index was actually used

Important:
HypoPG indexes are session-specific. Creation and EXPLAIN must happen on the same connection.

Do NOT use `EXPLAIN ANALYZE` with hypothetical indexes.

### HypoPG experiments already performed

Q6 was tested with:

`CREATE INDEX ON lineitem (l_shipdate, l_discount, l_quantity)`

Result approximately:

- baseline cost: 159,067.50
- new cost: 141,357.38
- reduction: 11.13%
- hypothetical index used: yes
- plan changed to Bitmap Index Scan / Bitmap Heap Scan

This is useful because it shows that QueryDoctor must reject indexes that are used but do not improve cost enough.

Q3 results:

1.
`CREATE INDEX ON customer (c_mktsegment, c_custkey)`
- reduction: ~1.74%
- used: yes

2.
`CREATE INDEX ON orders (o_orderdate, o_custkey)`
- reduction: 0%
- used: no

3.
`CREATE INDEX ON lineitem (l_shipdate, l_orderkey)`
- reduction: 0%
- used: no

Q12 results:

1.
`CREATE INDEX ON lineitem (l_receiptdate)`
- reduction: ~14.27%
- used: yes

2.
`CREATE INDEX ON lineitem (l_shipmode, l_receiptdate)`
- reduction: ~23.61%
- used: yes

3.
`CREATE INDEX ON lineitem (l_receiptdate, l_shipmode, l_orderkey)`
- reduction: ~10.06%
- used: yes

The current improvement threshold in the design is 30%.

### Important design principle

The LLM must NEVER fabricate performance numbers.

The architecture should separate responsibilities:

LLM:
- explain bottleneck
- propose candidate indexes
- optionally propose query rewrites
- generate retry feedback

Deterministic code / PostgreSQL:
- slow-query statistics
- EXPLAIN plans
- schema metadata
- cost numbers
- index-use detection
- HypoPG validation
- accept/reject threshold logic
- SQL safety validation

## Your first task: repository audit

Before adding new features:

1. Read `plan.md`.
2. Inspect the full repository tree.
3. Inspect the files already implemented.
4. Run the existing relevant scripts/tests.
5. Check Docker/PostgreSQL connectivity.
6. Verify:
   - TPC-H table counts
   - extensions
   - `pg_stat_statements`
   - `get_slow_queries()`
   - `EXPLAIN (FORMAT JSON)`
   - plan summarization
   - HypoPG same-session behavior
   - current cost-comparison functionality
7. Compare the actual repository against every requirement in `plan.md`.

Create a concise status report grouped as:

- COMPLETE
- PARTIALLY COMPLETE
- MISSING
- BROKEN / NEEDS FIX

Do not rewrite working code merely for style.

Fix actual bugs if they prevent the checkpoint from passing.

## Then continue implementation

After the audit, continue from the earliest incomplete required part of `plan.md`.

Prioritize MVP features only.

The target order should roughly be:

### A. Finish deterministic tools first

Complete Phase 2 before introducing LangGraph.

Ensure these are robust:

#### `get_slow_queries()`

Return structured objects/dicts with:

- queryid
- query
- calls
- mean_exec_time
- total_exec_time
- rows

Handle `pg_stat_statements` normalized SQL correctly.

#### Original-query mapping

Because `pg_stat_statements` replaces literals with `$1`, `$2`, etc., implement the mapping described in `plan.md`.

Map each relevant `queryid` back to the original concrete TPC-H query text.

Store the mapping in something like:

`data/query_map.json`

The agent must use the original concrete SQL for EXPLAIN.

Do not attempt to EXPLAIN the normalized `$1`, `$2` text directly.

#### `summarize_plan()`

Make it deterministic.

Extract at minimum:

- total cost
- plan rows
- node types
- sequential scans
- large-table scans
- index scans
- filters
- index conditions
- joins
- hash conditions
- join filters
- sorts
- sort keys

Keep the summary compact enough to send to the LLM.

#### Schema tool

Implement:

`get_table_info(tables)`

Return:

- table
- columns
- PostgreSQL types
- estimated row count
- table size
- existing indexes

#### Safety tool

Implement:

`validate_index_sql(sql)`

Use `sqlglot`.

Requirements:

- exactly one SQL statement
- only CREATE INDEX
- reject DROP
- reject ALTER
- reject DELETE
- reject UPDATE
- reject INSERT
- reject multiple statements / injection
- referenced table must exist
- referenced columns must exist
- maximum four key columns unless there is a strong reason to change this constraint

Do not let the LLM directly execute arbitrary SQL.

#### Improve `compare_costs()`

It should support:

- one candidate index
- multiple hypothetical indexes simultaneously
- evaluating each candidate individually
- evaluating a candidate set together
- baseline cost
- new cost
- percent reduction
- whether each hypothetical index was actually used
- new plan summary
- resetting HypoPG between independent experiments

All HypoPG operations for a candidate set must happen within one dedicated database connection.

### B. Establish reliable demo queries

Before LangGraph, test several TPC-H queries.

We initially considered Q3, Q6, and Q12, but do NOT force them to be the final demo set.

Also test promising candidates such as:

- Q10
- Q14
- possibly Q18 or Q19

Select approximately three queries based on measured behavior:

- meaningful planner cost reduction
- understandable bottleneck
- reliable execution
- interesting before/after plan
- suitable for a live hackathon demo

It is acceptable and desirable to keep one query where the initial proposal is rejected because improvement is below threshold. That demonstrates the critique/retry loop.

Create a small results summary for candidate demo queries.

Do not claim planner-cost reduction equals runtime reduction.

Label planner numbers as estimated planner cost reduction.

### C. Tests

Implement or complete tests described in `plan.md`.

At minimum verify:

- `get_slow_queries()` returns multiple queries
- plan summarizer detects lineitem sequential scans where appropriate
- valid hypothetical indexes can reduce planner cost on at least one selected demo query
- invalid SQL such as `DROP TABLE lineitem` is rejected
- HypoPG indexes are reset between experiments
- multiple-index candidate sets work

Run tests before continuing.

### D. Only then implement LangGraph

Once deterministic tools work, implement the state, models, prompts, LLM factory, nodes, and graph described in `plan.md`.

Use Groq.

Environment configuration should support:

`GROQ_API_KEY`

`LLM_PROVIDER=groq`

`LLM_MODEL=openai/gpt-oss-120b`

Use `langchain-groq`.

Use temperature 0.

Use Pydantic structured output for index proposals.

Do not parse free-form LLM text when structured output is possible.

Expected agent flow:

START
→ fetch_context
→ diagnose
→ propose
→ safety_check
→ validate
→ critic

Critic routes:

- retry → propose
- give_up → report
- accept → human_approval

Human approval:

- approved → report / optional real validation
- rejected with feedback → propose again

Use LangGraph `interrupt()` and a checkpointer.

Use a thread ID.

Enforce maximum iterations.

### Critic behavior

Make acceptance largely deterministic.

For example:

Accept only if:

- candidate is valid
- hypothetical index is actually used
- planner-cost reduction >= configured threshold

Retry if below threshold and remaining iterations exist.

Give up at maximum iterations.

Use the LLM only to produce useful retry feedback when appropriate.

### LLM prompt requirements

The proposer should receive:

- original SQL
- compact plan summary
- relevant table/schema metadata
- current indexes
- previous attempts
- measured results of previous attempts
- critic feedback

It should return at most three index candidates.

Do not propose an index already tested unsuccessfully unless there is a meaningful modification.

### E. CLI checkpoint before UI

Before implementing Streamlit, make the graph work end-to-end from the terminal.

Something like:

`python -m querydoctor.agent.graph --query q12`

Expected behavior:

1. load query
2. inspect plan
3. explain bottleneck
4. propose indexes
5. safety validation
6. HypoPG validation
7. critic
8. retry if necessary
9. interrupt for human approval
10. resume
11. generate report

Do not move to UI until this works reliably.

### F. Report

Implement:

- migration SQL
- rollback SQL
- markdown report

Migration should use:

`CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`

Rollback should use:

`DROP INDEX CONCURRENTLY IF EXISTS ...`

Do NOT send `CONCURRENTLY` to HypoPG.

Report should distinguish:

- measured runtime from pg_stat_statements
- PostgreSQL planner estimated cost
- hypothetical estimated cost reduction
- actual runtime improvement only if a real index was created and EXPLAIN ANALYZE was run on the local scratch DB

### G. Streamlit MVP

Only after CLI workflow works.

Use Streamlit directly with LangGraph unless FastAPI becomes genuinely necessary.

Dashboard should show:

- database connection status
- slow queries
- mean execution time
- calls
- total execution time
- Diagnose button
- step-by-step agent trace
- original plan bottleneck
- candidate indexes
- validation results
- baseline vs candidate planner cost
- accepted/rejected state
- human approval
- report
- migration SQL
- rollback SQL

Do not spend hackathon time building unnecessary frontend complexity.

## Important engineering requirements

- Preserve working functionality.
- Make small, testable changes.
- Run relevant tests after each meaningful change.
- Prefer simple implementation over abstractions that are unnecessary for the hackathon.
- Do not silently change the architecture in `plan.md`.
- If you think the plan contains a technical problem, explain it before changing the design.
- Do not fabricate benchmark results.
- Do not hardcode fake improvement percentages.
- Use actual PostgreSQL/HypoPG results.
- Never expose the Groq API key.
- Ensure `.env` is in `.gitignore`.
- Do not allow arbitrary LLM-generated SQL execution.
- Do not create real indexes until the optional local real-validation stage.
- Do not modify production-style data outside this local hackathon database.

## How to work

Work iteratively.

First:
1. audit the repository,
2. report current phase/checkpoint status,
3. fix incomplete deterministic tooling,
4. run the relevant checkpoint tests.

Then continue sequentially.

Do NOT attempt to implement the entire remaining project in one giant unverified change.

After each major phase, show:

- files created/modified
- commands executed
- test/checkpoint results
- what remains
- the next recommended step

Start now by reading `plan.md` and auditing the repository.
