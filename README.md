# QueryDoctor 🩺

**An AI database engineer for PostgreSQL. It finds your slow queries, proves which indexes help without touching production, and asks before it changes anything.**

> **The LLM proposes. PostgreSQL decides. A human approves.**

![QueryDoctor architecture](docs/architecture.png)

---

## The core idea

Slow queries are usually fixed by one well-chosen index. Picking that index is expert work: you have to read execution plans, understand selectivity, and avoid indexes the planner will ignore. LLMs are good at the "ideas" part of that job, but they can't be trusted with numbers. Ask one "how much faster will this be?" and it will happily invent an answer.

QueryDoctor splits the job so that each part does what it's good at:

| The LLM does | PostgreSQL + deterministic code does |
|---|---|
| Explain the bottleneck in plain language | Measure runtimes (`pg_stat_statements`) |
| Propose candidate indexes | Check SQL safety (allow-list, single `CREATE INDEX` only) |
| Write feedback for the next attempt | Test indexes **without building them** (HypoPG hypothetical indexes) |
| | Decide accept / retry / give up (fixed rules) |
| | Measure real speed-ups (rolled-back transaction) |
| | Generate the migration, rollback and report |

**No number in a QueryDoctor report comes from the LLM.**

---

## How it works

1. **Find slow queries** using measured runtimes from `pg_stat_statements`.
2. **Diagnose:** the LLM explains the bottleneck (e.g. "a sequential scan reads all 6 million rows of `lineitem` to keep 31k").
3. **Propose:** the LLM suggests up to 3 indexes as structured output.
4. **Safety check:** `sqlglot` lets through only a single plain `CREATE INDEX` on real tables and columns.
5. **What-if test:** HypoPG makes PostgreSQL plan the query *as if* the index existed. Nothing gets built.
6. **Critic:** accepts only if the planner **actually uses** the index **and** the estimated cost drops by ≥ 30%. Otherwise it retries with feedback, up to 3 rounds.
7. **Human approval:** you approve, or reject with feedback (which sends the agent back to step 3).
8. **Real validation (optional):** builds the real index **inside a transaction that is always rolled back**, then measures with `EXPLAIN ANALYZE`.
9. **Report:** a `CREATE INDEX CONCURRENTLY` migration, a rollback script, and a report that separates *measured* from *estimated* numbers.


---

## Demo: one query, start to finish

A real run on TPC-H **Q14** (6M-row `lineitem` table, primary keys only):

```console
$ python -m querydoctor.agent.graph --query q14 --real-validate --calls-per-day 10000

[fetch_context] Baseline planner cost 146,331. Seq Scan on lineitem (~6.0M rows)
                filtering (l_shipdate >= '1995-09-01' AND l_shipdate < '1995-10-01');
                Seq Scan on part (~200,000 rows). Measured mean runtime 381 ms over 3 calls.

[diagnose]      The expensive part of the plan is the sequential scan of lineitem that reads
                all 6 million rows and then filters on l_shipdate to keep only about 31k rows…

[propose]       Iteration 1: proposed 3 index(es):
                  lineitem(l_shipdate, l_partkey) INCLUDE (l_extendedprice, l_discount)
                  lineitem(l_shipdate) INCLUDE (l_partkey, l_extendedprice, l_discount)
                  part(p_partkey) INCLUDE (p_type)

[safety_check]  All 3 candidates passed the safety check.

[validate]      lineitem(l_shipdate, l_partkey) INCLUDE (…): 93.0% est. planner cost, used ✓
                lineitem(l_shipdate) INCLUDE (…):            93.0% est. planner cost, used ✓
                part(p_partkey) INCLUDE (p_type):             1.0% est. planner cost, used ✓

[critic]        ACCEPT: reduces estimated planner cost by 93.0% (threshold 30%)
                and the planner uses it.

==================================================================
Approve this index recommendation?
  CREATE INDEX ON lineitem(l_shipdate, l_partkey) INCLUDE (l_extendedprice, l_discount)
  Estimated planner cost: 146,331 → 10,209 (93.0% reduction)
==================================================================
Approve? [y/n] y

[real_validate] Actual (EXPLAIN ANALYZE, median of 3): 254 ms → 49 ms (5.2× faster),
                real index used, actual size 232 MB, built in 4.0 s and rolled back.

[report]        Report ready. Migration creates qd_lineitem_l_shipdate_l_partkey.
```

The generated migration and rollback:

```sql
-- QueryDoctor migration
-- Estimated planner cost reduction (HypoPG): 93.0%
-- Measured locally: 254 ms -> 49 ms, actual size 232 MB
CREATE INDEX CONCURRENTLY IF NOT EXISTS qd_lineitem_l_shipdate_l_partkey
    ON lineitem(l_shipdate, l_partkey) INCLUDE (l_extendedprice, l_discount);

-- Rollback
DROP INDEX CONCURRENTLY IF EXISTS qd_lineitem_l_shipdate_l_partkey;
```

### What happens when the first idea isn't good enough

With a demanding 80% threshold, Q12 shows the self-correction loop:

