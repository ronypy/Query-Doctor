# QueryDoctor 🩺

> **An AI database engineer that finds your slow queries, proves which fixes work without touching production, and asks before it changes anything.**

---

## Inspiration

Every team running PostgreSQL eventually hits the same wall: a dashboard that used to load instantly now takes four seconds, and nobody knows why. The fix is often a single well-chosen index. Choosing that index, though, is expert work. You have to read execution plans, understand selectivity, weigh storage and write overhead, and avoid indexes the planner will simply ignore.

My research is on **learned index selection**, using reinforcement learning to decide which indexes a database should have. Index selection is a famously hard combinatorial problem. With $n$ candidate indexes there are $2^n$ possible configurations, and each configuration's benefit depends on every query in the workload. Research systems tackle this with cost models and search. What I wanted to explore at this hackathon was a different question:

> *Can a large language model act like a careful database engineer, proposing ideas the way a human would, while every claim it makes is verified by the database itself?*

I wanted that last part to be non-negotiable. An LLM that confidently says "this index will make your query 10× faster" is dangerous if nobody checks. So QueryDoctor is built around one principle:

> **The LLM proposes. PostgreSQL decides. A human approves.**

---

## What it does

1. **Finds slow queries** from `pg_stat_statements`, using measured runtimes and call counts.
2. **Reads the execution plan** (`EXPLAIN (FORMAT JSON)`) and summarizes the bottleneck deterministically: sequential scans on large tables, filters, join keys and sort keys.
3. **Asks an LLM to propose indexes** using structured output (Pydantic), at most three candidates per round.
4. **Checks every proposal for safety** with a `sqlglot` allow-list. Only a single plain `CREATE INDEX` on existing tables and columns gets through; anything else is rejected.
5. **Validates the candidates with hypothetical indexes** (HypoPG). PostgreSQL re-plans the query as if the index existed, without building it.
6. **Critiques its own results.** A deterministic critic accepts an index only if the planner *actually uses it* and the estimated cost drops by at least a threshold. Otherwise it loops back with feedback.
7. **Pauses for human approval** via a LangGraph `interrupt()`. Rejecting with feedback sends the agent back to propose again.
8. **Optionally measures the real effect.** It builds the real index inside a transaction that is **always rolled back**, then measures with `EXPLAIN ANALYZE`.
9. **Generates a migration** (`CREATE INDEX CONCURRENTLY IF NOT EXISTS …`), a rollback, and a report that clearly separates *measured* numbers from *estimated* ones.

On top of the core loop:

- **Workload mode** optimizes the top-$N$ queries together, looking for indexes that help several queries at once.
- **Index hygiene** finds unused, duplicate and redundant indexes.
- **Monthly savings estimate** is computed only from measured runtimes.
- **MCP server** lets Claude Code or Claude Desktop use QueryDoctor as a tool, with the human still approving.
- **Streamlit dashboard** shows the agent's step-by-step trace, the charts and the approval card.

---

## How I built it

### Architecture

![QueryDoctor architecture](architecture.png)

```text
        ┌─────────────────────────────────────────────┐
        │   PostgreSQL 16  ·  TPC-H SF1 (8.6M rows)   │
        │   pg_stat_statements  ·  HypoPG             │
        └──────────────────────┬──────────────────────┘
                               │ slow queries + plans
                               ▼
  ┌───────────────────────────────────────────────────────────┐
  │              QueryDoctor agent  (LangGraph)               │
  │                                                           │
  │   1. Fetch context     plan, schema, measured runtime     │
  │   2. Diagnose          LLM explains the bottleneck        │
  │   3. Propose     ◄──┐  LLM suggests up to 3 indexes       │
  │   4. Safety check   │  sqlglot: single CREATE INDEX only  │
  │   5. What-if test   │  HypoPG: planner cost estimate      │
  │   6. Critic ────────┘  retry if not used or < 30% cheaper │
  │        │ passes                                           │
  │        ▼                                                  │
  │   7. Human approval    approve, or reject + feedback → 3  │
  │   8. Real validation   build → measure → ROLLBACK         │
  │   9. Report            migration · rollback · $ savings   │
  └─────────────────────────────┬─────────────────────────────┘
                                │
                                ▼
        Streamlit dashboard  ·  MCP server (Claude)  ·  CLI

   LLM = ideas only.  Every number comes from PostgreSQL.
```

### Tech stack

