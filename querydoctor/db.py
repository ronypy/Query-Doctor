import psycopg

from querydoctor.config import get_settings


def get_conn(admin: bool = False):
    """
    Agent connection (default): qd_agent role, read-only transactions.
    HypoPG works on this connection too — hypothetical indexes live only
    in backend memory, so read-only mode does not block them.

    Admin connection: only for scripts / optional real validation.
    """

    settings = get_settings()

    url = settings.admin_database_url if admin else settings.database_url

    conn = psycopg.connect(url, autocommit=True)

    with conn.cursor() as cur:
        cur.execute(
            f"SET statement_timeout = {int(settings.statement_timeout_ms)}"
        )

        if not admin:
            cur.execute("SET default_transaction_read_only = on")

    return conn
