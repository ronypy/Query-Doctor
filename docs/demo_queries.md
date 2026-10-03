# Demo query candidates — measured results

Measured 2026-10-03 with `python -m scripts.explore_demo_queries`. Raw numbers are in `data/demo_candidates.json`.

**All percentages are *estimated planner cost reduction*.** They come from PostgreSQL EXPLAIN with HypoPG hypothetical indexes. They are **not** runtime reductions. Runtime (mean ms) comes from pg_stat_statements before any change.

Critic threshold: 30%.

| Query | Mean runtime | Baseline cost | First "obvious" index | Better index | Story |
|---|---|---|---|---|---|
| **Q14** | ~381 ms | 146,331 | `lineitem(l_shipdate)` → **25.9%** (rejected) | `lineitem(l_shipdate) INCLUDE (l_partkey, l_extendedprice, l_discount)` → **93.0%** | retry → accept |
| **Q12** | ~488 ms | 198,578 | `lineitem(l_shipmode, l_receiptdate)` → **23.6%** (rejected) | `... INCLUDE (l_orderkey, l_commitdate, l_shipdate)` → **74.0%** | retry → accept |
| **Q19** | ~414 ms | 184,985 | `lineitem(l_partkey)` → **82.9%** | `lineitem(l_shipinstruct, l_shipmode, l_partkey)` → 94.2% | accept on first try |
| Q6 | ~326 ms | 159,068 | `lineitem(l_shipdate)` → 17.9% | `lineitem(l_shipdate, l_discount, l_quantity) INCLUDE (l_extendedprice)` → 75.6% | retry → accept |
| Q3 | ~2067 ms | 188,715 | `customer(c_mktsegment, c_custkey)` → 1.7% | manual best 28.9%; **the LLM agent found `lineitem(l_orderkey, l_shipdate) INCLUDE (l_extendedprice, l_discount)` → 31.0%** | borderline accept — not a reliable give_up demo |
| Q10 | ~737 ms | 184,538 | `orders(o_orderdate)` → 4.2% | `lineitem(l_orderkey) INCLUDE (l_returnflag, l_extendedprice, l_discount)` → 38.7% | borderline |
| Q18 | ~3519 ms | 672,756 | — | `lineitem(l_orderkey) INCLUDE (l_quantity)` → 40.8% | borderline |
| Q17 | not in workload | 1,949,150 | `lineitem(l_partkey)` → 90.5% | `... INCLUDE (l_quantity, l_extendedprice)` → 96.9% | dramatic, but too slow to run in the workload today |
| Q20 | not in workload | 1.79e9 | `lineitem(l_partkey, l_suppkey)` → 99.99% | — | dramatic, same caveat |
| Q4, Q5, Q2 | | | | best < 10% | not suitable |

## Recommended demo set
1. **Q14**: the main demo. The plain index is rejected at 25.9%, the critic asks for a covering index, and the retry reaches 93%.
2. **Q12**: the same retry pattern on a different bottleneck (shipmode + receiptdate filter).
3. **Q19**: the agent finds a strong index (≥ 80%) on the first try.
4. To show **give_up**, run any query with a high threshold, e.g. `--query q12 --threshold 0.8 --max-iterations 2`. Q3 is NOT reliable for this: in a live agent run the LLM found a 31.0% index.

## Caveats
- **Covering indexes are large:** 266–307 MB, versus 974 MB for lineitem. The report must mention the storage and write-overhead trade-off.
- **Proposals vary:** what the LLM proposes on the first try varies, so the retry story isn't guaranteed. Use the planned `DEMO_MODE` LLM cache to pin it for the live demo.
- **Q17 and Q20:** adding them to the workload needs a longer timeout, since without `l_partkey` indexes they run very long.

## Live agent runs (Phase D, 2026-10-03, Groq openai/gpt-oss-120b)
- **Q14:** iteration 1 accepted `lineitem(l_shipdate, l_partkey) INCLUDE (l_extendedprice, l_discount)` at 93.0%.
- **Q3:** iteration 1 accepted `lineitem(l_orderkey, l_shipdate) INCLUDE (l_extendedprice, l_discount)` at 31.0%.
- **Q19:** iteration 1 found 95.0% (415 MB). The human rejected it with "prefer a smaller index without INCLUDE columns". Iteration 2 found `lineitem(l_shipmode, l_shipinstruct, l_partkey, l_quantity)` at 94.2% (316 MB), which was approved.
- **Q12 at an 80% threshold with max 2 iterations:** 10.1% → retry → 74.0% → give_up, reported honestly.
- At the default 30% threshold, the LLM often proposes covering indexes on the first try, so a retry is not guaranteed. Pin it with the planned DEMO_MODE cache if the retry story matters for the pitch.