| Layer | Choice |
|---|---|
| Database | PostgreSQL 16 in Docker, with `pg_stat_statements` and `hypopg` |
| Benchmark | TPC-H scale factor 1 (8,661,245 rows), generated with DuckDB, **primary keys only** |
| Agent orchestration | LangGraph: state graph, conditional loops, `interrupt()`, SQLite checkpointer |
| LLM | Groq `openai/gpt-oss-120b`, temperature 0, Pydantic structured output |
| SQL safety | `sqlglot` AST validation, regenerating the SQL before execution |
| UI | Streamlit + Altair |
| Tool protocol | MCP Python SDK (stdio server) |
| Tests | `pytest`: 77 tests against a real database, with scripted fake LLMs for deterministic routing |

### Separating responsibilities

The most important design decision was a strict split:

| The LLM does | Deterministic code + PostgreSQL does |
|---|---|
| Explain the bottleneck in plain language | Slow-query statistics, plans, schema |
| Propose candidate indexes | Safety validation |
| Write feedback for the next round | Cost numbers, index-usage detection |
| | Accept / retry / give-up decisions |
| | Migration SQL, reports, savings math |

Every number in a report traces back to `pg_stat_statements`, `EXPLAIN` or `EXPLAIN ANALYZE`. The LLM never states a percentage.

### The math behind the decisions

**Single-query acceptance.** For a query with baseline planner cost $C_0$ and cost $C_i$ under hypothetical index $i$, the estimated reduction is

$$
\Delta_i \;=\; \frac{C_0 - C_i}{C_0} \times 100\%.
$$

The critic accepts index $i$ only if

$$
\text{used}(i) \;\wedge\; \Delta_i \;\ge\; \tau, \qquad \tau = 30\%,
$$

where $\text{used}(i)$ means the hypothetical index actually appears in the new plan. Without that condition, an index that changes nothing could still "pass".

**Workload mode.** For a workload $Q$, where query $q$ has $\text{calls}_q$ calls and planner cost $C_q(S)$ under index set $S$, the objective is the total weighted planner cost

$$
W(S) \;=\; \sum_{q \in Q} \text{calls}_q \cdot C_q(S).
$$

Choosing the best set is the classic index-selection problem, which is NP-hard in general. QueryDoctor uses a greedy approach based on **marginal gain**. In each round it re-plans every query with all already-selected indexes *plus* one more candidate $c$:

$$
g(c \mid S) \;=\; \frac{W(S) - W(S \cup \{c\})}{W(\varnothing)} \times 100\% \;-\; \lambda \cdot \text{size}_{\text{GB}}(c),
$$

$$
c^{*} \;=\; \arg\max_{c \,\notin\, S} \; g(c \mid S).
$$

Here $\lambda$ is an optional penalty that models storage and write overhead. Selection stops when $|S|$ reaches a limit or when $g(c^{*} \mid S)$ falls below a minimum gain. Because the gain is *marginal*, a redundant index (one whose benefit is already provided by $S$) scores roughly $0$ and is never picked twice.

**Savings, from measured runtimes only.** With measured medians $t_{\text{before}}$ and $t_{\text{after}}$ (in ms), an assumed call rate $r$ per day, and a stated vCPU-hour price $p$:

$$
\text{hours}_{\text{month}} \;=\; \frac{(t_{\text{before}} - t_{\text{after}}) \cdot r \cdot 30}{1000 \cdot 3600},
\qquad
\$_{\text{net}} \;=\; p \cdot \text{hours}_{\text{month}} \;-\; s \cdot \text{size}_{\text{GB}},
$$

where $s$ is the storage price per GB-month. The difference is **signed**: if a query gets slower, the formula counts that as a cost instead of rounding it to zero.

### Making real validation safe

Measuring the actual effect of an index means building a real one, and that's the one thing an agent like this must never do carelessly. The solution was PostgreSQL's **transactional DDL**:

```sql
BEGIN;
  EXPLAIN (ANALYZE, TIMING OFF) <query>;   -- warm-up + 3 runs → median "before"
  CREATE INDEX qd_rv_… ON …;               -- real index, invisible to other sessions
  EXPLAIN (ANALYZE, TIMING OFF) <query>;   -- 3 runs → median "after"
ROLLBACK;                                  -- the index disappears, always
```

Python enforces this with `conn.transaction(force_rollback=True)`. Even if the process crashes, PostgreSQL aborts the transaction. An advisory lock serializes concurrent runs, and the feature only works when it's explicitly enabled and pointed at a local database.

---

## Results

All "estimated" numbers are PostgreSQL planner costs with hypothetical indexes. "Measured" numbers come from real `EXPLAIN ANALYZE` runs on the local demo database (TPC-H SF1, warm cache).

