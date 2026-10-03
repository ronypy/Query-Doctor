import pytest

from querydoctor.db import get_conn


@pytest.fixture(scope="session")
def conn():
    try:
        c = get_conn()
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable: {e}")
    yield c
    c.close()


@pytest.fixture(scope="session")
def column_map(conn):
    from querydoctor.tools.schema import get_column_map
    return get_column_map(conn=conn)


@pytest.fixture(scope="session")
def table_rows(conn):
    from querydoctor.tools.schema import get_table_row_estimates
    return get_table_row_estimates(conn=conn)
