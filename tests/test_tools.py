import pytest

from querydoctor.tools.explain import explain_query, summarize_plan
from querydoctor.tools.hypopg import (
    create_hypothetical_index,
    list_hypothetical_indexes,
    reset_hypothetical,
)
from querydoctor.tools.query_map import get_query_sql, lookup
from querydoctor.tools.schema import get_table_info
from querydoctor.tools.slow_queries import get_slow_queries
from querydoctor.tools.validate import compare_costs


pytestmark = pytest.mark.db


def test_get_slow_queries_returns_multiple(conn):
    queries = get_slow_queries(limit=10)
    assert len(queries) >= 3
    for key in ("queryid", "query", "calls", "mean_exec_time",
                "total_exec_time", "rows"):
        assert key in queries[0]
    totals = [q["total_exec_time"] for q in queries]
    assert totals == sorted(totals, reverse=True)


def test_slow_queries_map_to_concrete_sql(conn):
    mapped = get_slow_queries(limit=10, only_mapped=True)
    assert len(mapped) >= 3
    for q in mapped:
        assert q["name"].startswith("q")
        assert "$1" not in q["sql"]


def test_query_map_lookup():
    by_name = lookup("q12")
    assert by_name is not None
    assert lookup(by_name["queryid"])["name"] == "q12"
    assert lookup("12")["name"] == "q12"
    assert "l_shipmode" in get_query_sql("Q12")


def test_summarize_plan_detects_lineitem_seq_scan(conn, table_rows):
    summary = summarize_plan(
        explain_query(get_query_sql("q06"), conn=conn), table_rows
    )
    assert summary["total_cost"] > 0
    assert any(s["table"] == "lineitem" for s in summary["seq_scans"])
    assert "lineitem" in summary["large_seq_scans"]


def test_summarize_plan_extracts_joins_and_sorts(conn):
    summary = summarize_plan(explain_query(get_query_sql("q03"), conn=conn))
    assert summary["joins"]
    assert any(j.get("hash_cond") for j in summary["joins"])
    assert any(s.get("sort_key") for s in summary["sorts"])
    assert set(summary["tables"]) == {"customer", "orders", "lineitem"}


def test_get_table_info(conn):
    info = get_table_info(["lineitem", "does_not_exist"], conn=conn)
    assert [t["table"] for t in info] == ["lineitem"]
    lineitem = info[0]
    assert lineitem["estimated_rows"] > 5_000_000
    assert any(c["name"] == "l_shipdate" and c["type"] == "date"
               for c in lineitem["columns"])
    assert any(i["name"] == "lineitem_pkey" for i in lineitem["indexes"])


def test_compare_costs_single_index_reduces_cost(conn):
    r = compare_costs(
        get_query_sql("q14"),
        "CREATE INDEX ON lineitem (l_shipdate) "
        "INCLUDE (l_partkey, l_extendedprice, l_discount)",
        conn=conn,
    )
    assert r["index_used"] is True
    assert r["new_cost"] < r["baseline_cost"]
    assert r["reduction_pct"] >= 30
    assert r["candidates"][0]["est_size_bytes"] > 0


def test_compare_costs_reports_unused_index(conn):
    r = compare_costs(
        get_query_sql("q03"),
        "CREATE INDEX ON orders (o_custkey, o_orderdate)",
        conn=conn,
    )
    assert r["index_used"] is False
    assert r["reduction_pct"] == pytest.approx(0, abs=0.01)


def test_compare_costs_multiple_candidates(conn):
    r = compare_costs(
        get_query_sql("q12"),
        [
            "CREATE INDEX ON lineitem (l_receiptdate)",
            "CREATE INDEX ON lineitem (l_shipmode, l_receiptdate)",
            "DROP TABLE lineitem",
        ],
        conn=conn,
    )
    valid = [c for c in r["candidates"] if c["valid"]]
    assert len(valid) == 2
    assert not r["candidates"][2]["valid"]

    # Each candidate is measured on its own.
    for c in valid:
        assert c["index_used"]
        assert c["new_cost"] < r["baseline_cost"]
    assert valid[0]["reduction_pct"] != valid[1]["reduction_pct"]

    # The set is also measured together, with per-index usage.
    combined = r["combined"]
    assert set(combined["sqls"]) == {c["sql"] for c in valid}
    assert set(combined["indexes_used"]) == set(combined["sqls"])
    assert combined["new_cost"] <= min(c["new_cost"] for c in valid) + 0.01
    assert r["best"]["sql"] == "CREATE INDEX ON lineitem(l_shipmode, l_receiptdate)"


def test_compare_costs_rejects_unsafe_sql_without_touching_db(conn):
    r = compare_costs(get_query_sql("q06"), "DROP TABLE lineitem", conn=conn)
    assert r["candidates"][0]["valid"] is False
    assert r["combined"] is None
    assert r["new_cost"] == r["baseline_cost"]


def test_hypopg_reset_between_experiments(conn):
    sql = get_query_sql("q14")
    baseline = summarize_plan(explain_query(sql, conn=conn))["total_cost"]

    compare_costs(sql, "CREATE INDEX ON lineitem (l_shipdate)", conn=conn)

    # compare_costs leaves no hypothetical index behind on the connection...
    assert list_hypothetical_indexes(conn) == []
    # ...so the next plan is back at baseline.
    assert summarize_plan(explain_query(sql, conn=conn))["total_cost"] == baseline


def test_hypopg_is_session_local(conn):
    from querydoctor.db import get_conn

    create_hypothetical_index(conn, "CREATE INDEX ON lineitem (l_shipdate)")
    other = get_conn()
    try:
        assert len(list_hypothetical_indexes(conn)) == 1
        assert list_hypothetical_indexes(other) == []
    finally:
        other.close()
        reset_hypothetical(conn)
    assert list_hypothetical_indexes(conn) == []