| Query | First idea | Best index found | Est. planner cost reduction | Measured runtime |
|---|---|---|---|---|
| Q14 | `lineitem(l_shipdate)`: 25.9% ✗ | covering index on `l_shipdate` | **93.0%** | 254 ms → 49 ms (**5.2×**) |
| Q12 | `(l_shipmode, l_receiptdate)`: 23.6% ✗ | covering version | **74.0%** | — |
| Q19 | — | `lineitem(l_partkey, l_shipmode, …)` | **95.0%** | 473 ms → 25 ms (**18.7×**, workload run) |
| Q18 | — | `lineitem(l_orderkey) INCLUDE (l_quantity)` | **40.8%** | 3.4 s → 1.1 s (**3.0×**) |
| Workload (6 queries) | — | 3 shared indexes | **31.5%** of total workload cost | Q18 5.0×, Q19 18.7× |

---

## Accomplishments that I'm proud of

- **A complete agent loop that runs on its own and is honest about its numbers.** QueryDoctor finds the problem, proposes fixes, measures them, retries, asks a human, and writes the migration. Not a single number in its reports comes from the LLM.
- **Real, measured speed-ups:** Q19 **18.7×** faster, Q14 **5.2×**, Q18 **3.0×**. These were measured with real indexes that were then rolled back, so the database was left untouched.
- **Index selection across a whole workload.** One shared index set reduced the estimated cost of six queries by **31.5%**, and the optimizer automatically skipped indexes that would have been redundant.
- **Real validation that can't leave a mess.** Using transactional DDL, a real index is built, measured and always rolled back.
- **Found and contained a database-crashing bug.** I traced PostgreSQL restarts to a HypoPG segfault, isolated the exact trigger, and made QueryDoctor both avoid it and recover from crashes it doesn't know about.
- **The agent beat my own analysis.** For Q3, the LLM found a 31.0% index where my manual exploration had topped out at 28.9%. The database verified it, not me.
- **77 tests against a real PostgreSQL**, plus an MCP server so Claude can use QueryDoctor as a tool, with the human still approving.

### What this can save, in dollars

QueryDoctor computes savings **only from measured runtimes**, never from planner estimates:

$$
\text{vCPU-hours saved per month} = \frac{(t_{\text{before}} - t_{\text{after}})\,[\text{s}] \times \text{calls per day} \times 30}{3600}
$$

**Example: one index (Q19).** Real validation measured **473 ms → 25 ms**, which saves 0.448 s per call. Suppose this query runs **100,000 times a day**, a modest load for a reporting API (this is an assumption):

$$
\frac{0.448 \times 100{,}000 \times 30}{3600} \approx 373 \text{ vCPU-hours/month}
$$

| Assumed price per vCPU-hour | Monthly saving | Yearly saving |
|---|---|---|
| \$0.04 (commodity compute) | ≈ \$15 | ≈ \$180 |
| \$0.10 (managed database) | ≈ \$37 | ≈ \$450 |

All of that comes from one index that costs a few cents a month in storage.

**Example: the workload (6 queries, 3 indexes).** At 100,000 calls per day *per query*, the measured improvements free up about **4,450 vCPU-hours a month**. That's roughly **6 vCPUs running nonstop**, worth about **\$178–\$445 per month (≈ \$2,100–\$5,300 per year)** at the prices above. This figure already *subtracts* the two queries that got slightly slower, and index storage adds only about \$0.06 a month.

The bigger win is often an avoided upgrade. Freeing six busy cores can be the difference between staying on the current database instance and moving up a size. Developer time counts too: diagnosing one slow query by hand can easily take an engineer an afternoon.

> These figures scale linearly with the assumed call rate and price. They come from a single local machine running TPC-H SF1 with a warm cache. QueryDoctor's report always lists the assumptions, so you can plug in your own.

---

## Challenges I faced

### 1. You can't `EXPLAIN` a normalized query
`pg_stat_statements` replaces literals with placeholders (`WHERE l_shipdate >= $1`), and PostgreSQL can't plan that. I needed to map each statistics entry back to its concrete SQL. My first idea was to execute every query and record its ID. I then discovered that `EXPLAIN (VERBOSE)` returns the same **Query Identifier** that `pg_stat_statements` uses, so all 22 TPC-H queries could be mapped in milliseconds without running a single one.

### 2. Hypothetical indexes live in one session only
HypoPG indexes exist only in the backend that created them. Creating the index on one connection and running `EXPLAIN` on another silently shows *no effect at all*. Every experiment had to run on one dedicated connection and call `hypopg_reset()` between candidates, and a test now proves the indexes really are session-local.

### 3. "Used" is not the same as "useful"
Early experiments showed indexes the planner *did* use but that barely helped (Q6: 11% reduction). They also showed indexes that looked perfect on paper and were *never used* (Q3: 0%). That's why the critic requires both conditions. It's also why the more interesting finding was that **covering indexes** (`INCLUDE`), which enable index-only scans, were what pushed most queries past the threshold.

