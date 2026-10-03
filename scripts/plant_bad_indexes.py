"""
Create deliberately bad indexes so the index-hygiene feature has something
to find. Local demo database only; `python -m scripts.reset_demo` removes them.

    python -m scripts.plant_bad_indexes
"""

from querydoctor.db import get_conn
from scripts.reset_demo import assert_local_admin_db


BAD_INDEXES = [
    # exact duplicate pair
    "CREATE INDEX IF NOT EXISTS qd_demo_orders_custkey ON orders (o_custkey)",
    "CREATE INDEX IF NOT EXISTS qd_demo_orders_custkey_dup ON orders (o_custkey)",
    # prefix-redundant pair: (p_brand) is a prefix of (p_brand, p_container)
    "CREATE INDEX IF NOT EXISTS qd_demo_part_brand ON part (p_brand)",
    "CREATE INDEX IF NOT EXISTS qd_demo_part_brand_container ON part (p_brand, p_container)",
    # unused index on a wide text column no workload query filters on
    "CREATE INDEX IF NOT EXISTS qd_demo_customer_comment ON customer (c_comment)",
]


def plant_bad_indexes(verbose: bool = True) -> list[str]:

    assert_local_admin_db()

    conn = get_conn(admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET maintenance_work_mem = '256MB'")
            for sql in BAD_INDEXES:
                cur.execute(sql)
                if verbose:
                    print(sql)
            cur.execute("ANALYZE orders, part, customer")
    finally:
        conn.close()

    return BAD_INDEXES


if __name__ == "__main__":
    plant_bad_indexes()
    print("\nPlanted. Remove with: python -m scripts.reset_demo")