```text
[validate]  lineitem(l_receiptdate, l_shipmode, l_orderkey): 10.1% est. planner cost, used ✓
[critic]    RETRY: the index was used, but it only gave 10.1%… the predicates on
            l_commitdate and l_shipdate are applied after the index lookup. Cover the
            filter columns so the planner can use an index-only scan.
[validate]  lineitem(l_shipmode, l_receiptdate, l_orderkey) INCLUDE (l_commitdate, l_shipdate): 74.0% ✓
[critic]    GIVE_UP: best index reached 74.0%, below the 80% threshold.
[report]    No index met the acceptance rule; no migration generated.
```

QueryDoctor reports "no recommendation" honestly instead of over-claiming.

### What happens when you push back

```text
[critic]          ACCEPT: lineitem(l_partkey, l_shipmode, l_shipinstruct, l_quantity)
                  INCLUDE (…): 95.0%, est. size 415 MB
Approve? [y/n] n
Feedback: Prefer a smaller index without INCLUDE columns
[propose]         Iteration 2: …
[critic]          ACCEPT: lineitem(l_shipmode, l_shipinstruct, l_partkey, l_quantity): 94.2%, 316 MB
Approve? [y/n] y
```

The same flow runs in the **Streamlit dashboard**: a live step-by-step trace, cost charts, an approval card, and download buttons for the report, migration and rollback.

---

## Results (TPC-H scale factor 1, local PostgreSQL 16)

*Estimated* = PostgreSQL planner cost with a hypothetical index. *Measured* = real `EXPLAIN ANALYZE` with a real index (warm cache, then rolled back).

| Query | Recommended index | Est. planner cost reduction | Measured runtime |
|---|---|---|---|
| Q14 | `lineitem(l_shipdate, l_partkey) INCLUDE (…)` | 93.0% | 254 ms → 49 ms (**5.2×**) |
| Q19 | `lineitem(l_partkey, l_shipmode, …)` | 95.0% | 473 ms → 25 ms (**18.7×**) |
| Q18 | `lineitem(l_orderkey) INCLUDE (l_quantity)` | 40.8% | 3.4 s → 1.1 s (**3.0×**) |
| Q12 | `lineitem(l_shipmode, l_receiptdate) INCLUDE (…)` | 74.0% | – |
| **Workload** (top 6 queries, 3 shared indexes) | greedy set | **31.5%** of total workload cost | Q18 5.0×, Q19 18.7× |

Real validation also catches estimates that are wrong. In the workload run, Q3 and Q10 used the new indexes but ran **0.9×** (slightly slower). The report flags that instead of hiding it.

---

## How much can it save per year?

QueryDoctor estimates savings **only from measured runtimes**, never from planner estimates:

$$
\text{vCPU-hours saved per year} = \frac{(t_{\text{before}} - t_{\text{after}})\,[\text{s}] \times \text{calls per day} \times 365}{3600}
$$

Here is the measured workload result (6 queries, 3 indexes) projected onto a cloud database at different traffic levels. The CPU time saved scales linearly with traffic.

| Traffic (calls/day **per query**) | CPU time saved / year | ≈ vCPUs freed (24/7) | Yearly saving at \$0.04 / vCPU-hr | Yearly saving at \$0.10 / vCPU-hr |
|---|---|---|---|---|
| 10,000 (small app) | ≈ 5,400 vCPU-hours | ≈ 0.6 | ≈ **\$220** | ≈ **\$540** |
| 100,000 (busy API / dashboard) | ≈ 54,000 vCPU-hours | ≈ 6 | ≈ **\$2,200** | ≈ **\$5,400** |
| 1,000,000 (high-traffic service) | ≈ 541,000 vCPU-hours | ≈ 62 | ≈ **\$21,700** | ≈ **\$54,100** |

- **Price range:** \$0.04 per vCPU-hour is roughly commodity compute; \$0.10 is closer to managed-database pricing (e.g. RDS or Cloud SQL). Plug in your own provider's rate.
- **Storage is negligible next to compute:** the three indexes add about 600 MB, roughly **\$0.70 a year** at \$0.10/GB-month.
- **Regressions are subtracted:** queries that got slower are counted as negative savings.
- **The biggest win is often an avoided upgrade.** Freeing ~6 busy vCPUs can mean staying on your current database instance instead of moving up a size, which is a step change in the monthly bill rather than a gradual saving.
- **Engineer time counts too:** diagnosing one slow query by hand often takes an afternoon. QueryDoctor does it in about 20–60 seconds.

> These are projections from measurements on one local machine (TPC-H SF1, warm cache). Real savings depend on your data, hardware and traffic. The generated report always lists its assumptions so you can recompute.

---

## Features

- 🧠 **Single-query agent:** diagnose → propose → validate → critique → approve → report.
- 🧮 **Workload mode:** optimizes the top-N queries together. A greedy optimizer picks the indexes with the largest *marginal* reduction of total workload cost, so redundant indexes are never picked twice.
- ⏱️ **Real validation:** actual `EXPLAIN ANALYZE` latency with a real index, always rolled back.
- 💰 **Savings estimate:** from measured runtimes, with every assumption stated.
- 🧹 **Index hygiene:** finds unused, duplicate and redundant indexes, with suggested `DROP` and recreate SQL (report only).
- 🔌 **MCP server:** use QueryDoctor from Claude Code or Claude Desktop. Recommendations still pause for your approval.
- 🖥️ **Streamlit dashboard:** live trace, charts, candidate × query heatmap, approval card, downloads.
- 🎬 **Demo mode:** records every LLM response; `DEMO_MODE=1` replays them, so a live demo can't be broken by API limits.

