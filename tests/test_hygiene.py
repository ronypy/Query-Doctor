"""
Hygiene tests use a scratch table created inside an admin transaction that
is always rolled back, so the catalog checks see it but nothing persists.
"""

import pytest

from querydoctor.db import get_conn
from querydoctor.tools.hygiene import analyze_index_hygiene, hygiene_markdown


pytestmark = pytest.mark.db


@pytest.fixture
def scratch(conn):
    admin = get_conn(admin=True)
    try:
        with admin.transaction(force_rollback=True):
            with admin.cursor() as cur:
                cur.execute("""
                    CREATE TABLE qd_hyg_test (
                        id int PRIMARY KEY, a int, b int, c int, d text,
                        u int UNIQUE
                    );
                    CREATE INDEX qd_hyg_a      ON qd_hyg_test (a);
                    CREATE INDEX qd_hyg_a_dup  ON qd_hyg_test (a);
                    CREATE INDEX qd_hyg_a_b    ON qd_hyg_test (a, b);
                    CREATE INDEX qd_hyg_b_desc ON qd_hyg_test (b DESC);
                    CREATE INDEX qd_hyg_b_c    ON qd_hyg_test (b, c);
                    CREATE INDEX qd_hyg_c_part ON qd_hyg_test (c) WHERE c > 0;
                    CREATE INDEX qd_hyg_c_full ON qd_hyg_test (c, d);
                    CREATE INDEX qd_hyg_u_copy ON qd_hyg_test (u);
                """)
            result = analyze_index_hygiene(conn=admin)
            yield {
                f["index"]: f for f in result["findings"]
                if f["table"] == "qd_hyg_test"
            }, result
    finally:
        admin.close()


def test_duplicate_detected_and_one_copy_kept(scratch):
    findings, _ = scratch
    dup = [n for n in ("qd_hyg_a", "qd_hyg_a_dup")
           if "duplicate" in findings.get(n, {}).get("kinds", [])]
    assert len(dup) == 1


def test_prefix_redundant_detected(scratch):
    findings, _ = scratch
    # whichever copy of (a) is not the duplicate is redundant to (a, b)
    redundant = [n for n in ("qd_hyg_a", "qd_hyg_a_dup")
                 if "redundant" in findings.get(n, {}).get("kinds", [])]
    assert redundant
    assert "qd_hyg_a_b" in findings[redundant[0]]["related"]


def test_not_redundant_when_sort_order_or_predicate_differs(scratch):
    findings, _ = scratch
    # (b DESC) vs (b, c): different option flags -> not a prefix
    assert "redundant" not in findings.get("qd_hyg_b_desc", {}).get("kinds", [])
    # partial index is never treated as redundant
    assert "redundant" not in findings.get("qd_hyg_c_part", {}).get("kinds", [])


def test_constraint_and_unique_indexes_never_flagged(scratch):
    findings, _ = scratch
    assert "qd_hyg_test_pkey" not in findings
    assert "qd_hyg_test_u_key" not in findings
    # a plain index duplicating a UNIQUE constraint's column IS flagged
    assert "duplicate" in findings["qd_hyg_u_copy"]["kinds"] or \
        "unused" in findings["qd_hyg_u_copy"]["kinds"]


def test_unused_and_sql_suggestions(scratch):
    findings, result = scratch
    f = findings["qd_hyg_a_b"]
    assert "unused" in f["kinds"]
    assert f["drop_sql"] == 'DROP INDEX CONCURRENTLY IF EXISTS public."qd_hyg_a_b";'
    assert f["recreate_sql"].startswith("CREATE INDEX CONCURRENTLY qd_hyg_a_b ON")
    assert result["reclaimable_bytes"] >= sum(x["size_bytes"] for x in findings.values())
    assert "Suggested cleanup" in hygiene_markdown(result)


def test_clean_pk_only_tables_have_no_findings_on_tpch(conn):
    result = analyze_index_hygiene(conn=conn)
    for f in result["findings"]:
        assert not f["index"].endswith("_pkey")
