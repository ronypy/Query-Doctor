import pytest

from querydoctor.tools.safety import check_index_sql, validate_index_sql


COLUMNS = {
    "lineitem": {"l_orderkey", "l_partkey", "l_shipdate", "l_discount",
                 "l_quantity", "l_extendedprice", "l_tax", "l_comment"},
    "orders": {"o_orderkey", "o_orderdate"},
}


@pytest.mark.parametrize("sql", [
    "CREATE INDEX ON lineitem (l_shipdate)",
    "create index on lineitem (l_shipdate, l_discount, l_quantity);",
    "CREATE INDEX idx ON public.lineitem USING btree (l_shipdate DESC)",
    "CREATE INDEX ON lineitem (l_shipdate) INCLUDE (l_extendedprice)",
    "CREATE INDEX ON orders (o_orderdate) WHERE o_orderdate >= DATE '1994-01-01'",
    "CREATE INDEX ON lineitem USING brin (l_shipdate)",
])
def test_valid_index_sql_accepted(sql):
    ok, reason = validate_index_sql(sql, COLUMNS)
    assert ok, reason


@pytest.mark.parametrize("sql, reason_part", [
    ("DROP TABLE lineitem", "Only CREATE INDEX"),
    ("ALTER TABLE lineitem ADD COLUMN x int", "Only CREATE INDEX"),
    ("DELETE FROM lineitem", "Only CREATE INDEX"),
    ("UPDATE lineitem SET l_tax = 0", "Only CREATE INDEX"),
    ("INSERT INTO orders VALUES (1, DATE '1994-01-01')", "Only CREATE INDEX"),
    ("CREATE TABLE x (a int)", "Only CREATE INDEX"),
    ("SELECT 1", "Only CREATE INDEX"),
    ("CREATE INDEX ON lineitem (l_shipdate); DROP TABLE lineitem", "one statement"),
    ("CREATE INDEX ON nope (a)", "Unknown table"),
    ("CREATE INDEX ON lineitem (l_bogus)", "Unknown column"),
    ("CREATE INDEX ON lineitem (l_shipdate) INCLUDE (l_bogus)", "Unknown column"),
    ("CREATE INDEX ON lineitem (l_shipdate) WHERE l_bogus > 1", "Unknown column"),
    ("CREATE INDEX ON lineitem (l_orderkey, l_partkey, l_shipdate, l_discount, l_quantity)",
     "Too many key columns"),
    ("CREATE UNIQUE INDEX ON lineitem (l_shipdate)", "UNIQUE"),
    ("CREATE INDEX ON lineitem USING gin (l_comment)", "not allowed"),
    ("CREATE INDEX ON other.lineitem (l_shipdate)", "schema public"),
    ("CREATE INDEX ON lineitem (l_shipdate) WHERE l_orderkey IN (SELECT 1)", "Subqueries"),
    ("", "Empty"),
])
def test_unsafe_sql_rejected(sql, reason_part):
    ok, reason = validate_index_sql(sql, COLUMNS)
    assert not ok
    assert reason_part.lower() in reason.lower()


def test_normalized_sql_strips_concurrently_and_comments():
    r = check_index_sql(
        "CREATE INDEX CONCURRENTLY ON lineitem (l_shipdate) -- ; DROP TABLE lineitem",
        COLUMNS,
    )
    assert r["ok"]
    assert "CONCURRENTLY" not in r["sql"].upper()
    assert "DROP" not in r["sql"].upper()
    assert r["table"] == "lineitem"
    assert r["columns"] == ["l_shipdate"]


@pytest.mark.db
def test_drop_rejected_against_live_schema(column_map):
    ok, _ = validate_index_sql("DROP TABLE lineitem", column_map)
    assert not ok
    ok, reason = validate_index_sql("CREATE INDEX ON lineitem (l_shipdate)", column_map)
    assert ok, reason