---

## Safety by design

- **Read-only database role** for the agent (hypothetical indexes work in read-only transactions).
- **Allow-list, not block-list:** only a single `CREATE INDEX` gets through. It's re-generated from the parsed syntax tree, so the raw LLM text never reaches the database. `DROP`, `ALTER`, `DELETE`, multiple statements and unknown columns are all rejected.
- **Numbers come from PostgreSQL**, never from the LLM.
- **Human approval is required.** QueryDoctor only *generates* migrations; you run them.
- **Real validation always rolls back.** It runs only when explicitly enabled and only against a local database.

---

## Quick start

**Requirements:** Docker, Python 3.11+, a [Groq](https://console.groq.com) API key. Give Docker at least 4 GB of memory.

```bash
# 1. Start PostgreSQL 16 with pg_stat_statements + HypoPG (port 5433)
docker compose up -d --build

# 2. Python environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env          # then set GROQ_API_KEY

# 4. Generate TPC-H SF1 (~1 GB of CSV) and load it, primary keys only
python scripts/generate_tpch.py
docker compose exec -T db psql -U postgres -d tpch < scripts/create_tpch.sql
for t in region nation supplier customer part partsupp orders lineitem; do
  docker compose exec -T db psql -U postgres -d tpch -c "COPY $t FROM '/data/$t.csv' CSV HEADER"
done
docker compose exec -T db psql -U postgres -d tpch -c "ANALYZE"

# 5. Map queries and record a workload in pg_stat_statements
python -m querydoctor.tools.query_map
python -m scripts.run_workload
```

### Run it

```bash
# Dashboard
streamlit run ui/app.py

# One query from the terminal (asks for approval)
python -m querydoctor.agent.graph --query q14 [--real-validate] [--calls-per-day 10000]

# Workload mode (top 6 queries together)
python -m querydoctor.agent.workload_graph --top 6 [--real-validate]

# Index hygiene report
python -m querydoctor.tools.hygiene

# Reset the demo database (drops added indexes, resets stats, reruns the workload)
python -m scripts.reset_demo
```

### Use it from Claude (MCP)

```bash
claude mcp add querydoctor -- /absolute/path/to/.venv/bin/python -m querydoctor.mcp_server
```

Then ask: *"Why is q14 slow, and what index would help? Show me the estimate before approving anything."* See [`docs/mcp.md`](docs/mcp.md) for the Claude Desktop setup.

---

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | – | Groq API key |
| `LLM_PROVIDER` / `LLM_MODEL` | `groq` / `openai/gpt-oss-120b` | LLM used for ideas (temperature 0) |
| `DATABASE_URL` | `postgresql://qd_agent:qd_agent@localhost:5433/tpch` | Read-only agent role |
| `ADMIN_DATABASE_URL` | `postgresql://postgres:postgres@localhost:5433/tpch` | Used only by demo scripts and real validation |
| `IMPROVEMENT_THRESHOLD` | `0.30` | Minimum estimated planner-cost reduction to accept |
| `MAX_ITERATIONS` | `3` | Proposal rounds before giving up |
| `ENABLE_REAL_VALIDATION` | `0` | Allow real index + `EXPLAIN ANALYZE` in a rolled-back transaction (local DB only) |
| `CALLS_PER_DAY`, `VCPU_HOUR_PRICE_USD`, `STORAGE_GB_MONTH_USD` | –, `0.04`, `0.10` | Savings-model assumptions |
| `DEMO_MODE` | `0` | Replay recorded LLM responses |

---


## Tests

```bash
pytest -q                               # 76 passed, 1 skipped
RUN_LLM_TESTS=1 pytest -k live          # optional live Groq smoke test
```

Most tests run against the **real** database with real HypoPG. Scripted fake LLMs make the agent's routing deterministic, so the tests prove the session-local behavior, the rollback guarantees and the safety rules rather than assuming them.

---

## Known limitations

- **Planner cost is not runtime.** Estimated reductions are labeled as estimates. Use real validation for actual timings.
- **No partial indexes.** Multi-column partial hypothetical indexes crash the PostgreSQL backend under HypoPG 1.4.3, so QueryDoctor rejects partial indexes. It also recovers if an unknown crash happens.
- **Demo-scale data:** results are from TPC-H SF1 on one local machine.
- **Free-tier LLM limits:** Groq's free tier allows about 200k tokens a day. Use `DEMO_MODE=1` for presentations.

---

## Tech stack

PostgreSQL 16 · `pg_stat_statements` · HypoPG · LangGraph · Groq (`openai/gpt-oss-120b`) · Pydantic · sqlglot · Streamlit · Altair · MCP Python SDK · DuckDB (TPC-H generation) · pytest

---