### 4. A HypoPG bug that crashed the whole database
During workload mode, PostgreSQL suddenly **restarted itself**. The logs showed `terminated by signal 11: Segmentation fault`. I isolated it to a single trigger: a **multi-column partial hypothetical index** segfaults the backend under HypoPG 1.4.3 while planning TPC-H Q18. The crash restarted every connection and wiped all `pg_stat_statements` data along the way. The fix had two parts:
- The safety layer now rejects partial indexes entirely.
- If an unknown crash still happens, the engine waits for the server, reconnects, excludes that candidate, and continues instead of failing the run.

### 5. Planner cost is not runtime, and the data proved it
I deliberately never treated "93% lower planner cost" as "93% faster". Real validation justified that caution. In one workload run, Q3 and Q10 *chose* the new indexes but actually ran **slightly slower** (0.9×). The report now labels every measured change:
- faster
- no change (within a ±15% noise band)
- a regression

### 6. Measurement overhead distorted the numbers
My first real validation of Q18 reported 5.7 s before the index, but `pg_stat_statements` said the query takes about 3.4 s. The difference was `EXPLAIN ANALYZE`'s per-node timing overhead. Switching to `TIMING OFF` gave numbers consistent with production statistics.

### 7. Making the LLM reliable
- **Structured output failures.** With long prompts, the model sometimes returned the *JSON schema itself* instead of filling it in. I benchmarked three output modes; function calling went 4/4 and was fastest, with strict JSON schema as a fallback.
- **Overfitting to literals.** The model proposed partial indexes that hardcoded one query's exact date parameters, which would be useless the next day.
- **Feedback that contradicted the data.** The LLM critic once claimed an index "was not used" when the measurements said it was. I rewrote the prompt to state measured facts in an unambiguous format.

### 8. Rate limits during development
Groq's free tier returned `503 over capacity`, and later `429: tokens per day (TPD) limit 200,000`, in the middle of testing. I added exponential backoff, and then a **record/replay cache**: every LLM response is stored, and `DEMO_MODE=1` replays recorded responses, so a slow or rate-limited API can't break a live demo.

---

## What I learned

- **LLMs are good at ideas and bad at numbers.** Ideas are cheap to verify, so letting the model brainstorm and letting the database judge plays to both strengths. The LLM even found a better Q3 index (31.0%) than my own manual exploration (28.9%).
- **Determinism makes a demo reliable.** The critic and the workload optimizer are plain code. The LLM's output varies, but the *decisions* about what to accept are reproducible.
- **PostgreSQL is more capable than I thought.** I came away with a new appreciation for transactional DDL, `EXPLAIN (VERBOSE)` query identifiers, index-only scans and the visibility map, and `pg_relation_size` versus HypoPG's size estimates (232 MB actual vs. 266 MB estimated).
- **Index selection is a set problem, not a per-query problem.** In workload mode, the best index for Q18, `lineitem(l_orderkey) INCLUDE (l_quantity)`, also helped Q3, Q10 and Q5. Meanwhile a second index that looked great on its own added only 4.3% once the first existed. Marginal gain is the right lens.
- **Honesty has to be designed in.** Labeling every figure as *measured* or *estimated*, counting regressions as costs, and showing "no recommendation" when nothing passes all took deliberate effort. They're also what makes the tool trustworthy.
- **Test against the real thing.** Most of the 77 tests run against a real PostgreSQL with real HypoPG, using scripted fake LLMs only to make the agent's routing deterministic. That's how the session-local behavior, the rollback guarantees and the crash recovery were verified rather than assumed.

---

## Safety by design

- **Read-only database role** for the agent. Hypothetical indexes work even in read-only transactions.
- **Allow-list, not block-list:** only a single `CREATE INDEX`, re-generated from the parsed syntax tree, ever reaches the database.
- **Numbers come from PostgreSQL**, never from the LLM.
- **Human approval** is required. The output is a reviewable migration with a rollback, and QueryDoctor never applies it.
- **Real validation rolls back**, runs only against a local database, and must be explicitly enabled.

---

## What's next

- **Learned index selection:** replace the greedy optimizer with a reinforcement-learning policy, connecting this project directly to my research.
- **Storage budgets and write-cost modeling** for write-heavy workloads.
- **CI integration:** comment on pull requests that introduce slow queries, with a validated index suggestion.
- **More engines:** MySQL and other databases with what-if index support.

---

*Built at an MLH hackathon with PostgreSQL, HypoPG, LangGraph, Groq, Streamlit and the Model Context Protocol.*
